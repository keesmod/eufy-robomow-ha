import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';
import {
  EufyError,
  type AuthAnswer,
  type AuthState,
  type MowerAdapter,
  type MowerDevice,
  type MowerLocalSession,
  type MowerLocalSessionEnd,
  type MowerLocalSessionOptions,
  type MowerTelemetry,
} from '@keesmod/eufy-mega-client';
import { MowerBridge, type BridgeDependencies, type DiscoveryDocument, type MowerStateDocument, type OpenLocalSession } from '../src/bridge.ts';
import type { BridgeConfig } from '../src/config.ts';
import { MOWERS_PATH, STATE_PATH } from '../src/server.ts';
import { TOKEN, assertNoSecrets, baselineHandles, call, settledHandles, temporaryDirectory, testConfig, type Reply } from './helpers.ts';

const ID_A = 'a'.repeat(64);
const ID_B = 'b'.repeat(64);
const HOST_A = '192.0.2.10';
const HOST_B = 'mower-b.lan';
const T0 = Date.parse('2026-09-19T12:00:00Z');

function device(id: string): MowerDevice {
  return { id, kind: 'mower', model: 'E15', productCode: 'T2880' };
}

/** Authenticates and discovers without any network. It has no private binding, like any custom adapter. */
class DiscoveringAdapter implements MowerAdapter {
  connected = false;
  discoveries = 0;
  devices: MowerDevice[] = [device(ID_A)];
  failWith: string | null = null;
  waitForAbort = false;

  async connect(_answer: AuthAnswer | undefined, _signal: AbortSignal): Promise<AuthState> {
    this.connected = true;
    return { state: 'connected' };
  }

  async shutdown(): Promise<void> {
    this.connected = false;
  }

  async discover(signal: AbortSignal): Promise<MowerDevice[]> {
    this.discoveries += 1;
    if (this.waitForAbort)
      await new Promise<never>((_, reject) => signal.addEventListener('abort', () => reject(new EufyError('request_aborted')), { once: true }));
    if (this.failWith) throw new EufyError(this.failWith);
    return this.devices.map((entry) => ({ ...entry }));
  }
}

function telemetry(observedAt: string, battery = 85): MowerTelemetry {
  return {
    source: 'local-tuya-3.5',
    observedAt,
    status: { state: 'unconfirmed', level: 'observed' },
    battery: { state: 'reported', value: { percent: battery }, dp: ['8'], source: 'local-tuya-3.5', observedAt },
    progress: { state: 'unconfirmed' },
    network: { state: 'reported', value: { kind: 'wifi', signalPercent: 70 }, dp: ['134', '109'], source: 'local-tuya-3.5', observedAt },
    fields: {
      '8': { id: '8', value: battery, declared: true, code: 'battery_percentage', type: 'value', valid: true, unit: '%' },
      '155': { id: '155', value: 'PRIVATE-BLOB', declared: true, code: 'PRIVATE-CODE', type: 'raw' },
    },
    dps: { '8': battery, '134': 'Wifi', '109': 70, '155': 'PRIVATE-BLOB' },
  };
}

interface SessionLog {
  opened: { id: string; options: MowerLocalSessionOptions }[];
  queries: number;
  disconnects: number;
  lastConnected: () => boolean;
}

/** Synthetic LAN session. `answer` decides what one query returns or throws. */
function sessions(log: SessionLog, answer: (signal: AbortSignal) => Promise<MowerTelemetry>, openFailure?: () => string | null): OpenLocalSession {
  return async (id, options, signal) => {
    const failure = openFailure?.();
    if (failure) throw new EufyError(failure);
    log.opened.push({ id, options });
    let connected = true;
    let end!: (value: MowerLocalSessionEnd) => void;
    const closed = new Promise<MowerLocalSessionEnd>((resolve) => {
      end = resolve;
    });
    log.lastConnected = () => connected;
    const session: MowerLocalSession = {
      get connected() {
        return connected;
      },
      closed,
      schema: undefined,
      queryStatus: async () => {
        throw new Error('not used by the bridge');
      },
      queryTelemetry: async (querySignal) => {
        log.queries += 1;
        return answer(querySignal ?? signal);
      },
      receiveReport: async () => {
        throw new Error('not used by the bridge');
      },
      disconnect: async () => {
        if (!connected) return;
        connected = false;
        log.disconnects += 1;
        end('disconnected');
      },
    };
    return session;
  };
}

function sessionLog(): SessionLog {
  return { opened: [], queries: 0, disconnects: 0, lastConnected: () => false };
}

