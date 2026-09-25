import assert from 'node:assert/strict';
import { rm } from 'node:fs/promises';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import type { AuthAnswer, AuthState, MowerAdapter, MowerDevice } from '@keesmod/eufy-mega-client';
import { MowerBridge, type BridgeDependencies, type BridgeState } from '../src/bridge.ts';
import { MAP_CONTENT_TYPE, libraryMapAcquisition } from '../src/maps.ts';
import { MOWERS_PATH, STATE_PATH } from '../src/server.ts';
import { TOKEN, assertNoSecrets, baselineHandles, call, settledHandles, temporaryDirectory, testConfig, type Reply } from './helpers.ts';
import { PROVISIONING_SECRETS, SYNTHETIC_PROVISIONING, fakeMaps, readZip, streams, waitFor, writeProvisioning, type FakeMapAcquisition, type FakeMaps } from './map-fixtures.ts';

const ID_A = 'a'.repeat(64);
const ID_B = 'b'.repeat(64);
const T0 = Date.parse('2026-09-23T12:00:00Z');
/** Stands in for private lawn data inside the synthetic map file. It must stay inside the bundle. */
const LAWN_NAME = 'PRIVATE-LAWN-NAME';
const STREAM = { 'x-eufy-map-mode': 'stream' };
const mapPath = (id: string) => `${MOWERS_PATH}/${id}/map`;
const iso = (time: number) => new Date(time).toISOString();

function device(id: string): MowerDevice {
  return { id, kind: 'mower', model: 'E15', productCode: 'T2880' };
}

/** Authenticates and discovers without any network. */
class Adapter implements MowerAdapter {
  connected = false;
  devices: MowerDevice[] = [device(ID_A)];

  async connect(_answer: AuthAnswer | undefined, _signal: AbortSignal): Promise<AuthState> {
    this.connected = true;
    return { state: 'connected' };
  }

  async shutdown(): Promise<void> {
    this.connected = false;
  }

  async discover(_signal: AbortSignal): Promise<MowerDevice[]> {
    return this.devices.map((entry) => ({ ...entry }));
  }
}

interface MapFixture {
  bridge: MowerBridge;
  clock: { now: number };
  maps: FakeMaps;
  provisioning: string;
  privateDirectory: string;
  get: (path: string, headers?: Record<string, string>) => Promise<Reply>;
  state: () => Promise<BridgeState>;
}

async function mapFixture(
  t: TestContext,
  options: { devices?: MowerDevice[]; mowerId?: string; configured?: boolean; provisioning?: boolean; dependencies?: Omit<BridgeDependencies, 'adapter' | 'now'> } = {},
): Promise<MapFixture> {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const privateDirectory = join(directory.path, '..', 'private');
  const provisioning = options.provisioning === false ? join(privateDirectory, 'map-provisioning.json') : await writeProvisioning(privateDirectory);
  const adapter = new Adapter();
  if (options.devices) adapter.devices = options.devices;
  const clock = { now: T0 };
  const maps = fakeMaps();
  const config = testConfig(directory.path, {
    maps: options.configured === false ? null : { provisioningFile: provisioning, mowerId: options.mowerId ?? null },
  });
  const bridge = new MowerBridge(config, { adapter: () => adapter, now: () => clock.now, mapAcquisition: maps.create, mapWatchIntervalMs: 5, ...options.dependencies });
  t.after(() => bridge.stop().catch(() => {}));
  await bridge.start();
  await bridge.connect();
  const address = bridge.address;
  assert.ok(address);
  const base = `http://127.0.0.1:${address.port}`;
  const get = (path: string, headers: Record<string, string> = {}) => call(base, path, { token: TOKEN, headers });
  return { bridge, clock, maps, provisioning, privateDirectory, get, state: async () => (await get(STATE_PATH)).json as BridgeState };
}

/** The demand reaches the library after the provisioning file was read, so wait for it. */
async function nextDemand(f: MapFixture, count: number): Promise<FakeMapAcquisition> {
  await waitFor(() => f.maps.created.length === count && f.maps.created[count - 1]!.running, `demand ${count} to reach the library`);
  return f.maps.created[count - 1]!;
}

