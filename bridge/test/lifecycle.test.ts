import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { createServer } from 'node:net';
import { once } from 'node:events';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { EufyError, type AuthAnswer, type AuthState, type MowerAdapter, type MowerAdapterContext, type MowerSession } from '@keesmod/eufy-mega-client';
import { MowerBridge, type BridgeState } from '../src/bridge.ts';
import { STATE_PATH } from '../src/server.ts';
import { IDENTITY_FILE, SESSION_FILE } from '../src/storage.ts';
import { TOKEN, assertNoSecrets, baselineHandles, call, settledHandles, temporaryDirectory, testConfig } from './helpers.ts';

/** Every bridge is stopped when its test ends, so a failed assertion never leaves a listener behind. */
function owned(t: TestContext, ...parameters: ConstructorParameters<typeof MowerBridge>): MowerBridge {
  const bridge = new MowerBridge(...parameters);
  t.after(() => bridge.stop().catch(() => {}));
  return bridge;
}

function base(bridge: MowerBridge): string {
  const address = bridge.address;
  assert.ok(address, 'bridge is listening');
  return `http://127.0.0.1:${address.port}`;
}

async function exists(path: string): Promise<boolean> {
  return stat(path).then(
    () => true,
    () => false,
  );
}

async function servedState(bridge: MowerBridge): Promise<BridgeState> {
  const reply = await call(base(bridge), STATE_PATH, { token: TOKEN });
  assert.equal(reply.status, 200);
  assertNoSecrets(reply.text);
  return reply.json as BridgeState;
}

/** Adapter that authenticates without any network and uses the bridge's session store like the library would. */
type Behaviour = 'connect' | 'wait' | 'reject' | 'hang-shutdown' | 'fail-shutdown';

class SyntheticAdapter implements MowerAdapter {
  connected = false;
  readonly loaded: (MowerSession | undefined)[] = [];
  shutdowns = 0;
  readonly #context: MowerAdapterContext;
  readonly #behaviour: Behaviour;

  constructor(context: MowerAdapterContext, behaviour: Behaviour) {
    this.#context = context;
    this.#behaviour = behaviour;
  }

