import assert from 'node:assert/strict';
import { once } from 'node:events';
import { test } from 'node:test';
import { ApiError } from '../src/errors.ts';
import { MOWERS_PATH, RawReply, STATE_PATH, createPrivateServer, type MapRequest, type PrivateApi } from '../src/server.ts';
import { TOKEN, baselineHandles, call, settledHandles } from './helpers.ts';

const maps: { id: string; request: MapRequest }[] = [];
const settings: { id: string; key: string; value: string | null }[] = [];
const BUNDLE = Buffer.from('synthetic bundle bytes');

async function withServer(run: (base: string, calls: () => number) => Promise<void>): Promise<void> {
  let calls = 0;
  const api: PrivateApi = {
    state: () => {
      calls += 1;
      return { protocol: 1, lifecycle: 'running' };
    },
    discover: async () => ({ contract: 1, mowers: [] }),
    mowerState: async (id: string) => {
      if (id === 'missing') throw new ApiError(404, 'unknown_mower');
      if (id === 'broken') throw new Error('upstream detail that must not leak');
      return { contract: 1, id };
    },
    command: async (id: string, kind: string) => {
      if (id === 'observed') throw new ApiError(403, 'control_disabled');
      return { contract: 1, id, command: kind };
    },
    setting: async (id: string, key: string, value: string | null) => {
      settings.push({ id, key, value });
      if (id === 'observed') throw new ApiError(403, 'settings_disabled');
      return { contract: 1, id, setting: key };
    },
    map: async (id: string, request: MapRequest) => {
      maps.push({ id, request });
      if (id === 'unconfigured') throw new ApiError(404, 'map_unconfigured');
      const headers = { ETag: '"tag"', 'X-Eufy-Map-Age-Ms': '12' };
      if (request.ifNoneMatch === '"tag"') return new RawReply(304, headers, null);
      return new RawReply(200, { ...headers, 'Content-Type': 'application/vnd.eufy-robomow-map+zip' }, BUNDLE);
    },
  };
  const server = createPrivateServer(TOKEN, api);
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  try {
    await run(`http://127.0.0.1:${address.port}`, () => calls);
  } finally {
    server.close();
    server.closeAllConnections();
    await once(server, 'close');
  }
}

test('every request needs the exact bearer token before any route is visible', async () => {
  const handles = await baselineHandles();
  await withServer(async (base, calls) => {
    for (const authorization of [undefined, '', 'Bearer', `Bearer ${TOKEN.slice(0, -1)}`, `Bearer ${TOKEN.slice(0, -1)}X`, `Basic ${TOKEN}`, `bearer ${TOKEN}`, `Bearer  ${TOKEN}`]) {
      const reply = await call(base, STATE_PATH, authorization === undefined ? {} : { authorization });
      assert.equal(reply.status, 401, `authorization ${JSON.stringify(authorization)}`);
      assert.deepEqual(reply.json, { error: 'unauthorized' });
      assert.equal(reply.headers['www-authenticate'], 'Bearer');
    }
    for (const path of [MOWERS_PATH, `${MOWERS_PATH}/missing/state`, '/v1/other', '/']) {
      assert.equal((await call(base, path)).status, 401, `${path} is not distinguishable without the token`);
    }
    assert.equal(calls(), 0, 'the state document is never built for unauthorized requests');
  });
  assert.deepEqual(await settledHandles(handles), handles);
});