/** Waits until the running demand has ended and its outcome is recorded. */
async function settled(f: MapFixture): Promise<void> {
  await waitFor(() => f.bridge.state().maps?.acquiring === false, 'the demand to settle');
}

function assertPrivate(text: string): void {
  assertNoSecrets(text);
  assert.ok(!text.includes(LAWN_NAME), 'no map content');
  for (const secret of PROVISIONING_SECRETS) assert.ok(!text.includes(secret), 'no provisioning content');
}

test('without map provisioning the route answers 404 and the state reports no map', async (t) => {
  const f = await mapFixture(t, { configured: false });
  const reply = await f.get(mapPath(ID_A));
  assert.equal(reply.status, 404);
  assert.deepEqual(reply.json, { error: 'map_unconfigured' });
  const state = await f.state();
  assert.deepEqual(state.routes, { discovery: true, state: true, control: false, maps: false, settings: false });
  assert.equal(state.maps, null);
  assert.equal(f.maps.created.length, 0);
});

test('the map route validates the id, the mode, discovery and the mower the provisioning belongs to', async (t) => {
  const f = await mapFixture(t);
  assert.deepEqual((await f.get(mapPath('not-a-mower-id'))).json, { error: 'invalid_mower_id' });
  const mode = await f.get(mapPath(ID_A), { 'x-eufy-map-mode': 'live' });
  assert.equal(mode.status, 400);
  assert.deepEqual(mode.json, { error: 'invalid_map_mode' });
  const unknown = await f.get(mapPath(ID_B));
  assert.equal(unknown.status, 404);
  assert.deepEqual(unknown.json, { error: 'unknown_mower' });
  assert.equal(f.maps.created.length, 0, 'no demand for a refused request');

  const two = await mapFixture(t, { devices: [device(ID_A), device(ID_B)] });
  const unresolved = await two.get(mapPath(ID_A));
  assert.equal(unresolved.status, 409);
  assert.deepEqual(unresolved.json, { error: 'map_mower_unresolved' });
  assert.equal(two.maps.created.length, 0);

  const named = await mapFixture(t, { devices: [device(ID_A), device(ID_B)], mowerId: ID_B });
  const other = await named.get(mapPath(ID_A));
  assert.equal(other.status, 404);
  assert.deepEqual(other.json, { error: 'map_unconfigured' });
  const owned = await named.get(mapPath(ID_B));
  assert.equal(owned.status, 503);
  assert.deepEqual(owned.json, { error: 'map_unavailable' });
  await nextDemand(named, 1);
});