  async connect(_answer: AuthAnswer | undefined, signal: AbortSignal): Promise<AuthState> {
    if (this.#behaviour === 'reject') throw new Error('upstream detail that must not leak');
    if (this.#behaviour === 'wait')
      return new Promise((_, reject) => signal.addEventListener('abort', () => reject(new EufyError('request_aborted')), { once: true }));
    this.loaded.push(await this.#context.sessionStore.load());
    await this.#context.sessionStore.save({ version: 1, data: 'SYNTHETIC-OPAQUE' });
    this.connected = true;
    return { state: 'connected' };
  }

  async shutdown(): Promise<void> {
    this.shutdowns += 1;
    this.connected = false;
    if (this.#behaviour === 'hang-shutdown') return new Promise(() => {});
    if (this.#behaviour === 'fail-shutdown') throw new Error('flush failed');
  }
}

test('idle startup and shutdown keep observe_only, touch no cloud and leave no transport handles', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const handles = await baselineHandles();
  let adapters = 0;
  const bridge = owned(t, testConfig(directory.path), {
    adapter: () => {
      adapters += 1;
      throw new Error('the adapter must not be created without an explicit connect');
    },
  });
  assert.equal(bridge.lifecycle, 'created');
  await bridge.start();
  assert.equal(bridge.lifecycle, 'running');
  const state = await servedState(bridge);
  assert.equal(state.protocol, 1);
  assert.equal(state.bridge, 'eufy-robomow-bridge');
  assert.equal(state.operating_mode, 'observe_only');
  assert.equal(state.lifecycle, 'running');
  assert.deepEqual(state.auth, { state: 'disconnected', last_error: null, attempted_at: null });
  assert.deepEqual(state.client, { package: '@keesmod/eufy-mega-client', version: '0.20.0', module: 'mowers', lifecycle: 'open', connected: false });
  assert.deepEqual(state.mowers, { count: null, discovered_at: null, error: null });
  assert.deepEqual(state.routes, { discovery: true, state: true, control: false, maps: false, settings: false });
  assert.equal(state.maps, null, 'no map status without map provisioning');
  assert.match(String(state.bridge_id), /^[0-9a-f-]{36}$/);
  assert.equal(state.bridge_id, bridge.bridgeId);
  assert.equal((await call(base(bridge), STATE_PATH)).status, 401);
  const address = base(bridge);
  await bridge.stop();
  assert.equal(bridge.lifecycle, 'stopped');
  assert.equal(adapters, 0);
  assert.equal(await exists(join(directory.path, SESSION_FILE)), false, 'no session is written without authentication');
  assert.equal((await stat(directory.path)).mode & 0o777, 0o700);
  assert.equal((await stat(join(directory.path, IDENTITY_FILE))).mode & 0o777, 0o600);
  await assert.rejects(call(address, STATE_PATH, { token: TOKEN }), { code: 'ECONNREFUSED' });
  assert.equal(bridge.state().lifecycle, 'stopped');
  assert.equal(bridge.state().client.lifecycle, 'closed');
  await bridge.stop();
  await assert.rejects(bridge.start(), { code: 'bridge_not_restartable' });
  await assert.rejects(bridge.connect(), { code: 'bridge_not_running' });
  assert.deepEqual(await settledHandles(handles), handles);
});

test('failed cloud authentication records a stable code, keeps observe_only and leaves no handles', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const handles = await baselineHandles();
  const requests: string[] = [];
  const fetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    requests.push(new URL(String(input)).pathname);
    assert.equal(init?.redirect, 'error');
    return new Response(JSON.stringify({ msg: 'PRIVATE-REJECTION' }), { status: 401, headers: { 'content-type': 'application/json' } });
  };
  const bridge = owned(t, testConfig(directory.path), { fetch, now: () => Date.parse('2026-09-19T10:00:00Z') });
  await bridge.start();
  const result = await bridge.connect();
  assert.deepEqual(result, { state: 'disconnected', last_error: 'authentication_failed', attempted_at: '2026-09-19T10:00:00.000Z' });
  assert.deepEqual(requests, ['/v1/user/email/login'], 'exactly one login request and no retry');
  const state = await servedState(bridge);
  assert.equal(state.operating_mode, 'observe_only');
  assert.deepEqual(state.auth, result);
  assert.equal(state.client.connected, false);
  assert.equal(await exists(join(directory.path, SESSION_FILE)), false);
  await bridge.stop();
  assert.equal(bridge.lifecycle, 'stopped');
  assert.deepEqual(requests, ['/v1/user/email/login']);
  assert.deepEqual(await settledHandles(handles), handles);
});

test('adapter failures without a known code are reduced to mower_authentication_failed', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const bridge = owned(t, testConfig(directory.path), { adapter: (context) => new SyntheticAdapter(context, 'reject') });
  await bridge.start();
  const result = await bridge.connect();
  assert.equal(result.state, 'disconnected');
  assert.equal(result.last_error, 'mower_authentication_failed');
  assert.ok(!JSON.stringify(bridge.state()).includes('upstream detail'));
  await bridge.stop();
});

test('a connected session is stored privately in the mower data path and restored after a restart', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const handles = await baselineHandles();
  const adapters: SyntheticAdapter[] = [];
  const dependencies = {
    adapter: (context: MowerAdapterContext) => {
      const adapter = new SyntheticAdapter(context, 'connect');
      adapters.push(adapter);
      return adapter;
    },
  };
  const first = owned(t, testConfig(directory.path), dependencies);
  await first.start();
  const result = await first.connect();
  assert.equal(result.state, 'connected');
  assert.equal(result.last_error, null);
  const state = await servedState(first);
  assert.equal(state.auth.state, 'connected');
  assert.equal(state.client.connected, true);
  assert.equal(state.operating_mode, 'observe_only');
  assert.equal((await stat(join(directory.path, SESSION_FILE))).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(await readFile(join(directory.path, SESSION_FILE), 'utf8')), { version: 1, data: 'SYNTHETIC-OPAQUE' });
  assert.deepEqual(adapters[0]?.loaded, [undefined]);
  const identity = first.bridgeId;
  await first.stop();
  assert.equal(adapters[0]?.shutdowns, 1);
  assert.equal(first.state().auth.state, 'disconnected');
  const second = owned(t, testConfig(directory.path), dependencies);
  await second.start();
  assert.equal(second.bridgeId, identity, 'the identity survives restarts');
  await second.connect();
  assert.deepEqual(adapters[1]?.loaded, [{ version: 1, data: 'SYNTHETIC-OPAQUE' }], 'the saved session is offered to the next connect');
  await second.stop();
  assert.equal(adapters.length, 2, 'each bridge instance owns exactly one adapter');
  assert.deepEqual(await settledHandles(handles), handles);
});

