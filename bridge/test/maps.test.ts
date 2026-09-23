import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { test } from 'node:test';
import { crc32 } from 'node:zlib';
import type { MapAcquisitionSnapshot, MapStreamName } from '@keesmod/eufy-mega-client';
import { ApiError, BridgeError } from '../src/errors.ts';
import {
  MAP_FILES,
  MAP_FILE_MAX_BYTES,
  MAX_PROVISIONING_BYTES,
  buildMapBundle,
  checkMapSnapshot,
  mapMode,
  readProvisioningFile,
  snapshotDigest,
} from '../src/maps.ts';
import { temporaryDirectory } from './helpers.ts';
import { PROVISIONING_SECRETS, SYNTHETIC_PROVISIONING, bytesField, field, mapFile, pathFile, poseFile, readZip, streams, writeProvisioning } from './map-fixtures.ts';

const DEVICE = 'a'.repeat(64);
const RECEIVED_AT = Date.parse('2026-09-23T10:00:00.250Z');

function snapshot(files: Record<MapStreamName, Uint8Array>, receivedAt = RECEIVED_AT, revision = 1): MapAcquisitionSnapshot {
  return { revision, receivedAt, files };
}

interface ContractFixture {
  device_id: string;
  received_at: number;
  files: Record<MapStreamName, string>;
  snapshot_id: string;
  etag: string;
  bundle: string;
}

test('the bridge builds byte for byte the bundle that the integration test validates', async () => {
  // tests/test_bridge_map.py decodes this same bundle with the integration's own validator.
  const fixture = JSON.parse(await readFile(join(import.meta.dirname, '..', '..', 'tests', 'fixtures', 'bridge_map_bundle.json'), 'utf8')) as ContractFixture;
  const files = Object.fromEntries(Object.entries(fixture.files).map(([name, value]) => [name, new Uint8Array(Buffer.from(value, 'base64'))])) as Record<MapStreamName, Uint8Array>;
  assert.deepEqual(checkMapSnapshot(snapshot(files)), { ok: true, fullPath: false }, "the integration's synthetic streams pass the library decoder");
  const bundle = buildMapBundle(fixture.device_id, snapshot(files, fixture.received_at));
  assert.equal(bundle.body.toString('base64'), fixture.bundle);
  assert.equal(bundle.etag, fixture.etag);
  assert.equal(bundle.snapshotId, fixture.snapshot_id);
});

test('the bundle is a stored ZIP with the manifest and the three files exactly as retained', () => {
  const files = streams();
  const bundle = buildMapBundle(DEVICE, snapshot(files));
  const members = readZip(bundle.body);
  assert.deepEqual([...members.keys()], ['manifest.json', ...MAP_FILES]);
  for (const [name, member] of members) {
    assert.equal(member.method, 0, `${name} is stored without compression`);
    assert.equal(member.flags, 0, `${name} is neither encrypted nor streamed`);
    assert.equal(member.crc, crc32(member.data), `${name} carries its CRC-32`);
  }
  for (const name of MAP_FILES) assert.deepEqual(new Uint8Array(members.get(name)!.data), files[name], name);
  const manifest = JSON.parse(members.get('manifest.json')!.data.toString('utf8')) as Record<string, unknown>;
  assert.deepEqual(manifest, {
    schema_version: 1,
    device_id: DEVICE,
    captured_at: Math.floor(RECEIVED_AT / 1000),
    snapshot_id: bundle.snapshotId,
    files: Object.fromEntries(MAP_FILES.map((name) => [name, { size: files[name].length, sha256: createHash('sha256').update(files[name]).digest('hex') }])),
  });
  assert.equal(bundle.etag, `"${bundle.snapshotId}-${Math.floor(RECEIVED_AT / 1000)}"`);
  assert.equal(bundle.deviceId, DEVICE);
  assert.equal(bundle.receivedAt, RECEIVED_AT);
});

test('the snapshot digest covers every name, length and byte and the tag follows the capture second', () => {
  const files = streams();
  const digest = createHash('sha256');
  for (const name of MAP_FILES) {
    const length = Buffer.alloc(8);
    length.writeBigUInt64BE(BigInt(files[name].length));
    digest.update(Buffer.from(name, 'ascii')).update(length).update(files[name]);
  }
  assert.equal(snapshotDigest(files), digest.digest('hex'));
  const moved = { ...files, 'navPath.bin.stream': new Uint8Array([...files['navPath.bin.stream'], 0]) };
  assert.notEqual(snapshotDigest(moved), snapshotDigest(files));
  const first = buildMapBundle(DEVICE, snapshot(files));
  assert.deepEqual(buildMapBundle(DEVICE, snapshot(streams())).body, first.body, 'identical input gives identical bytes');
  const sameSecond = buildMapBundle(DEVICE, snapshot(files, RECEIVED_AT + 500, 2));
  assert.equal(sameSecond.etag, first.etag, 'the manifest dates in whole seconds, so the bytes and the tag are the same');
  assert.deepEqual(sameSecond.body, first.body);
  const later = buildMapBundle(DEVICE, snapshot(files, RECEIVED_AT + 1_000, 3));
  assert.equal(later.snapshotId, first.snapshotId);
  assert.notEqual(later.etag, first.etag, 'a later capture of the same files is a new representation');
});