test('an idle request starts one demand, is answered at once and the demand ends after the full path', async (t) => {
  const f = await mapFixture(t);
  const first = await f.get(mapPath(ID_A));
  assert.equal(first.status, 503, 'nothing to serve yet, the request does not wait for the demand');
  assert.deepEqual(first.json, { error: 'map_unavailable' });
  const acquisition = await nextDemand(f, 1);
  assert.deepEqual(acquisition.provisioning, SYNTHETIC_PROVISIONING, 'the library receives the operator file as written');
  assert.equal(acquisition.demands[0]?.demandMs, 30_000);
  let state = await f.state();
  assert.deepEqual(state.routes, { discovery: true, state: true, control: false, maps: true, settings: false });
  assert.deepEqual(state.maps, { captured_at: null, age_ms: null, stale: false, error: null, acquiring: true, streaming: false, last_demand: null });

  // A demand opens with the empty realtime placeholder path.
  acquisition.publish(streams({ path: 'realtime', name: LAWN_NAME }), T0 - 1_000);
  await waitFor(() => f.bridge.state().maps?.captured_at !== null, 'the first snapshot');
  const placeholder = await f.get(mapPath(ID_A));
  assert.equal(placeholder.status, 200);
  assert.equal(placeholder.headers['content-type'], MAP_CONTENT_TYPE);
  assert.equal(placeholder.headers['cache-control'], 'no-store');
  assert.equal(placeholder.headers['content-length'], String(placeholder.raw.length));
  assert.match(String(placeholder.headers.etag), /^"[0-9a-f]{64}-\d+"$/);
  assert.equal(placeholder.headers['x-eufy-map-captured-at'], iso(T0 - 1_000));
  assert.equal(placeholder.headers['x-eufy-map-age-ms'], '1000');
  assert.equal(placeholder.headers['x-eufy-map-stale'], 'false');
  assert.equal(placeholder.headers['x-eufy-map-error'], undefined);
  const manifest = JSON.parse(readZip(placeholder.raw).get('manifest.json')!.data.toString('utf8')) as { device_id: string; captured_at: number };
  assert.equal(manifest.device_id, ID_A);
  assert.equal(manifest.captured_at, Math.floor((T0 - 1_000) / 1000));
  assert.equal(acquisition.running, true, 'the placeholder alone keeps the idle demand open');

  // The full path arrives and ends the idle demand, as the retained source closed its idle stream.
  acquisition.publish(streams({ path: 'history', name: LAWN_NAME }), T0 - 200);
  await waitFor(() => acquisition.ended !== undefined, 'the idle demand to end');
  assert.equal(acquisition.ended, 'aborted');
  await settled(f);
  assert.equal(acquisition.shutdowns, 1);
  assert.equal(acquisition.cleared, 1, "the library's retained bytes are released");
  state = await f.state();
  assert.deepEqual(state.maps?.last_demand, {
    started_at: iso(T0),
    ended_at: iso(T0),
    end: 'aborted',
    cancellation_confirmed: true,
    cleanup_confirmed: true,
    published: 2,
    rejected: 0,
  });
  const full = await f.get(mapPath(ID_A));
  assert.equal(full.status, 200);
  assert.notEqual(full.headers.etag, placeholder.headers.etag);
  assert.equal(full.headers['x-eufy-map-age-ms'], '200');
  assert.ok(full.raw.includes(Buffer.from(LAWN_NAME)), 'the bundle carries the private map to the authenticated client');

  f.clock.now += 60_000;
  const unchanged = await f.get(mapPath(ID_A), { 'if-none-match': String(full.headers.etag) });
  assert.equal(unchanged.status, 304);
  assert.equal(unchanged.raw.length, 0);
  assert.equal(unchanged.headers.etag, full.headers.etag);
  assert.equal(unchanged.headers['x-eufy-map-age-ms'], '60200', 'the age grows with the bridge clock');
  assert.equal((await f.get(mapPath(ID_A), { 'if-none-match': `W/"other", ${String(full.headers.etag)}` })).status, 304);
  assert.equal(f.bridge.state().maps?.acquiring, false, 'no second demand within the idle interval');
  assert.equal(f.maps.created.length, 1);
  assertPrivate((await f.get(STATE_PATH)).text);

  f.clock.now += 5 * 60_000;
  const refreshing = await f.get(mapPath(ID_A), { 'if-none-match': String(full.headers.etag) });
  assert.equal(refreshing.status, 304, 'the last good bundle is served while the next demand runs');
  await nextDemand(f, 2);
});

test('a stream request keeps demands running for the lease and chains the next one', async (t) => {
  const f = await mapFixture(t);
  assert.equal((await f.get(mapPath(ID_A), STREAM)).status, 503);
  const first = await nextDemand(f, 1);
  assert.equal((await f.state()).maps?.streaming, true);
  first.publish(streams({ path: 'history' }), T0);
  await waitFor(() => f.bridge.state().maps?.captured_at !== null, 'the first snapshot');
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(first.running, true, 'under a stream lease the full path does not end the demand');
  const early = await f.get(mapPath(ID_A), STREAM);
  assert.equal(early.status, 200);
  assert.equal(f.maps.created.length, 1, 'one demand at a time');
  first.end('demand_expired');
  await settled(f);
  assert.equal((await f.state()).maps?.error, null);
  assert.equal((await f.state()).maps?.last_demand?.end, 'demand_expired');

  f.clock.now += 2_000;
  const chained = await f.get(mapPath(ID_A), { ...STREAM, 'if-none-match': String(early.headers.etag) });
  assert.equal(chained.status, 304);
  const second = await nextDemand(f, 2);
  second.publish(streams({ path: 'history' }), f.clock.now);
  await waitFor(() => f.bridge.state().maps?.captured_at === iso(f.clock.now), 'the second snapshot');
  assert.equal(second.running, true);

  f.clock.now += 31_000;
  await waitFor(() => second.ended !== undefined, 'the demand to end once the lease expired');
  assert.equal(second.ended, 'aborted');
  assert.equal((await f.state()).maps?.streaming, false);
});