interface Fixture {
  bridge: MowerBridge;
  adapter: DiscoveringAdapter;
  clock: { now: number };
  base: string;
  get: (path: string) => Promise<Reply>;
}

async function fixture(t: TestContext, config: Partial<BridgeConfig> = {}, dependencies: Omit<BridgeDependencies, 'adapter' | 'now'> = {}): Promise<Fixture> {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const adapter = new DiscoveringAdapter();
  const clock = { now: T0 };
  const bridge = new MowerBridge(testConfig(directory.path, config), {
    adapter: () => adapter,
    now: () => clock.now,
    discoveryCacheMs: 5_000,
    discoveryIntervalMs: 1_000,
    ...dependencies,
  });
  t.after(() => bridge.stop().catch(() => {}));
  await bridge.start();
  const address = bridge.address;
  assert.ok(address);
  const base = `http://127.0.0.1:${address.port}`;
  return { bridge, adapter, clock, base, get: (path) => call(base, path, { token: TOKEN }) };
}

test('discovery needs a connected module, is cached, spaced and keeps an older list after a failed refresh', async (t) => {
  const f = await fixture(t);
  const refused = await f.get(MOWERS_PATH);
  assert.equal(refused.status, 503);
  assert.deepEqual(refused.json, { error: 'authentication_required' });
  assert.equal(f.adapter.discoveries, 0, 'the library refuses discovery before authentication');
  assert.deepEqual((await f.get(STATE_PATH)).json && ((await f.get(STATE_PATH)).json as { mowers: unknown }).mowers, { count: null, discovered_at: null, error: 'authentication_required' });
  f.clock.now += 100;
  assert.equal((await f.bridge.connect()).state, 'connected');
  const first = await f.get(MOWERS_PATH);
  assert.equal(first.status, 200, 'a fresh session discovers at once, the failed attempt does not delay it');
  assertNoSecrets(first.text);
  assert.deepEqual(first.json, {
    contract: 1,
    discovered_at: new Date(f.clock.now).toISOString(),
    fresh: true,
    error: null,
    mowers: [{ id: ID_A, kind: 'mower', model: 'E15', productCode: 'T2880', state_available: false }],
  });
  assert.equal(f.adapter.discoveries, 1);
  f.clock.now += 4_000;
  assert.equal((await f.get(MOWERS_PATH)).status, 200);
  assert.equal(f.adapter.discoveries, 1, 'a list younger than the cache age is served without a request to the cloud');
  f.clock.now += 1_000;
  assert.equal((await f.get(MOWERS_PATH)).status, 200);
  assert.equal(f.adapter.discoveries, 2, 'an older list is refreshed once');
  f.adapter.failWith = 'mower_request_failed';
  f.clock.now += 5_000;
  const stale = await f.get(MOWERS_PATH);
  assert.equal(stale.status, 200);
  const staleDocument = stale.json as DiscoveryDocument;
  assert.equal(staleDocument.fresh, false);
  assert.equal(staleDocument.error, 'mower_request_failed');
  assert.equal(staleDocument.mowers.length, 1, 'the older list stays available');
  assert.equal(f.adapter.discoveries, 3);
  f.clock.now += 500;
  await f.get(MOWERS_PATH);
  assert.equal(f.adapter.discoveries, 3, 'attempts are spaced by the interval');
  f.clock.now += 500;
  await f.get(MOWERS_PATH);
  assert.equal(f.adapter.discoveries, 4);
  const state = (await f.get(STATE_PATH)).json as { mowers: unknown; routes: unknown };
  assert.deepEqual(state.mowers, { count: 1, discovered_at: new Date(T0 + 5_100).toISOString(), error: 'mower_request_failed' });
  assert.deepEqual(state.routes, { discovery: true, state: true, control: false, maps: false });
  f.adapter.failWith = null;
  f.clock.now += 1_000;
  assert.equal(((await f.get(MOWERS_PATH)).json as DiscoveryDocument).fresh, true);
});

test('a discovery that reports no list at all is a 503 with the failure code and a later timeout is bounded', async (t) => {
  const f = await fixture(t, {}, { discoveryTimeoutMs: 50 });
  await f.bridge.connect();
  f.adapter.failWith = 'mower_region_unsupported';
  const failed = await f.get(MOWERS_PATH);
  assert.equal(failed.status, 503);
  assert.deepEqual(failed.json, { error: 'mower_region_unsupported' });
  f.adapter.failWith = null;
  f.adapter.waitForAbort = true;
  f.clock.now += 1_000;
  const timedOut = await f.get(MOWERS_PATH);
  assert.equal(timedOut.status, 503);
  assert.deepEqual(timedOut.json, { error: 'request_timeout' });
});

