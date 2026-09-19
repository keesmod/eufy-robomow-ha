import assert from 'node:assert/strict';
import { once } from 'node:events';
import { test } from 'node:test';
import { ApiError } from '../src/errors.ts';
import { MOWERS_PATH, STATE_PATH, createPrivateServer, type PrivateApi } from '../src/server.ts';
import { TOKEN, baselineHandles, call, settledHandles } from './helpers.ts';

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

test('the read-only routes are served without caching and refuse other methods and paths', async () => {
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
    for (const path of ['/v1/state/', '/v1/mowers/', '/v1/mowers/abc', '/v1/mowers/abc/state/extra', '/v1/mowers/a/b/state', '/v1/login', '/v1/cameras', '/health', '/']) {
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
  });
});