test('without any bundle the route answers 503 with the failure code and the next attempt waits', async (t) => {
  const f = await mapFixture(t, { provisioning: false });
  const first = await f.get(mapPath(ID_A));
  assert.equal(first.status, 503);
  assert.deepEqual(first.json, { error: 'map_unavailable' });
  await settled(f);
  const failed = await f.get(mapPath(ID_A));
  assert.equal(failed.status, 503);
  assert.deepEqual(failed.json, { error: 'map_provisioning_unreadable' });
  assert.equal(f.maps.created.length, 0, 'no acquisition without provisioning');
  const state = await f.state();
  assert.deepEqual(state.maps, { captured_at: null, age_ms: null, stale: false, error: 'map_provisioning_unreadable', acquiring: false, streaming: false, last_demand: null });

  await writeProvisioning(f.privateDirectory, SYNTHETIC_PROVISIONING, 0o644);
  f.clock.now += 30_000;
  await f.get(mapPath(ID_A), STREAM);
  assert.equal(f.bridge.state().maps?.acquiring, false, 'no attempt before the retry interval, even in stream mode');
  f.clock.now += 30_000;
  await f.get(mapPath(ID_A));
  await settled(f);
  assert.deepEqual((await f.get(mapPath(ID_A))).json, { error: 'map_provisioning_insecure' });

  await writeProvisioning(f.privateDirectory, SYNTHETIC_PROVISIONING, 0o600);
  f.clock.now += 60_000;
  await f.get(mapPath(ID_A));
  await nextDemand(f, 1);
});

test('a failed or refused demand keeps the last good bundle, marks it stale and spaces the next demand', async (t) => {
  const f = await mapFixture(t);
  await f.get(mapPath(ID_A));
  const first = await nextDemand(f, 1);
  first.publish(streams(), T0);
  first.end('demand_expired');
  await settled(f);
  const good = await f.get(mapPath(ID_A));
  assert.equal(good.status, 200);

  f.clock.now += 5 * 60_000;
  await f.get(mapPath(ID_A));
  const second = await nextDemand(f, 2);
  second.end('connection_failed', { cancellationConfirmed: false, cleanupConfirmed: true });
  await settled(f);
  const stale = await f.get(mapPath(ID_A));
  assert.equal(stale.status, 200);
  assert.equal(stale.headers.etag, good.headers.etag);
  assert.deepEqual(stale.raw, good.raw, 'the last good bundle is unchanged');
  assert.equal(stale.headers['x-eufy-map-stale'], 'true');
  assert.equal(stale.headers['x-eufy-map-error'], 'mower_map_connection_failed');
  let state = await f.state();
  assert.equal(state.maps?.stale, true);
  assert.equal(state.maps?.error, 'mower_map_connection_failed');
  assert.deepEqual(state.maps?.last_demand, {
    started_at: iso(f.clock.now),
    ended_at: iso(f.clock.now),
    end: 'connection_failed',
    cancellation_confirmed: false,
    cleanup_confirmed: true,
    published: 0,
    rejected: 0,
  });

  f.clock.now += 30_000;
  await f.get(mapPath(ID_A), STREAM);
  assert.equal(f.bridge.state().maps?.acquiring, false, 'the next demand waits for the retry interval');
  f.clock.now += 30_000;
  await f.get(mapPath(ID_A), STREAM);
  const third = await nextDemand(f, 3);
  third.publish({ ...streams(), 'map.bin.stream': new Uint8Array([0xff]) }, f.clock.now);
  third.end('demand_expired');
  await settled(f);
  state = await f.state();
  assert.equal(state.maps?.error, 'map_undecodable', 'a snapshot the decoder refuses is never published');
  assert.equal(state.maps?.last_demand?.rejected, 1);
  assert.equal((await f.get(mapPath(ID_A))).headers.etag, good.headers.etag);

  f.clock.now += 60_000;
  await f.get(mapPath(ID_A), STREAM);
  const expired = await nextDemand(f, 4);
  expired.end('demand_expired');
  await settled(f);
  assert.equal((await f.state()).maps?.error, 'mower_map_incomplete', 'a demand without a complete snapshot published nothing');

  await rm(f.provisioning);
  f.clock.now += 60_000;
  await f.get(mapPath(ID_A), STREAM);
  await settled(f);
  assert.equal((await f.state()).maps?.error, 'map_provisioning_unreadable');
  assert.equal(f.maps.created.length, 4, 'the file is read for every demand');

  await writeProvisioning(f.privateDirectory);
  f.clock.now += 60_000;
  await f.get(mapPath(ID_A), STREAM);
  const recovered = await nextDemand(f, 5);
  recovered.publish(streams(), f.clock.now);
  recovered.end('demand_expired');
  await settled(f);
  const fresh = await f.get(mapPath(ID_A));
  assert.equal(fresh.headers['x-eufy-map-stale'], 'false');
  assert.notEqual(fresh.headers.etag, good.headers.etag, 'a later capture is a new representation');
  assertPrivate((await f.get(STATE_PATH)).text);
});