test('the state route validates the id, needs discovery and a configured host for that id', async (t) => {
  const log = sessionLog();
  const f = await fixture(t, {}, { openLocalSession: sessions(log, async () => telemetry(new Date(T0).toISOString())) });
  await f.bridge.connect();
  const invalid = await f.get(`${MOWERS_PATH}/not-a-mower-id/state`);
  assert.equal(invalid.status, 400);
  assert.deepEqual(invalid.json, { error: 'invalid_mower_id' });
  const unknown = await f.get(`${MOWERS_PATH}/${ID_B}/state`);
  assert.equal(unknown.status, 404);
  assert.deepEqual(unknown.json, { error: 'unknown_mower' });
  assert.equal(f.adapter.discoveries, 1, 'the state request ran the first discovery itself');
  const unconfigured = await f.get(`${MOWERS_PATH}/${ID_A}/state`);
  assert.equal(unconfigured.status, 503);
  assert.deepEqual(unconfigured.json, { error: 'mower_host_unconfigured' });
  assert.equal(log.opened.length, 0, 'no LAN session is opened without a host');
});

test('the single host applies only to a sole mower while the hosts map is exact', async (t) => {
  const log = sessionLog();
  const f = await fixture(t, { host: HOST_A, hosts: { [ID_B]: HOST_B } }, { openLocalSession: sessions(log, async () => telemetry(new Date(T0).toISOString())) });
  f.adapter.devices = [device(ID_A), device(ID_B)];
  await f.bridge.connect();
  const list = (await f.get(MOWERS_PATH)).json as DiscoveryDocument;
  assert.deepEqual(
    list.mowers.map((mower) => [mower.id, mower.state_available]),
    [
      [ID_A, false],
      [ID_B, true],
    ],
  );
  assert.deepEqual((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).json, { error: 'mower_host_unconfigured' });
  assert.equal((await f.get(`${MOWERS_PATH}/${ID_B}/state`)).status, 200);
  assert.deepEqual(log.opened, [{ id: ID_B, options: { host: HOST_B, timeoutMs: 5_000 } }]);
});

test('the state route serves the four typed fields with freshness, closes the session and joins concurrent queries', async (t) => {
  const handles = await baselineHandles();
  const log = sessionLog();
  const observedAt = new Date(T0 - 1_500).toISOString();
  const f = await fixture(t, { host: HOST_A, localTimeoutMs: 7_000 }, {
    openLocalSession: sessions(log, async () => {
      // A real query takes time on the LAN, so the second request arrives while the first is in flight.
      await new Promise((resolve) => setTimeout(resolve, 40));
      return telemetry(observedAt);
    }),
  });
  await f.bridge.connect();
  const [one, two] = await Promise.all([f.get(`${MOWERS_PATH}/${ID_A}/state`), f.get(`${MOWERS_PATH}/${ID_A}/state`)]);
  assert.equal(one.status, 200);
  assert.equal(two.status, 200);
  assert.deepEqual(one.json, two.json);
  assert.equal(log.opened.length, 1, 'concurrent requests share one LAN session');
  assert.equal(log.queries, 1);
  assert.equal(log.disconnects, 1);
  assert.equal(log.lastConnected(), false, 'the session is closed after the query');
  assert.deepEqual(log.opened[0], { id: ID_A, options: { host: HOST_A, timeoutMs: 7_000 } });
  assertNoSecrets(one.text);
  assert.ok(!one.text.includes(HOST_A), 'the LAN host is never served');
  assert.ok(!one.text.includes('"dps"') && !one.text.includes('"fields"') && !one.text.includes('155'), 'raw data points are absent');
  assert.deepEqual(one.json, {
    contract: 1,
    id: ID_A,
    source: 'local-tuya-3.5',
    age_ms: 1_500,
    stale: false,
    error: null,
    observed_at: observedAt,
    status: { state: 'unconfirmed', level: 'observed' },
    battery: { state: 'reported', value: { percent: 85 }, dp: ['8'], source: 'local-tuya-3.5', observedAt },
    progress: { state: 'unconfirmed' },
    network: { state: 'reported', value: { kind: 'wifi', signalPercent: 70 }, dp: ['134', '109'], source: 'local-tuya-3.5', observedAt },
  });
  const list = (await f.get(MOWERS_PATH)).json as DiscoveryDocument;
  assert.equal(list.mowers[0]?.state_available, true);
  const third = await f.get(`${MOWERS_PATH}/${ID_A}/state`);
  assert.equal(log.opened.length, 2, 'every later request opens its own bounded session');
  assert.equal((third.json as MowerStateDocument).stale, false);
  await f.bridge.stop();
  assert.deepEqual(await settledHandles(handles), handles);
});

