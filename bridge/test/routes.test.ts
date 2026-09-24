import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';
import {
  E15_TELEMETRY_DEFINITIONS,
  EufyError,
  decodeMowerTelemetry,
  type AuthAnswer,
  type AuthState,
  type MowerAdapter,
  type MowerCommandEnd,
  type MowerCommandKind,
  type MowerCommandOutcome,
  type MowerCommandRequest,
  type MowerDevice,
  type MowerLocalSession,
  type MowerLocalSessionEnd,
  type MowerLocalSessionOptions,
  type MowerTelemetry,
} from '@keesmod/eufy-mega-client';
import { MowerBridge, type BridgeDependencies, type BridgeState, type DiscoveryDocument, type MowerCommandDocument, type MowerStateDocument, type OpenLocalSession } from '../src/bridge.ts';
import type { BridgeConfig, ControlConfig } from '../src/config.ts';
import { MOWERS_PATH, STATE_PATH } from '../src/server.ts';
import { TOKEN, assertNoSecrets, baselineHandles, call, settledHandles, temporaryDirectory, testConfig, type Reply } from './helpers.ts';

const ID_A = 'a'.repeat(64);
const ID_B = 'b'.repeat(64);
const HOST_A = '192.0.2.10';
const HOST_B = 'mower-b.lan';
const T0 = Date.parse('2026-09-19T12:00:00Z');
/** Synthetic opt-in. The stop route is the operator's words and is never served. */
const CONTROL: ControlConfig = { stopRoute: 'PRIVATE-STOP-ROUTE pause then the app', maxStateAgeMs: 30_000, readBackMs: 20_000 };
const CONTROL_MODE: Partial<BridgeConfig> = { operatingMode: 'control', control: CONTROL };

function device(id: string): MowerDevice {
  return { id, kind: 'mower', model: 'E15', productCode: 'T2880' };
}

/**
 * Authenticates and discovers without any network. It has no private binding, like any custom
 * adapter. Setting `connected` to false stands in for the end of the library's session reuse window.
 */
class DiscoveringAdapter implements MowerAdapter {
  connected = false;
  connects = 0;
  connectFailWith: string | null = null;
  discoveries = 0;
  devices: MowerDevice[] = [device(ID_A)];
  failWith: string | null = null;
  waitForAbort = false;