test('only snapshots that the library decodes into a realtime map with a lawn boundary pass the gate', () => {
  assert.deepEqual(checkMapSnapshot(snapshot(streams({ path: 'history' }))), { ok: true, fullPath: true });
  assert.deepEqual(checkMapSnapshot(snapshot(streams({ path: 'realtime' }))), { ok: true, fullPath: false }, 'the empty placeholder path is valid but not the full path');
  const map = mapFile();
  const cases: [string, Partial<Record<MapStreamName, Uint8Array>>, string][] = [
    ['truncated map file', { 'map.bin.stream': map.subarray(0, map.length - 1) }, 'map_undecodable'],
    ['multi-map channel without a realtime map', { 'map.bin.stream': new Uint8Array(bytesField(3, field(1, 1))) }, 'map_undecodable'],
    ['truncated path file', { 'cleanPath.bin.stream': pathFile('history').subarray(0, 5) }, 'map_undecodable'],
    ['pose file with a nested record', { 'navPath.bin.stream': new Uint8Array(bytesField(1, poseFile())) }, 'map_undecodable'],
    ['degenerate boundary', { 'map.bin.stream': mapFile({ boundary: [[0, 0], [5, 5]] }) }, 'map_boundary_missing'],
    ['region without a boundary', { 'map.bin.stream': mapFile({ boundary: [] }) }, 'map_boundary_missing'],
    ['empty file', { 'navPath.bin.stream': new Uint8Array(0) }, 'map_file_size'],
    ['file above the integration limit', { 'navPath.bin.stream': new Uint8Array(MAP_FILE_MAX_BYTES + 1) }, 'map_file_size'],
  ];
  for (const [name, replaced, code] of cases) {
    assert.deepEqual(checkMapSnapshot(snapshot({ ...streams(), ...replaced })), { ok: false, code }, name);
  }
});

test('the provisioning file is read afresh, must be private and never reveals its content', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const root = join(directory.path, '..', 'private');
  const good = await writeProvisioning(root);
  assert.deepEqual(await readProvisioningFile(good), SYNTHETIC_PROVISIONING);
  for (const mode of [0o400, 0o640, 0o440]) assert.deepEqual(await readProvisioningFile(await writeProvisioning(root, SYNTHETIC_PROVISIONING, mode, `mode-${mode.toString(8)}.json`)), SYNTHETIC_PROVISIONING);
  await mkdir(join(root, 'directory.json'), { mode: 0o700 });
  const cases: [string, Promise<string>, string][] = [
    ['readable by others', writeProvisioning(root, SYNTHETIC_PROVISIONING, 0o644, 'others.json'), 'map_provisioning_insecure'],
    ['writable by the group', writeProvisioning(root, SYNTHETIC_PROVISIONING, 0o660, 'group.json'), 'map_provisioning_insecure'],
    ['missing', Promise.resolve(join(root, 'missing.json')), 'map_provisioning_unreadable'],
    ['a directory', Promise.resolve(join(root, 'directory.json')), 'map_provisioning_unreadable'],
    ['empty', writeProvisioning(root, '', 0o600, 'empty.json'), 'map_provisioning_unreadable'],
    ['not JSON', writeProvisioning(root, 'PRIVATE-RRRRRRRR', 0o600, 'text.json'), 'map_provisioning_unreadable'],
    ['a JSON array', writeProvisioning(root, '[1]', 0o600, 'array.json'), 'map_provisioning_unreadable'],
    ['too large', writeProvisioning(root, `"${'x'.repeat(MAX_PROVISIONING_BYTES)}"`, 0o600, 'large.json'), 'map_provisioning_unreadable'],
  ];
  for (const [name, path, code] of cases) {
    await assert.rejects(readProvisioningFile(await path), (error: unknown) => {
      assert.ok(error instanceof BridgeError, name);
      assert.equal(error.code, code, name);
      for (const secret of PROVISIONING_SECRETS) assert.ok(!error.message.includes(secret), `${name} leaks nothing`);
      return true;
    });
  }
});

test('the map mode header is idle by default, stream on request and nothing else', () => {
  assert.equal(mapMode(undefined), 'idle');
  assert.equal(mapMode(''), 'idle');
  assert.equal(mapMode('idle'), 'idle');
  assert.equal(mapMode('stream'), 'stream');
  for (const value of ['STREAM', 'live', 'idle, stream']) {
    assert.throws(() => mapMode(value), (error: unknown) => error instanceof ApiError && error.status === 400 && error.code === 'invalid_map_mode', value);
  }
});