test('authentication is bounded, cancelled by shutdown and never retried', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const handles = await baselineHandles();
  let adapter: SyntheticAdapter | undefined;
  const bridge = owned(t, testConfig(directory.path), {
    adapter: (context) => (adapter = new SyntheticAdapter(context, 'wait')),
    connectTimeoutMs: 50,
  });
  await bridge.start();
  const attempt = bridge.connect();
  assert.equal(bridge.connect(), attempt, 'a concurrent call joins the running attempt');
  const timedOut = await attempt;
  assert.equal(timedOut.state, 'disconnected');
  assert.equal(timedOut.last_error, 'request_timeout');
  const pending = bridge.connect();
  const stopping = bridge.stop();
  const aborted = await pending;
  assert.equal(aborted.last_error, 'request_aborted');
  await stopping;
  assert.equal(bridge.lifecycle, 'stopped');
  assert.equal(adapter?.shutdowns, 1);
  assert.equal(bridge.state().auth.last_error, 'request_aborted');
  assert.deepEqual(await settledHandles(handles), handles);
});

test('a port in use fails startup cleanly with nothing left behind', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const blocker = createServer();
  t.after(() => new Promise((resolve) => blocker.close(() => resolve(undefined))));
  blocker.listen(0, '127.0.0.1');
  await once(blocker, 'listening');
  const address = blocker.address();
  assert.ok(address && typeof address === 'object');
  const handles = await baselineHandles();
  const bridge = owned(t, testConfig(directory.path, { port: address.port }));
  await assert.rejects(bridge.start(), { code: 'address_in_use' });
  assert.equal(bridge.lifecycle, 'stopped');
  assert.equal(bridge.address, null);
  await assert.rejects(bridge.connect(), { code: 'bridge_not_running' });
  assert.deepEqual(await settledHandles(handles), handles);
});

test('shutdown reports incomplete or overdue library cleanup while the server still closes', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const handles = await baselineHandles();
  const failing = owned(t, testConfig(directory.path), { adapter: (context) => new SyntheticAdapter(context, 'fail-shutdown') });
  await failing.start();
  await failing.connect();
  const failingAddress = base(failing);
  await assert.rejects(failing.stop(), { code: 'shutdown_incomplete' });
  assert.equal(failing.lifecycle, 'stopped');
  await assert.rejects(call(failingAddress, STATE_PATH, { token: TOKEN }), { code: 'ECONNREFUSED' });
  await assert.rejects(failing.stop(), { code: 'shutdown_incomplete' }, 'repeated stop reuses the same result');
  const hanging = owned(t, testConfig(join(directory.path, 'second')), {
    adapter: (context) => new SyntheticAdapter(context, 'hang-shutdown'),
    shutdownTimeoutMs: 50,
  });
  await hanging.start();
  await hanging.connect();
  const hangingAddress = base(hanging);
  await assert.rejects(hanging.stop(), { code: 'shutdown_timeout' });
  assert.equal(hanging.lifecycle, 'stopped');
  await assert.rejects(call(hangingAddress, STATE_PATH, { token: TOKEN }), { code: 'ECONNREFUSED' });
  assert.deepEqual(await settledHandles(handles), handles);
});
