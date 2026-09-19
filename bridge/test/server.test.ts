import assert from 'node:assert/strict';
import { once } from 'node:events';
import { test } from 'node:test';
import { STATE_PATH, createPrivateServer } from '../src/server.ts';
import { TOKEN, call, settledHandles, transportHandles } from './helpers.ts';

async function withServer(run: (base: string, calls: () => number) => Promise<void>): Promise<void> {
  let calls = 0;
  const server = createPrivateServer(TOKEN, () => {
    calls += 1;
    return { protocol: 1, lifecycle: 'running' };
  });
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
  const handles = transportHandles();
  await withServer(async (base, calls) => {
    for (const authorization of [undefined, '', 'Bearer', `Bearer ${TOKEN.slice(0, -1)}`, `Bearer ${TOKEN.slice(0, -1)}X`, `Basic ${TOKEN}`, `bearer ${TOKEN}`, `Bearer  ${TOKEN}`]) {
      const reply = await call(base, STATE_PATH, authorization === undefined ? {} : { authorization });
      assert.equal(reply.status, 401, `authorization ${JSON.stringify(authorization)}`);
      assert.deepEqual(reply.json, { error: 'unauthorized' });
      assert.equal(reply.headers['www-authenticate'], 'Bearer');
    }
    assert.equal((await call(base, '/v1/other')).status, 401, 'unknown paths are not distinguishable without the token');
    assert.equal((await call(base, '/')).status, 401);
    assert.equal(calls(), 0, 'the state document is never built for unauthorized requests');
  });
  assert.deepEqual(await settledHandles(handles), handles);
});

test('the state document is the only route and is served read-only without caching', async () => {
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
    for (const method of ['POST', 'PUT', 'DELETE']) {
      const refused = await call(base, STATE_PATH, { token: TOKEN, method, body: '{"command":"start"}' });
      assert.equal(refused.status, 405, method);
      assert.equal(refused.headers.allow, 'GET');
      assert.deepEqual(refused.json, { error: 'method_not_allowed' });
    }
    for (const path of ['/v1/mowers', '/v1/state/', '/v1/login', '/v1/cameras', '/health', '/']) {
      const missing = await call(base, path, { token: TOKEN });
      assert.equal(missing.status, 404, path);
      assert.deepEqual(missing.json, { error: 'not_found' });
    }
    assert.equal(calls(), 2, 'refused requests never touch the state document');
  });
});
