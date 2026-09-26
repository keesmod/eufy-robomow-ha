import { chmod, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import {
  EufyError,
  type MapAcquisitionDemand,
  type MapAcquisitionEnd,
  type MapAcquisitionResult,
  type MapAcquisitionSnapshot,
  type MapSessionProvisioning,
  type MapStreamName,
} from '@keesmod/eufy-mega-client';
import type { CreateMapAcquisition, MapAcquisitionPort } from '../src/maps.ts';

/**
 * Synthetic E15 map streams built from the library's documented field numbers. No capture,
 * lawn, device or account data. Coordinates are small made-up integers.
 */
function varint(value: number): number[] {
  const bytes: number[] = [];
  let rest = value;
  do {
    const low = rest % 128;
    rest = Math.floor(rest / 128);
    bytes.push(rest > 0 ? low | 0x80 : low);
  } while (rest > 0);
  return bytes;
}

function zigzag(value: number): number {
  return value >= 0 ? value * 2 : -value * 2 - 1;
}

export function field(number: number, value: number): Buffer {
  return Buffer.from([...varint(number * 8), ...varint(value)]);
}

export function bytesField(number: number, payload: Uint8Array | string): Buffer {
  const bytes = typeof payload === 'string' ? Buffer.from(payload, 'utf8') : Buffer.from(payload);
  return Buffer.concat([Buffer.from([...varint(number * 8 + 2), ...varint(bytes.length)]), bytes]);
}

function point(x: number, y: number): Buffer {
  return Buffer.concat([field(1, zigzag(x)), field(2, zigzag(y))]);
}

export type Points = readonly (readonly [number, number])[];
const LAWN: Points = [
  [-20, 0],
  [100, 0],
  [100, 80],
  [0, 80],
];

/**
 * `map.bin.stream`: a channel message whose realtime map (2) carries one map (1) with one region
 * (10). `name` goes into the region name, so a test can prove that geometry never leaves the
 * bundle. `boundary` replaces the region polygon, an empty list omits it.
 */
export function mapFile(options: { name?: string; boundary?: Points } = {}): Uint8Array {
  const boundary = options.boundary ?? LAWN;
  const polygon = Buffer.concat(boundary.map(([x, y]) => bytesField(1, point(x, y))));
  const region = Buffer.concat([field(1, 1), ...(options.name ? [bytesField(2, options.name)] : []), ...(boundary.length ? [bytesField(3, polygon)] : [])]);
  const map = Buffer.concat([field(1, 7), bytesField(10, region), field(16, 539)]);
  return new Uint8Array(bytesField(2, bytesField(1, map)));
}

/** `cleanPath.bin.stream`: `realtime` without points is the placeholder that opens a demand, `history` the full path. */
export function pathFile(kind: 'realtime' | 'history', points: Points = kind === 'history' ? LAWN.slice(0, 3) : []): Uint8Array {
  const entries = points.map(([x, y]) => bytesField(7, Buffer.concat([bytesField(1, point(x, y)), field(2, 0)])));
  return new Uint8Array(Buffer.concat([field(1, 3), field(2, 7), ...(kind === 'history' ? [field(3, 1)] : []), bytesField(5, Buffer.alloc(0)), ...entries]));
}

/** `navPath.bin.stream`: one pose record, display only. */
export function poseFile(): Uint8Array {
  return new Uint8Array(Buffer.concat([field(1, zigzag(9)), field(2, zigzag(10)), field(3, zigzag(1571))]));
}

export function streams(options: { name?: string; path?: 'realtime' | 'history'; boundary?: Points } = {}): Record<MapStreamName, Uint8Array> {
  const mapOptions: { name?: string; boundary?: Points } = {};
  if (options.name !== undefined) mapOptions.name = options.name;
  if (options.boundary !== undefined) mapOptions.boundary = options.boundary;
  return { 'map.bin.stream': mapFile(mapOptions), 'cleanPath.bin.stream': pathFile(options.path ?? 'history'), 'navPath.bin.stream': poseFile() };
}

function copyFiles(files: Readonly<Record<MapStreamName, Uint8Array>>): Record<MapStreamName, Uint8Array> {
  return {
    'map.bin.stream': new Uint8Array(files['map.bin.stream']),
    'cleanPath.bin.stream': new Uint8Array(files['cleanPath.bin.stream']),
    'navPath.bin.stream': new Uint8Array(files['navPath.bin.stream']),
  };
}

export interface ZipMember {
  data: Buffer;
  method: number;
  flags: number;
  crc: number;
}

/** Reads the central directory and the local headers of a ZIP archive. Enough for the bundle checks. */
export function readZip(archive: Buffer): Map<string, ZipMember> {
  const end = archive.length - 22;
  if (archive.readUInt32LE(end) !== 0x06054b50) throw new Error('no end of central directory');
  const count = archive.readUInt16LE(end + 10);
  let cursor = archive.readUInt32LE(end + 16);
  const members = new Map<string, ZipMember>();
  for (let index = 0; index < count; index += 1) {
    if (archive.readUInt32LE(cursor) !== 0x02014b50) throw new Error('bad central directory entry');
    const flags = archive.readUInt16LE(cursor + 8);
    const method = archive.readUInt16LE(cursor + 10);
    const crc = archive.readUInt32LE(cursor + 16);
    const size = archive.readUInt32LE(cursor + 20);
    const nameLength = archive.readUInt16LE(cursor + 28);
    const extraLength = archive.readUInt16LE(cursor + 30);
    const commentLength = archive.readUInt16LE(cursor + 32);
    const local = archive.readUInt32LE(cursor + 42);
    const name = archive.subarray(cursor + 46, cursor + 46 + nameLength).toString('ascii');
    if (archive.readUInt32LE(local) !== 0x04034b50) throw new Error('bad local header');
    const start = local + 30 + archive.readUInt16LE(local + 26) + archive.readUInt16LE(local + 28);
    members.set(name, { data: archive.subarray(start, start + size), method, flags, crc });
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  return members;
}

/**
 * Synthetic provisioning. It is expired and names no real endpoint, so the library's own
 * validation refuses it before any network I/O. The sentinel values must never reach a response.
 */
export const SYNTHETIC_PROVISIONING = {
  expiresAt: 0,
  accountUid: 'synthetic-account',
  peer: 'synthetic-peer',
  localKey: 'PRIVATE-KKKKKKKK',
  password: 'PRIVATE-PPPPPPPP',
  motoId: 'synthetic',
  preconnect: false,
  iceTokens: [],
  tcpToken: { credential: 'PRIVATE-RRRRRRRR', username: 'synthetic', domain: 'relay.invalid', urls: ['tcp4:relay.invalid:1'] },
  mqtt: { host: 'broker.invalid', port: 8883, clientId: 'synthetic/mb/synthetic-account', username: 'synthetic', password: 'PRIVATE-MMMMMMMM' },
  mqttHeader: '000000000000000000000000',
  subscribeTopics: ['synthetic/synthetic-peer'],
  publishTopic: 'smart/mb/out/synthetic-peer',
};
export const PROVISIONING_SECRETS = ['PRIVATE-KKKKKKKK', 'PRIVATE-PPPPPPPP', 'PRIVATE-RRRRRRRR', 'PRIVATE-MMMMMMMM', 'relay.invalid'];

/** Writes one provisioning file with an exact mode, independent of the umask. */
export async function writeProvisioning(directory: string, content: unknown = SYNTHETIC_PROVISIONING, mode = 0o600, name = 'map-provisioning.json'): Promise<string> {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const path = join(directory, name);
  await writeFile(path, typeof content === 'string' ? content : JSON.stringify(content), { mode });
  await chmod(path, mode);
  return path;
}

/**
 * Scriptable stand-in for the library's `PortableMapAcquisition`: a test publishes complete
 * snapshots and ends the demand, or the demand's signal ends it with `aborted`.
 */
export class FakeMapAcquisition implements MapAcquisitionPort {
  readonly provisioning: unknown;
  readonly demands: MapAcquisitionDemand[] = [];
  shutdowns = 0;
  cleared = 0;
  failShutdown = false;
  onAbort: { cancellationConfirmed: boolean; cleanupConfirmed: boolean } = { cancellationConfirmed: true, cleanupConfirmed: true };
  ended: MapAcquisitionEnd | undefined;
  #snapshot: MapAcquisitionSnapshot | undefined;
  #revision = 0;
  #finish: ((result: MapAcquisitionResult) => void) | undefined;

  constructor(provisioning: MapSessionProvisioning) {
    this.provisioning = structuredClone(provisioning);
  }

  get lastComplete(): MapAcquisitionSnapshot | undefined {
    const snapshot = this.#snapshot;
    return snapshot && { revision: snapshot.revision, receivedAt: snapshot.receivedAt, files: copyFiles(snapshot.files) };
  }

  get running(): boolean {
    return this.#finish !== undefined;
  }

  acquire(demand: MapAcquisitionDemand = {}): Promise<MapAcquisitionResult> {
    this.demands.push(demand);
    return new Promise((resolve) => {
      this.#finish = resolve;
      const abort = () => this.end('aborted', this.onAbort);
      demand.signal?.addEventListener('abort', abort, { once: true });
      if (demand.signal?.aborted) abort();
    });
  }

  /** One complete snapshot, retained as the library would until a later one replaces it. */
  publish(files: Readonly<Record<MapStreamName, Uint8Array>>, receivedAt: number): void {
    this.#revision += 1;
    this.#snapshot = { revision: this.#revision, receivedAt, files: copyFiles(files) };
  }

  end(reason: MapAcquisitionEnd, confirmations: Pick<MapAcquisitionResult, 'cancellationConfirmed' | 'cancellationFailure' | 'cleanupConfirmed'> = { cancellationConfirmed: true, cleanupConfirmed: true }): void {
    const finish = this.#finish;
    if (!finish) return;
    this.#finish = undefined;
    this.ended = reason;
    const last = this.lastComplete;
    finish({ reason, ...confirmations, ...(last ? { lastComplete: last } : {}) });
  }

  async shutdown(): Promise<void> {
    this.shutdowns += 1;
    if (this.failShutdown) throw new EufyError('shutdown_incomplete');
  }

  clearLastComplete(): void {
    this.cleared += 1;
    this.#snapshot = undefined;
  }
}

export interface FakeMaps {
  create: CreateMapAcquisition;
  created: FakeMapAcquisition[];
  latest: () => FakeMapAcquisition;
}

export function fakeMaps(): FakeMaps {
  const created: FakeMapAcquisition[] = [];
  return {
    created,
    create: (provisioning) => {
      const acquisition = new FakeMapAcquisition(provisioning);
      created.push(acquisition);
      return acquisition;
    },
    latest: () => {
      const last = created.at(-1);
      if (!last) throw new Error('no acquisition was created');
      return last;
    },
  };
}

export async function waitFor(condition: () => boolean, what: string, timeoutMs = 2_000): Promise<void> {
  const until = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