test('the routes are served without caching and refuse other methods and paths', async () => {
  await withServer(async (base, calls) => {
    const reply = await call(base, STATE_PATH, { token: TOKEN });
    assert.equal(reply.status, 200);
    assert.equal(reply.headers['content-type'], 'application/json');
    assert.equal(reply.headers['cache-control'], 'no-store');
    assert.equal(reply.headers['content-length'], String(Buffer.byteLength(reply.text)));
    assert.deepEqual(reply.json, { protocol: 1, lifecycle: 'running' });
    assert.equal(calls(), 1);
    const query = await call(base, `${STATE_PATH}?verbose=1`, { token: TOKEN });
    assert.equal(query.status, 200, 'query strings are ignored');
    const mowers = await call(base, MOWERS_PATH, { token: TOKEN });
    assert.equal(mowers.status, 200);
    assert.deepEqual(mowers.json, { contract: 1, mowers: [] });
    const state = await call(base, `${MOWERS_PATH}/abc/state`, { token: TOKEN });
    assert.equal(state.status, 200);
    assert.deepEqual(state.json, { contract: 1, id: 'abc' });
    for (const method of ['POST', 'PUT', 'DELETE']) {
      for (const path of [STATE_PATH, MOWERS_PATH, `${MOWERS_PATH}/abc/state`]) {
        const refused = await call(base, path, { token: TOKEN, method, body: '{"command":"start"}' });
        assert.equal(refused.status, 405, `${method} ${path}`);
        assert.equal(refused.headers.allow, 'GET');
        assert.deepEqual(refused.json, { error: 'method_not_allowed' });
      }
    }
    const command = await call(base, `${MOWERS_PATH}/abc/commands/start`, { token: TOKEN, method: 'POST', body: 'ignored body' });
    assert.equal(command.status, 200, 'the command route is POST and addressed by path');
    assert.deepEqual(command.json, { contract: 1, id: 'abc', command: 'start' });
    for (const method of ['GET', 'PUT', 'DELETE']) {
      const refused = await call(base, `${MOWERS_PATH}/abc/commands/start`, { token: TOKEN, method });
      assert.equal(refused.status, 405, `${method} command`);
      assert.equal(refused.headers.allow, 'POST');
      assert.deepEqual(refused.json, { error: 'method_not_allowed' });
    }
    for (const path of ['/v1/state/', '/v1/mowers/', '/v1/mowers/abc', '/v1/mowers/abc/state/extra', '/v1/mowers/a/b/state', '/v1/mowers/abc/commands', '/v1/mowers/abc/commands/', '/v1/mowers/abc/commands/START', '/v1/mowers/abc/commands/start/extra', '/v1/mowers/abc/commands/start-now', '/v1/login', '/v1/cameras', '/health', '/']) {
      const missing = await call(base, path, { token: TOKEN });
      assert.equal(missing.status, 404, path);
      assert.deepEqual(missing.json, { error: 'not_found' });
    }
    assert.equal(calls(), 2, 'refused requests never touch the state document');
  });
});

test('route failures carry only their status and stable code', async () => {
  await withServer(async (base) => {
    const missing = await call(base, `${MOWERS_PATH}/missing/state`, { token: TOKEN });
    assert.equal(missing.status, 404);
    assert.deepEqual(missing.json, { error: 'unknown_mower' });
    const broken = await call(base, `${MOWERS_PATH}/broken/state`, { token: TOKEN });
    assert.equal(broken.status, 500);
    assert.deepEqual(broken.json, { error: 'internal_error' });
    assert.ok(!broken.text.includes('upstream detail'));
    const forbidden = await call(base, `${MOWERS_PATH}/observed/commands/start`, { token: TOKEN, method: 'POST' });
    assert.equal(forbidden.status, 403);
    assert.deepEqual(forbidden.json, { error: 'control_disabled' });
  });
});