  async connect(_answer: AuthAnswer | undefined, _signal: AbortSignal): Promise<AuthState> {
    this.connects += 1;
    if (this.connectFailWith) throw new EufyError(this.connectFailWith);
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

/** The three confirmed DP 107 definitions name this data point, so the library lists it once per definition. */
const STATUS_DP = ['107', '107', '107'];

/**
 * One synthetic DP 107 payload: varint records derived from the library's confirmed field
 * definitions, not from any capture. Field numbers map to wire type 0 tags.
 */
function wirePayload(fields: Record<number, number>): string {
  const bytes: number[] = [];
  for (const [number, value] of Object.entries(fields)) bytes.push(Number(number) << 3, value);
  return Buffer.from(bytes).toString('base64');
}

/**
 * Real library decoding of a synthetic snapshot, so the route serves exactly what the pinned
 * release reports. `robotStatus` is the DP 107 payload, absent by default. DP 155 stands in for
 * a private raw data point that must never reach a response.
 */
function telemetry(observedAt: string, battery = 85, robotStatus?: string | number): MowerTelemetry {
  const dps: Record<string, string | number> = { '8': battery, '134': 'Wifi', '109': 70, '155': 'PRIVATE-BLOB' };
  if (robotStatus !== undefined) dps['107'] = robotStatus;
  return decodeMowerTelemetry({ source: 'local-tuya-3.5', observedAt, dps });
}

interface SessionLog {
  opened: { id: string; options: MowerLocalSessionOptions }[];
  queries: number;
  commands: MowerCommandRequest[];
  disconnects: number;
  lastConnected: () => boolean;
}

type CommandAnswer = (request: MowerCommandRequest, signal: AbortSignal) => Promise<MowerCommandOutcome>;

/**
 * Synthetic command outcome in the library's shape. The `before` snapshot and the reports carry a
 * private raw data point that must never reach a response.
 */
function outcome(kind: MowerCommandKind, end: MowerCommandEnd, at: string): MowerCommandOutcome {
  const write =
    kind === 'start' || kind === 'stop'
      ? { dp: '1', code: 'switch_go', value: kind === 'start' }
      : { dp: '2', code: 'pause', value: kind === 'pause' };
  const snapshot = { source: 'local-tuya-3.5' as const, observedAt: at, dps: { '1': false, '118': 100, '155': 'PRIVATE-BLOB' } };
  const base = { command: kind, write, before: snapshot, sentAt: at, reply: { observedAt: at, returnCodeZero: true, rejected: false } };
  if (end === 'reflected' && kind === 'stop') {
    // The library reflects a stop through the map-saving DP 107 payload, not through an activity.
    return {
      ...base,
      stage: 'reflected',
      end,
      acknowledgement: { observedAt: at, sequence: 11, dp: write.dp },
      payload: { observedAt: at, sequence: 12, name: 'map_saving' },
      reports: [
        { ...snapshot, kind: 'device-report', sequence: 11, dps: { [write.dp]: write.value } },
        { ...snapshot, kind: 'device-report', sequence: 12, dps: { '107': 'PRIVATE-BLOB' } },
      ],
    };
  }
  if (end === 'reflected') {
    const activity = kind === 'pause' ? ('paused' as const) : ('mowing' as const);
    return {
      ...base,
      stage: 'reflected',
      end,
      acknowledgement: { observedAt: at, sequence: 11, dp: write.dp },
      activity: { observedAt: at, sequence: 12, value: activity },
      reports: [
        { ...snapshot, kind: 'device-report', sequence: 11, dps: { [write.dp]: write.value } },
        { ...snapshot, kind: 'device-report', sequence: 12, dps: { '107': 'PRIVATE-BLOB' } },
      ],
    };
  }
  if (end === 'rejected') return { ...base, stage: 'sent', end, reply: { observedAt: at, returnCodeZero: false, rejected: true }, reports: [] };
  return { ...base, stage: 'sent', end, reports: [] };
}

/** Synthetic LAN session. `answer` decides what one query returns or throws, `command` what one command resolves. */
function sessions(log: SessionLog, answer: (signal: AbortSignal) => Promise<MowerTelemetry>, openFailure?: () => string | null, command?: CommandAnswer): OpenLocalSession {
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
      commandsEnabled: command !== undefined,
      schema: undefined,
      queryStatus: async () => {
        throw new Error('not used by the bridge');
      },
      sendCommand: async (request, commandSignal) => {
        log.commands.push({ ...request });
        if (!command) throw new EufyError('mower_commands_disabled');
        return command(request, commandSignal ?? signal);
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
  return { opened: [], queries: 0, commands: [], disconnects: 0, lastConnected: () => false };
}

interface Fixture {
  bridge: MowerBridge;
  adapter: DiscoveringAdapter;
  clock: { now: number };
  base: string;
  get: (path: string) => Promise<Reply>;
  post: (path: string) => Promise<Reply>;
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
  return { bridge, adapter, clock, base, get: (path) => call(base, path, { token: TOKEN }), post: (path) => call(base, path, { token: TOKEN, method: 'POST' }) };
}

const commandPath = (id: string, kind: string) => `${MOWERS_PATH}/${id}/commands/${kind}`;

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
    status: { state: 'missing', dp: STATUS_DP },
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

test('the pinned release confirms exactly the three DP 107 activities and no other status definition', () => {
  const status = E15_TELEMETRY_DEFINITIONS.filter((definition) => definition.field === 'status');
  assert.deepEqual(
    status.map((definition) => [definition.dp, definition.level, definition.decode.kind === 'wire' ? definition.decode.match : null, definition.decode.kind === 'wire' ? definition.decode.activity : null]),
    [
      ['107', 'confirmed', { 1: 2, 3: 1 }, 'mowing'],
      ['107', 'confirmed', { 1: 2, 3: 2 }, 'paused'],
      ['107', 'confirmed', { 1: 1, 3: 1 }, 'returning'],
    ],
  );
  assert.ok(E15_TELEMETRY_DEFINITIONS.every((definition) => definition.field !== 'progress'), 'mowing progress stays unconfirmed');
});

test('the state route serves the confirmed activity with its observation time and keeps missing and invalid explicit', async (t) => {
  const log = sessionLog();
  const observedAt = new Date(T0 - 250).toISOString();
  let robotStatus: string | number | undefined;
  const f = await fixture(t, { host: HOST_A }, { openLocalSession: sessions(log, async () => telemetry(observedAt, 85, robotStatus)) });
  await f.bridge.connect();
  const status = async () => {
    const reply = await f.get(`${MOWERS_PATH}/${ID_A}/state`);
    assert.equal(reply.status, 200);
    assertNoSecrets(reply.text);
    assert.ok(!reply.text.includes('"dps"') && !reply.text.includes('"fields"') && !reply.text.includes('PRIVATE-BLOB'), 'raw data points are absent');
    if (typeof robotStatus === 'string' && robotStatus) assert.ok(!reply.text.includes(robotStatus), 'the raw DP 107 payload is never served');
    const document = reply.json as MowerStateDocument;
    assert.equal(document.stale, false);
    assert.equal(document.error, null);
    assert.equal(document.observed_at, observedAt);
    assert.equal(document.age_ms, 250);
    return document.status;
  };
  // Confirmed payloads: fields 1 and 3 as recorded in the library's E15 registry.
  for (const [fields, activity] of [
    [{ 1: 2, 3: 1 }, 'mowing'],
    [{ 1: 2, 3: 2 }, 'paused'],
    [{ 1: 1, 3: 1 }, 'returning'],
  ] as const) {
    robotStatus = wirePayload(fields);
    assert.deepEqual(await status(), { state: 'reported', value: activity, dp: STATUS_DP, source: 'local-tuya-3.5', observedAt }, activity);
  }
  // Absent DP 107 is missing, never an activity.
  robotStatus = undefined;
  assert.deepEqual(await status(), { state: 'missing', dp: STATUS_DP });
  // Withheld shapes are invalid for that report: default, field 6, map-saving, transitional, malformed and wrongly typed.
  for (const [name, payload] of [
    ['default empty', ''],
    ['default zero byte', Buffer.from([0]).toString('base64')],
    ['field 6', wirePayload({ 6: 1 })],
    ['map saving', wirePayload({ 2: 5, 3: 1 })],
    ['transitional', wirePayload({ 1: 2 })],
    ['malformed', 'not base64!'],
    ['not a string', 7],
  ] as const) {
    robotStatus = payload;
    assert.deepEqual(await status(), { state: 'invalid', dp: STATUS_DP }, name);
  }
  assert.equal(log.opened.length, 11, 'every request ran its own bounded query');
  // A later failure serves the last activity as stale with its original observation time. Age never changes it.
  robotStatus = wirePayload({ 1: 2, 3: 1 });
  await status();
  robotStatus = 'not base64!';
  f.clock.now += 3_600_000;
  const aged = (await f.get(`${MOWERS_PATH}/${ID_A}/state`)).json as MowerStateDocument;
  assert.deepEqual(aged.status, { state: 'invalid', dp: STATUS_DP }, 'a fresh invalid report replaces the earlier activity');
  assert.equal(aged.stale, false);
  assert.equal(aged.age_ms, 3_600_250);
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

/** LAN sessions that need the library's cloud session, like `openLocalSession` does. */
function sessionsNeedingCloud(log: SessionLog, f: () => Fixture, command?: CommandAnswer): OpenLocalSession {
  return sessions(
    log,
    async () => telemetry(new Date(f().clock.now).toISOString()),
    () => (f().adapter.connected ? null : 'authentication_required'),
    command,
  );
}

test('a lapsed cloud session is renewed by the next route, which discovers again before its LAN session', async (t) => {
  const log = sessionLog();
  const lines: string[] = [];
  let f!: Fixture;
  f = await fixture(t, { host: HOST_A }, { openLocalSession: sessionsNeedingCloud(log, () => f), log: (level, message) => lines.push(`${level} ${message}`) });
  await f.bridge.connect();
  const statePath = `${MOWERS_PATH}/${ID_A}/state`;
  assert.equal(((await f.get(statePath)).json as MowerStateDocument).stale, false);
  assert.equal(f.adapter.connects, 1);
  assert.equal(f.adapter.discoveries, 1);
  // The library's reuse window ends: the module reports disconnected and holds no binding.
  f.adapter.connected = false;
  f.clock.now += 1_000;
  const lapsed = (await f.get(STATE_PATH)).json as BridgeState;
  assert.deepEqual(lapsed.auth, { state: 'disconnected', last_error: null, attempted_at: new Date(T0).toISOString() }, 'the state document follows the library, not the earlier attempt');
  assert.equal(lapsed.client.connected, false);
  const renewed = await f.get(statePath);
  assert.equal(renewed.status, 200);
  const document = renewed.json as MowerStateDocument;
  assert.equal(document.stale, false, 'the route renewed the session instead of failing');
  assert.equal(document.error, null);
  assert.equal(f.adapter.connects, 2, 'one renewal');
  assert.equal(f.adapter.discoveries, 2, 'discovery ran again for the new bindings although the list was younger than the cache age');
  assert.deepEqual(lines, ['info mower cloud session renewed']);
  const state = (await f.get(STATE_PATH)).json as BridgeState;
  assert.deepEqual(state.auth, { state: 'connected', last_error: null, attempted_at: new Date(T0 + 1_000).toISOString() });
  assertNoSecrets(JSON.stringify(state) + lines.join('\n'));
  f.clock.now += 1_000;
  assert.equal(((await f.get(statePath)).json as MowerStateDocument).stale, false);
  assert.equal(f.adapter.connects, 2, 'a live session is not renewed');
  assert.equal(f.adapter.discoveries, 2, 'fresh bindings are not discovered again');
});

test('a failed renewal is spaced by the re-authentication interval and a refused sign-in is never repeated', async (t) => {
  const log = sessionLog();
  const lines: string[] = [];
  let f!: Fixture;
  f = await fixture(t, { host: HOST_A }, { openLocalSession: sessionsNeedingCloud(log, () => f), reauthIntervalMs: 60_000, log: (level, message) => lines.push(`${level} ${message}`) });
  const statePath = `${MOWERS_PATH}/${ID_A}/state`;
  assert.equal((await f.get(statePath)).status, 503, 'before the explicit startup attempt no route signs in');
  assert.equal(f.adapter.connects, 0);
  await f.bridge.connect();
  await f.get(statePath);
  f.adapter.connected = false;
  f.adapter.connectFailWith = 'mower_request_failed';
  f.clock.now += 1_000;
  const failed = (await f.get(statePath)).json as MowerStateDocument;
  assert.equal(failed.stale, true, 'the last good result is served as stale');
  assert.equal(failed.error, 'authentication_required');
  assert.equal(f.adapter.connects, 2);
  assert.deepEqual(lines, ['warn mower cloud session renewal failed (mower_request_failed)']);
  f.clock.now += 59_000;
  await f.get(statePath);
  assert.equal(f.adapter.connects, 2, 'no attempt within the interval after a failure');
  assert.equal(log.opened.length, 1, 'no LAN session without a cloud session');
  f.adapter.connectFailWith = null;
  f.clock.now += 1_000;
  assert.equal(((await f.get(statePath)).json as MowerStateDocument).stale, false, 'the attempt after the interval renewed the session');
  assert.equal(f.adapter.connects, 3);
  // A refused sign-in needs the operator and is never repeated.
  f.adapter.connected = false;
  f.adapter.connectFailWith = 'authentication_failed';
  f.clock.now += 1_000;
  assert.equal(((await f.get(statePath)).json as MowerStateDocument).error, 'authentication_required');
  assert.equal(f.adapter.connects, 4);
  f.clock.now += 3_600_000;
  await f.get(statePath);
  await f.get(MOWERS_PATH);
  assert.equal(f.adapter.connects, 4, 'no automatic attempt after a refused sign-in');
  const state = (await f.get(STATE_PATH)).json as BridgeState;
  assert.equal(state.auth.state, 'disconnected');
  assert.equal(state.auth.last_error, 'authentication_failed');
});

test('a command after a lapse renews the session and discovers before its single write, and without a session nothing is written', async (t) => {
  const log = sessionLog();
  let f!: Fixture;
  f = await fixture(t, { host: HOST_A, ...CONTROL_MODE }, {
    openLocalSession: sessionsNeedingCloud(log, () => f, async (request) => outcome(request.kind, 'reflected', new Date(f.clock.now).toISOString())),
  });
  await f.bridge.connect();
  await f.get(`${MOWERS_PATH}/${ID_A}/state`);
  f.adapter.connected = false;
  f.clock.now += 1_000;
  const started = await f.post(commandPath(ID_A, 'start'));
  assert.equal(started.status, 200);
  assert.equal((started.json as MowerCommandDocument).result, 'confirmed');
  assert.equal(f.adapter.connects, 2, 'the command route renewed the session first');
  assert.equal(f.adapter.discoveries, 2, 'and restored the binding before its LAN session');
  assert.deepEqual(log.commands.map((request) => request.kind), ['start'], 'written once');
  f.adapter.connected = false;
  f.adapter.connectFailWith = 'authentication_failed';
  f.clock.now += 1_000;
  const refused = await f.post(commandPath(ID_A, 'pause'));
  assert.equal(refused.status, 503);
  assert.deepEqual(refused.json, { error: 'authentication_required' });
  assert.deepEqual(log.commands.map((request) => request.kind), ['start'], 'nothing is written without a session');
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

test('observe_only answers every command route with 403 before discovery, the library or a session', async (t) => {
  const log = sessionLog();
  const f = await fixture(t, { host: HOST_A }, { openLocalSession: sessions(log, async () => telemetry(new Date(T0).toISOString()), undefined, async (request) => outcome(request.kind, 'reflected', new Date(T0).toISOString())) });
  await f.bridge.connect();
  const state = (await f.get(STATE_PATH)).json as BridgeState;
  assert.equal(state.operating_mode, 'observe_only');
  assert.deepEqual(state.routes, { discovery: true, state: true, control: false, maps: false });
  assert.equal(state.control, null);
  for (const kind of ['start', 'pause', 'resume', 'stop', 'return', 'jump']) {
    const refused = await f.post(commandPath(ID_A, kind));
    assert.equal(refused.status, 403, kind);
    assert.deepEqual(refused.json, { error: 'control_disabled' });
  }
  assert.equal(f.adapter.discoveries, 0, 'no discovery ran for a refused command');
  assert.equal(log.opened.length, 0, 'no LAN session was opened');
  assert.equal(log.commands.length, 0);
  await assert.rejects(f.bridge.command(ID_A, 'start'), { code: 'control_disabled', status: 403 });
});

test('control mode routes start, pause and resume behind a fresh observation, serves the outcome and closes the session', async (t) => {
  const handles = await baselineHandles();
  const log = sessionLog();
  let observedAt = new Date(T0 - 1_000).toISOString();
  let end: MowerCommandEnd = 'reflected';
  let failure: Error | null = null;
  const f = await fixture(t, { host: HOST_A, ...CONTROL_MODE }, {
    openLocalSession: sessions(log, async () => telemetry(observedAt), undefined, async (request) => {
      if (failure) throw failure;
      return outcome(request.kind, end, new Date(T0 + 5_000).toISOString());
    }),
  });
  await f.bridge.connect();
  const state = (await f.get(STATE_PATH)).json as BridgeState;
  assert.equal(state.operating_mode, 'control');
  assert.deepEqual(state.routes, { discovery: true, state: true, control: true, maps: false });
  assert.deepEqual(state.control, { classes: ['start', 'pause', 'resume', 'stop'], max_state_age_ms: 30_000, read_back_ms: 20_000 });
  assertNoSecrets(JSON.stringify(state));
  // Without a successful observation nothing is written.
  const unknownState = await f.post(commandPath(ID_A, 'start'));
  assert.equal(unknownState.status, 409);
  assert.deepEqual(unknownState.json, { error: 'telemetry_stale' });
  assert.equal(log.opened.length, 0, 'a stale refusal opens no session');
  assert.equal((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).status, 200);
  f.clock.now = T0 + 5_000;
  const started = await f.post(commandPath(ID_A, 'start'));
  assert.equal(started.status, 200, started.text);
  assertNoSecrets(started.text);
  assert.ok(!started.text.includes(HOST_A) && !started.text.includes('"dps"') && !started.text.includes('PRIVATE-BLOB') && !started.text.includes('155'), 'raw data points, reports and the host are absent');
  const at = new Date(T0 + 5_000).toISOString();
  assert.deepEqual(started.json, {
    contract: 1,
    id: ID_A,
    command: 'start',
    result: 'confirmed',
    write: { dp: '1', code: 'switch_go', value: true },
    sent_at: at,
    stage: 'reflected',
    end: 'reflected',
    before_observed_at: at,
    reply: { observed_at: at, return_code_zero: true, rejected: false },
    acknowledgement: { observed_at: at, sequence: 11, dp: '1' },
    activity: { observed_at: at, sequence: 12, value: 'mowing' },
    payload: null,
    reports: 2,
  });
  assert.deepEqual(log.commands, [{ kind: 'start' }]);
  assert.equal(log.opened.length, 2, 'the command opened its own bounded session');
  assert.deepEqual(log.opened[1], { id: ID_A, options: { host: HOST_A, timeoutMs: 5_000 } });
  assert.equal(log.disconnects, 2, 'the command session is closed');
  assert.equal(log.lastConnected(), false);
  // Freshness is the age of the last successful observation against the documented maximum.
  f.clock.now = T0 - 1_000 + 30_000;
  end = 'timed_out';
  const paused = (await f.post(commandPath(ID_A, 'pause'))).json as MowerCommandDocument;
  assert.equal(paused.result, 'uncertain', 'a timed out write is uncertain, never confirmed and never repeated');
  assert.equal(paused.stage, 'sent');
  assert.equal(paused.end, 'timed_out');
  assert.equal(paused.activity, null);
  assert.equal(paused.reports, 0);
  assert.deepEqual(paused.write, { dp: '2', code: 'pause', value: true });
  f.clock.now += 1;
  const tooOld = await f.post(commandPath(ID_A, 'resume'));
  assert.equal(tooOld.status, 409);
  assert.deepEqual(tooOld.json, { error: 'telemetry_stale' });
  assert.equal(log.commands.length, 2, 'the stale refusal reached no session');
  observedAt = new Date(f.clock.now).toISOString();
  assert.equal((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).status, 200, 'a new observation renews the window');
  end = 'rejected';
  const resumed = (await f.post(commandPath(ID_A, 'resume'))).json as MowerCommandDocument;
  assert.equal(resumed.result, 'failed');
  assert.deepEqual(resumed.reply, { observed_at: new Date(T0 + 5_000).toISOString(), return_code_zero: false, rejected: true });
  assert.deepEqual(resumed.write, { dp: '2', code: 'pause', value: false });
  // Classes and ids are checked on the bridge.
  const unsupported = await f.post(commandPath(ID_A, 'return'));
  assert.equal(unsupported.status, 409);
  assert.deepEqual(unsupported.json, { error: 'command_unsupported' });
  const unknownKind = await f.post(commandPath(ID_A, 'jump'));
  assert.equal(unknownKind.status, 404);
  assert.deepEqual(unknownKind.json, { error: 'not_found' });
  const invalid = await f.post(commandPath('not-a-mower-id', 'start'));
  assert.equal(invalid.status, 400);
  assert.deepEqual(invalid.json, { error: 'invalid_mower_id' });
  const unknown = await f.post(commandPath(ID_B, 'start'));
  assert.equal(unknown.status, 404);
  assert.deepEqual(unknown.json, { error: 'unknown_mower' });
  assert.equal(log.commands.length, 3, 'refused classes and ids reached no session');
  // The library's typed refusals map to 409 with the library code, other failures to 503.
  failure = new EufyError('mower_command_map_saving');
  const refused = await f.post(commandPath(ID_A, 'start'));
  assert.equal(refused.status, 409);
  assert.deepEqual(refused.json, { error: 'mower_command_map_saving' });
  assert.equal(log.disconnects, log.opened.length, 'a refused command still closes its session');
  failure = new EufyError('mower_local_disconnected');
  const lost = await f.post(commandPath(ID_A, 'start'));
  assert.equal(lost.status, 503);
  assert.deepEqual(lost.json, { error: 'mower_local_disconnected' });
  failure = new TypeError('unexpected detail');
  const unexpected = await f.post(commandPath(ID_A, 'start'));
  assert.equal(unexpected.status, 503);
  assert.deepEqual(unexpected.json, { error: 'internal_error' });
  assert.ok(!unexpected.text.includes('unexpected detail'));
  await f.bridge.stop();
  assert.deepEqual(await settledHandles(handles), handles);
});

test('control mode routes stop, serves the map-saving payload as its reflection and keeps return unsupported', async (t) => {
  const handles = await baselineHandles();
  const log = sessionLog();
  let end: MowerCommandEnd = 'reflected';
  const f = await fixture(t, { host: HOST_A, ...CONTROL_MODE }, {
    openLocalSession: sessions(log, async () => telemetry(new Date(T0 - 1_000).toISOString()), undefined, async (request) =>
      outcome(request.kind, end, new Date(T0 + 5_000).toISOString()),
    ),
  });
  await f.bridge.connect();
  assert.equal((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).status, 200);
  f.clock.now = T0 + 5_000;
  const stopped = await f.post(commandPath(ID_A, 'stop'));
  assert.equal(stopped.status, 200, stopped.text);
  assertNoSecrets(stopped.text);
  assert.ok(!stopped.text.includes(HOST_A) && !stopped.text.includes('"dps"') && !stopped.text.includes('PRIVATE-BLOB'), 'raw data points, reports and the host are absent');
  const at = new Date(T0 + 5_000).toISOString();
  assert.deepEqual(stopped.json, {
    contract: 1,
    id: ID_A,
    command: 'stop',
    result: 'confirmed',
    write: { dp: '1', code: 'switch_go', value: false },
    sent_at: at,
    stage: 'reflected',
    end: 'reflected',
    before_observed_at: at,
    reply: { observed_at: at, return_code_zero: true, rejected: false },
    acknowledgement: { observed_at: at, sequence: 11, dp: '1' },
    activity: null,
    payload: { observed_at: at, sequence: 12, name: 'map_saving' },
    reports: 2,
  });
  assert.deepEqual(log.commands, [{ kind: 'stop' }]);
  assert.equal(log.disconnects, log.opened.length, 'the stop session is closed');
  // A stop whose read-back passed without the payload is uncertain, like every other class.
  end = 'timed_out';
  const uncertain = (await f.post(commandPath(ID_A, 'stop'))).json as MowerCommandDocument;
  assert.equal(uncertain.result, 'uncertain');
  assert.equal(uncertain.end, 'timed_out');
  assert.equal(uncertain.activity, null);
  assert.equal(uncertain.payload, null);
  // Return stays unsupported: the owned firmware ignored DP 3 from paused and from the stopped task.
  const unsupported = await f.post(commandPath(ID_A, 'return'));
  assert.equal(unsupported.status, 409);
  assert.deepEqual(unsupported.json, { error: 'command_unsupported' });
  assert.equal(log.commands.length, 2, 'return reached no session');
  await f.bridge.stop();
  assert.deepEqual(await settledHandles(handles), handles);
});

test('a command needs a configured host, a non-stale observation and exclusive ownership of the mower', async (t) => {
  const log = sessionLog();
  let openFailure: string | null = null;
  let queryFailure: Error | null = null;
  const f = await fixture(t, { hosts: { [ID_A]: HOST_A }, ...CONTROL_MODE }, {
    openLocalSession: sessions(
      log,
      async () => {
        if (queryFailure) throw queryFailure;
        return telemetry(new Date(T0).toISOString());
      },
      () => openFailure,
      async (request) => {
        await new Promise((resolve) => setTimeout(resolve, 40));
        return outcome(request.kind, 'reflected', new Date(T0).toISOString());
      },
    ),
  });
  f.adapter.devices = [device(ID_A), device(ID_B)];
  await f.bridge.connect();
  const noHost = await f.post(commandPath(ID_B, 'start'));
  assert.equal(noHost.status, 503);
  assert.deepEqual(noHost.json, { error: 'mower_host_unconfigured' });
  assert.equal((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).status, 200);
  const [one, two] = await Promise.all([f.post(commandPath(ID_A, 'start')), f.post(commandPath(ID_A, 'pause'))]);
  const statuses = [one.status, two.status].sort();
  assert.deepEqual(statuses, [200, 409], 'one command owns the mower at a time');
  assert.deepEqual((one.status === 409 ? one : two).json, { error: 'command_in_progress' });
  assert.equal(log.commands.length, 1, 'the second command never reached a session');
  // A failed state query leaves the mower stale until a query succeeds again, whatever the age.
  queryFailure = new EufyError('mower_local_protocol_error');
  assert.equal(((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).json as MowerStateDocument).stale, true);
  const stale = await f.post(commandPath(ID_A, 'start'));
  assert.equal(stale.status, 409);
  assert.deepEqual(stale.json, { error: 'telemetry_stale' });
  queryFailure = null;
  assert.equal(((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).json as MowerStateDocument).stale, false);
  openFailure = 'mower_local_unreachable';
  const unreachable = await f.post(commandPath(ID_A, 'start'));
  assert.equal(unreachable.status, 503);
  assert.deepEqual(unreachable.json, { error: 'mower_local_unreachable' });
  assert.equal(log.commands.length, 1, 'a failed session open never reaches a command');
});

test('shutdown aborts an in-flight command, answers it and leaves no handles', async (t) => {
  const handles = await baselineHandles();
  const log = sessionLog();
  const f = await fixture(t, { host: HOST_A, ...CONTROL_MODE }, {
    openLocalSession: sessions(
      log,
      async () => telemetry(new Date(T0).toISOString()),
      undefined,
      (_request, signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(new EufyError('request_aborted')), { once: true })),
    ),
  });
  await f.bridge.connect();
  assert.equal((await f.get(`${MOWERS_PATH}/${ID_A}/state`)).status, 200);
  const pending = f.post(commandPath(ID_A, 'start'));
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(log.commands.length, 1);
  await f.bridge.stop();
  const aborted = await pending;
  assert.equal(aborted.status, 503);
  assert.deepEqual(aborted.json, { error: 'request_aborted' });
  assert.equal(log.disconnects, 2, 'the aborted command session is closed');
  await assert.rejects(f.bridge.command(ID_A, 'start'), { code: 'bridge_not_running' });
  assert.deepEqual(await settledHandles(handles), handles);
});