test("the library's own validation refuses expired provisioning before any network I/O", async (t) => {
  const f = await mapFixture(t, { dependencies: { mapAcquisition: libraryMapAcquisition } });
  assert.equal((await f.get(mapPath(ID_A))).status, 503);
  await settled(f);
  const reply = await f.get(mapPath(ID_A));
  assert.deepEqual(reply.json, { error: 'mower_map_invalid_provisioning' });
  const state = await f.state();
  assert.equal(state.maps?.last_demand, null, 'the demand never reached the library acquisition');
  assertPrivate(reply.text + JSON.stringify(state));
});

test('unconfirmed cleanup stops acquisition until a restart and keeps the last good bundle', async (t) => {
  for (const variant of ['result', 'shutdown'] as const) {
    const f = await mapFixture(t);
    await f.get(mapPath(ID_A));
    const acquisition = await nextDemand(f, 1);
    acquisition.publish(streams(), T0);
    if (variant === 'shutdown') {
      acquisition.failShutdown = true;
      acquisition.end('demand_expired');
    } else acquisition.end('demand_expired', { cancellationConfirmed: true, cleanupConfirmed: false });
    await settled(f);
    const reply = await f.get(mapPath(ID_A));
    assert.equal(reply.status, 200, variant);
    assert.equal(reply.headers['x-eufy-map-error'], 'mower_map_cleanup_unconfirmed', variant);
    assert.equal((await f.state()).maps?.last_demand?.cleanup_confirmed, false, variant);
    f.clock.now += 3_600_000;
    await f.get(mapPath(ID_A), STREAM);
    assert.equal(f.bridge.state().maps?.acquiring, false, `${variant}: no further demand`);
    assert.equal(f.maps.created.length, 1, variant);
  }
});

test('shutdown aborts a running demand, waits for its cleanup and leaves no handles', async (t) => {
  const handles = await baselineHandles();
  const f = await mapFixture(t);
  assert.equal((await f.get(mapPath(ID_A), STREAM)).status, 503);
  const acquisition = await nextDemand(f, 1);
  await f.bridge.stop();
  assert.equal(acquisition.ended, 'aborted');
  assert.equal(acquisition.shutdowns, 1);
  assert.equal(f.bridge.state().maps?.acquiring, false);
  await assert.rejects(f.bridge.map(ID_A, { mode: undefined, ifNoneMatch: undefined }), { code: 'bridge_not_running' });
  assert.deepEqual(await settledHandles(handles), handles);
});