test('the map route is GET only, passes its two headers and serves the bundle bytes or a 304', async () => {
  await withServer(async (base) => {
    maps.length = 0;
    const bundle = await call(base, `${MOWERS_PATH}/abc/map`, { token: TOKEN, headers: { 'x-eufy-map-mode': 'stream' } });
    assert.equal(bundle.status, 200);
    assert.deepEqual(bundle.raw, BUNDLE);
    assert.equal(bundle.headers['content-type'], 'application/vnd.eufy-robomow-map+zip');
    assert.equal(bundle.headers['content-length'], String(BUNDLE.length));
    assert.equal(bundle.headers['cache-control'], 'no-store');
    assert.equal(bundle.headers.etag, '"tag"');
    assert.equal(bundle.headers['x-eufy-map-age-ms'], '12');
    const unchanged = await call(base, `${MOWERS_PATH}/abc/map`, { token: TOKEN, headers: { 'if-none-match': '"tag"' } });
    assert.equal(unchanged.status, 304);
    assert.equal(unchanged.raw.length, 0);
    assert.equal(unchanged.headers.etag, '"tag"');
    assert.equal(unchanged.headers['cache-control'], 'no-store');
    assert.deepEqual(maps, [
      { id: 'abc', request: { mode: 'stream', ifNoneMatch: undefined } },
      { id: 'abc', request: { mode: undefined, ifNoneMatch: '"tag"' } },
    ]);
    const unconfigured = await call(base, `${MOWERS_PATH}/unconfigured/map`, { token: TOKEN });
    assert.equal(unconfigured.status, 404);
    assert.deepEqual(unconfigured.json, { error: 'map_unconfigured' });
    for (const method of ['POST', 'PUT', 'DELETE']) {
      const refused = await call(base, `${MOWERS_PATH}/abc/map`, { token: TOKEN, method });
      assert.equal(refused.status, 405, method);
      assert.equal(refused.headers.allow, 'GET');
    }
    for (const path of ['/v1/mowers/abc/map/', '/v1/mowers/abc/map/extra', '/v1/mowers/abc/maps', '/v1/map', '/v1/mowers/map']) {
      assert.equal((await call(base, path, { token: TOKEN })).status, 404, path);
    }
    assert.equal((await call(base, `${MOWERS_PATH}/abc/map`)).status, 401, 'the bundle needs the token');
    assert.equal(maps.length, 3, 'refused requests never reach the map');
  });
});

test('the settings route is POST only, reads the value from the query and ignores the body', async () => {
  await withServer(async (base) => {
    settings.length = 0;
    const changed = await call(base, `${MOWERS_PATH}/abc/settings/mow_height?value=45`, { token: TOKEN, method: 'POST', body: '{"value":75}' });
    assert.equal(changed.status, 200);
    assert.deepEqual(changed.json, { contract: 1, id: 'abc', setting: 'mow_height' });
    const missing = await call(base, `${MOWERS_PATH}/abc/settings/smart_no_go_zones`, { token: TOKEN, method: 'POST', body: 'value=true' });
    assert.equal(missing.status, 200, 'the route decides about a missing value');
    const forbidden = await call(base, `${MOWERS_PATH}/observed/settings/volume?value=30`, { token: TOKEN, method: 'POST' });
    assert.equal(forbidden.status, 403);
    assert.deepEqual(forbidden.json, { error: 'settings_disabled' });
    assert.deepEqual(settings, [
      { id: 'abc', key: 'mow_height', value: '45' },
      { id: 'abc', key: 'smart_no_go_zones', value: null },
      { id: 'observed', key: 'volume', value: '30' },
    ]);
    for (const method of ['GET', 'PUT', 'DELETE']) {
      const refused = await call(base, `${MOWERS_PATH}/abc/settings/mow_height?value=45`, { token: TOKEN, method });
      assert.equal(refused.status, 405, method);
      assert.equal(refused.headers.allow, 'POST');
    }
    for (const path of ['/v1/mowers/abc/settings', '/v1/mowers/abc/settings/', '/v1/mowers/abc/settings/MOW_HEIGHT', '/v1/mowers/abc/settings/mow-height', '/v1/mowers/abc/settings/mow_height/45', '/v1/mowers/abc/setting/mow_height']) {
      const missingRoute = await call(base, path, { token: TOKEN, method: 'POST' });
      assert.equal(missingRoute.status, 404, path);
      assert.deepEqual(missingRoute.json, { error: 'not_found' });
    }
    assert.equal((await call(base, `${MOWERS_PATH}/abc/settings/mow_height?value=45`, { method: 'POST' })).status, 401, 'the route needs the token');
    assert.equal(settings.length, 3, 'refused requests never reach the route');
  });
});