test('a failed query serves the last good result as stale with the failure code, otherwise a 503', async (t) => {
  const log = sessionLog();
  let openFailure: string | null = null;
  let queryFailure: Error | null = null;
  const observedAt = new Date(T0).toISOString();
  const f = await fixture(
    t,
    { hosts: { [ID_A]: HOST_A, [ID_B]: HOST_B } },
    {
      openLocalSession: sessions(
        log,
        async () => {
          if (queryFailure) throw queryFailure;
          return telemetry(observedAt);
        },
        () => openFailure,
      ),
    },
  );
  f.adapter.devices = [device(ID_A), device(ID_B)];
  await f.bridge.connect();
  const good = (await f.get(`${MOWERS_PATH}/${ID_A}/state`)).json as MowerStateDocument;
  assert.equal(good.stale, false);
  openFailure = 'mower_local_unreachable';
  f.clock.now += 30_000;
  const stale = await f.get(`${MOWERS_PATH}/${ID_A}/state`);
  assert.equal(stale.status, 200);
  const staleDocument = stale.json as MowerStateDocument;
  assert.equal(staleDocument.stale, true);
  assert.equal(staleDocument.error, 'mower_local_unreachable');
  assert.equal(staleDocument.age_ms, 30_000);
  assert.deepEqual(staleDocument.battery, good.battery);
  assert.equal(staleDocument.observed_at, observedAt, 'the stale document keeps the original observation time');
  const never = await f.get(`${MOWERS_PATH}/${ID_B}/state`);
  assert.equal(never.status, 503);
  assert.deepEqual(never.json, { error: 'mower_local_unreachable' });
  openFailure = null;
  queryFailure = new EufyError('mower_local_protocol_error');
  const opened = log.opened.length;
  const broken = await f.get(`${MOWERS_PATH}/${ID_B}/state`);
  assert.equal(broken.status, 503);
  assert.deepEqual(broken.json, { error: 'mower_local_protocol_error' });
  assert.equal(log.opened.length, opened + 1);
  assert.equal(log.disconnects, opened + 1, 'a failed query still closes its session');
  queryFailure = new TypeError('unexpected detail');
  const unexpected = await f.get(`${MOWERS_PATH}/${ID_B}/state`);
  assert.equal(unexpected.status, 503);
  assert.deepEqual(unexpected.json, { error: 'internal_error' });
  assert.ok(!unexpected.text.includes('unexpected detail'));
});

test('an adapter without the private binding capability reports mower_protocol_unavailable through the library', async (t) => {
  const f = await fixture(t, { host: HOST_A });
  await f.bridge.connect();
  const reply = await f.get(`${MOWERS_PATH}/${ID_A}/state`);
  assert.equal(reply.status, 503);
  assert.deepEqual(reply.json, { error: 'mower_protocol_unavailable' });
});

test('shutdown aborts an in-flight query and discovery, answers them and leaves no handles', async (t) => {
  const handles = await baselineHandles();
  const log = sessionLog();
  const f = await fixture(t, { host: HOST_A }, {
    openLocalSession: sessions(log, (signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new EufyError('request_aborted')), { once: true }))),
  });
  await f.bridge.connect();
  await f.get(MOWERS_PATH);
  const pending = f.get(`${MOWERS_PATH}/${ID_A}/state`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(log.opened.length, 1);
  f.adapter.waitForAbort = true;
  f.clock.now += 10_000;
  const discovering = f.get(MOWERS_PATH);
  await new Promise((resolve) => setTimeout(resolve, 20));
  await f.bridge.stop();
  const aborted = await pending;
  assert.equal(aborted.status, 503);
  assert.deepEqual(aborted.json, { error: 'request_aborted' });
  assert.equal(log.disconnects, 1, 'the aborted session is closed');
  const list = await discovering;
  assert.equal(list.status, 200, 'the older list is still served');
  assert.equal((list.json as DiscoveryDocument).error, 'request_aborted');
  await assert.rejects(f.bridge.discover(), { code: 'bridge_not_running' });
  await assert.rejects(f.bridge.mowerState(ID_A), { code: 'bridge_not_running' });
  assert.deepEqual(await settledHandles(handles), handles);
});
