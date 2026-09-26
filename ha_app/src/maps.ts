import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { crc32 } from 'node:zlib';
import {
  EufyError,
  PortableMapAcquisition,
  decodeMowerMapSnapshot,
  type MapAcquisitionDemand,
  type MapAcquisitionEnd,
  type MapAcquisitionResult,
  type MapAcquisitionSnapshot,
  type MapSessionProvisioning,
  type MapStreamName,
} from '@keesmod/eufy-mega-client';
import { ApiError, BridgeError } from './errors.ts';
import { RawReply } from './server.ts';

/** Media type of the map bundle, the one the integration validates for every compatible map source. */
export const MAP_CONTENT_TYPE = 'application/vnd.eufy-robomow-map+zip';
/** The three transport files the library retains, in the order of the manifest's snapshot digest. */
export const MAP_FILES: readonly MapStreamName[] = ['map.bin.stream', 'cleanPath.bin.stream', 'navPath.bin.stream'];
/** The integration refuses larger stream files. The library retains up to 8 MiB per file. */
export const MAP_FILE_MAX_BYTES = 5 * 1024 * 1024;
export const MAP_MANIFEST_SCHEMA = 1;
/** The library refuses larger provisioning. */
export const MAX_PROVISIONING_BYTES = 65_536;
/** One demand, the library's default and the lease of the retained map source. */
export const DEFAULT_MAP_DEMAND_MS = 30_000;
/** A stream request keeps demands running this long, as the retained map source did. */
export const DEFAULT_MAP_STREAM_LEASE_MS = 30_000;
/** Without a stream lease, at most one demand per interval refreshes the idle map. */
export const DEFAULT_MAP_IDLE_INTERVAL_MS = 5 * 60_000;
/** After a demand that published nothing, the next one waits this long. */
export const DEFAULT_MAP_RETRY_INTERVAL_MS = 60_000;
/** How often a running demand's retained files are checked for a newer complete snapshot. */
export const DEFAULT_MAP_WATCH_INTERVAL_MS = 1_000;

export type MapMode = 'idle' | 'stream';

/** The `X-Eufy-Map-Mode` request header. Absent means idle. */
export function mapMode(value: string | undefined): MapMode {
  if (value === undefined || value === '' || value === 'idle') return 'idle';
  if (value === 'stream') return 'stream';
  throw new ApiError(400, 'invalid_map_mode');
}

function errorCode(error: unknown): string {
  if (error instanceof EufyError || error instanceof BridgeError) return error.code;
  return 'internal_error';
}

function iso(time: number): string {
  return new Date(time).toISOString();
}

/** One served bundle. The body is private lawn geometry and is never logged or put in a state document. */
export interface MapBundle {
  body: Buffer;
  etag: string;
  snapshotId: string;
  /** The mower id the manifest binds the bundle to. */
  deviceId: string;
  /** The library's local receipt time of the snapshot in epoch milliseconds. */
  receivedAt: number;
}

/** MS-DOS date of 1980-01-01 00:00, the earliest ZIP time, so identical input gives identical bytes. */
const ZIP_EPOCH_DATE = (1 << 5) | 1;

/** A ZIP archive whose members are stored without compression, the only form the integration accepts. */
function storedZip(members: readonly (readonly [string, Uint8Array])[]): Buffer {
  const parts: Uint8Array[] = [];
  const directory: Buffer[] = [];
  let offset = 0;
  for (const [name, data] of members) {
    const encodedName = Buffer.from(name, 'ascii');
    const checksum = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(ZIP_EPOCH_DATE, 12);
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(encodedName.length, 26);
    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(20, 4);
    entry.writeUInt16LE(20, 6);
    entry.writeUInt16LE(ZIP_EPOCH_DATE, 14);
    entry.writeUInt32LE(checksum, 16);
    entry.writeUInt32LE(data.length, 20);
    entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(encodedName.length, 28);
    entry.writeUInt32LE(offset, 42);
    parts.push(local, encodedName, data);
    directory.push(entry, encodedName);
    offset += local.length + encodedName.length + data.length;
  }
  const central = Buffer.concat(directory);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(members.length, 8);
  end.writeUInt16LE(members.length, 10);
  end.writeUInt32LE(central.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, central, end]);
}

/** SHA-256 over every file name, its length as eight big-endian bytes and its bytes, in manifest order. */
export function snapshotDigest(files: Readonly<Record<MapStreamName, Uint8Array>>): string {
  const digest = createHash('sha256');
  for (const name of MAP_FILES) {
    const payload = files[name];
    const length = Buffer.alloc(8);
    length.writeBigUInt64BE(BigInt(payload.length));
    digest.update(name, 'ascii').update(length).update(payload);
  }
  return digest.digest('hex');
}

/**
 * The map bundle the integration already validates for any compatible source: a stored ZIP with
 * `manifest.json` and the three transport files exactly as the library retained them. The
 * manifest binds the bundle to one mower id, dates it in whole seconds from the library's
 * receipt time and names every file's size and SHA-256. Identical input gives identical bytes,
 * so the entity tag is the snapshot digest plus that second.
 */
export function buildMapBundle(deviceId: string, snapshot: MapAcquisitionSnapshot): MapBundle {
  const capturedAt = Math.floor(snapshot.receivedAt / 1000);
  const snapshotId = snapshotDigest(snapshot.files);
  const files: Record<string, { size: number; sha256: string }> = {};
  for (const name of MAP_FILES) {
    const payload = snapshot.files[name];
    files[name] = { size: payload.length, sha256: createHash('sha256').update(payload).digest('hex') };
  }
  const manifest = Buffer.from(
    JSON.stringify({ schema_version: MAP_MANIFEST_SCHEMA, device_id: deviceId, captured_at: capturedAt, snapshot_id: snapshotId, files }),
    'utf8',
  );
  const body = storedZip([['manifest.json', manifest], ...MAP_FILES.map((name) => [name, snapshot.files[name]] as const)]);
  return { body, etag: `"${snapshotId}-${capturedAt}"`, snapshotId, deviceId, receivedAt: snapshot.receivedAt };
}

export type MapSnapshotCheck =
  | { ok: true; fullPath: boolean }
  | { ok: false; code: 'map_file_size' | 'map_undecodable' | 'map_boundary_missing' };

/**
 * The gate before a snapshot replaces the served bundle, on the library's decoder. Every file is
 * within the integration's size limit, all three decode without a fault, the map file carries a
 * realtime map with at least one region and no degenerate region boundary. `fullPath` is true when
 * the cleaning path is a `history` or `complete` path rather than the empty `realtime` placeholder
 * that opens a demand. Only this outcome leaves the function, never geometry.
 */
export function checkMapSnapshot(snapshot: MapAcquisitionSnapshot): MapSnapshotCheck {
  for (const name of MAP_FILES) {
    const size = snapshot.files[name]?.length ?? 0;
    if (size === 0 || size > MAP_FILE_MAX_BYTES) return { ok: false, code: 'map_file_size' };
  }
  const geometry = decodeMowerMapSnapshot(snapshot);
  const map = geometry.map;
  const path = geometry.cleaningPath;
  if (geometry.faults.length > 0 || !map || !path || !geometry.navigationPose) return { ok: false, code: 'map_undecodable' };
  if (map.regions.length === 0 || map.issues.includes('degenerate_region_boundary')) return { ok: false, code: 'map_boundary_missing' };
  return { ok: true, fullPath: path.kind === 'history' || path.kind === 'complete' };
}

/** Group may not write the provisioning file and others may not access it. */
const PROVISIONING_FORBIDDEN_MODE = 0o027;

/**
 * Reads the operator's provisioning file afresh for one demand, so the operator can replace it
 * while the bridge runs. It must be a regular file of at most 64 KiB holding one JSON object,
 * writable by its owner only and not readable by others. The content is private: it is handed to
 * the library and never logged, served or kept.
 */
export async function readProvisioningFile(path: string): Promise<unknown> {
  let handle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NONBLOCK);
  } catch {
    throw new BridgeError('map_provisioning_unreadable');
  }
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size === 0 || info.size > MAX_PROVISIONING_BYTES) throw new BridgeError('map_provisioning_unreadable');
    if ((info.mode & PROVISIONING_FORBIDDEN_MODE) !== 0) throw new BridgeError('map_provisioning_insecure');
    const source = await handle.readFile('utf8');
    if (Buffer.byteLength(source, 'utf8') > MAX_PROVISIONING_BYTES) throw new BridgeError('map_provisioning_unreadable');
    const parsed: unknown = JSON.parse(source);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new BridgeError('map_provisioning_unreadable');
    return parsed;
  } catch (error) {
    if (error instanceof BridgeError) throw error;
    throw new BridgeError('map_provisioning_unreadable');
  } finally {
    await handle.close().catch(() => {});
  }
}

/** The part of the library's `PortableMapAcquisition` this bridge uses. Route tests replace it. */
export interface MapAcquisitionPort {
  readonly lastComplete: MapAcquisitionSnapshot | undefined;
  acquire(demand?: MapAcquisitionDemand): Promise<MapAcquisitionResult>;
  shutdown(): Promise<void>;
  clearLastComplete(): void;
}

export type CreateMapAcquisition = (provisioning: MapSessionProvisioning) => MapAcquisitionPort;

/** Production: one library acquisition per demand, validated by its constructor before any I/O. */
export const libraryMapAcquisition: CreateMapAcquisition = (provisioning) => new PortableMapAcquisition(provisioning);

export interface MapTimings {
  demandMs: number;
  streamLeaseMs: number;
  idleIntervalMs: number;
  retryIntervalMs: number;
  watchIntervalMs: number;
}

/** The last demand that reached the library. Counts and library outcomes only, never geometry. */
export interface MapDemandSummary {
  started_at: string;
  ended_at: string;
  end: MapAcquisitionEnd;
  cancellation_confirmed: boolean;
  /** Fixed library failure category only, never protocol payloads or exception text. */
  cancellation_failure?: MapAcquisitionResult['cancellationFailure'];
  cleanup_confirmed: boolean;
  /** Snapshots this demand published. */
  published: number;
  /** Snapshots this demand received that failed the gate. */
  rejected: number;
}

/** Map acquisition status in the bridge state document. It never contains geometry. */
export interface MapStatus {
  /** Receipt time of the served snapshot, or null before the first one. */
  captured_at: string | null;
  /** Age of the served snapshot against the bridge clock. */
  age_ms: number | null;
  /** True when a bundle is served but the last demand failed. */
  stale: boolean;
  /** Stable code of the last failed demand, null after a demand that published and ended cleanly. */
  error: string | null;
  acquiring: boolean;
  /** A stream request arrived within the lease. */
  streaming: boolean;
  last_demand: MapDemandSummary | null;
}

interface Demand {
  id: string;
  controller: AbortController;
  startedAt: number;
  revision: number;
  published: number;
  rejected: number;
  rejection: string | null;
  fullPath: boolean;
  done: Promise<void>;
}

function etagMatches(header: string | undefined, etag: string): boolean {
  if (!header) return false;
  return header.split(',').some((candidate) => {
    const tag = candidate.trim();
    return tag === '*' || (tag.startsWith('W/') ? tag.slice(2) : tag) === etag;
  });
}

/**
 * The read-only map for the one mower the provisioning belongs to. Requests drive acquisition:
 * a request starts at most one demand of the library's `PortableMapAcquisition` when one is due
 * and is answered at once with the last good bundle, never after waiting for the demand. A
 * `stream` request keeps demands running for the lease, otherwise one demand per idle interval
 * refreshes the map and ends once the full cleaning path arrived. Every complete snapshot passes
 * the library's decoder before it replaces the bundle, and a failed demand never removes it.
 * Nothing is replayed. Failed demands back off, and uncertain cancellation or cleanup blocks
 * further acquisition until the bridge restarts.
 */
export class MowerMaps {
  readonly #provision: (id: string, signal: AbortSignal) => Promise<MapSessionProvisioning>;
  readonly #create: CreateMapAcquisition;
  readonly #timings: MapTimings;
  readonly #now: () => number;
  readonly #lifetime: AbortSignal;
  #bundle: MapBundle | undefined;
  #error: string | null = null;
  #active: Demand | undefined;
  #lastStartAt: number | undefined;
  #retryAt = Number.NEGATIVE_INFINITY;
  #leaseUntil = Number.NEGATIVE_INFINITY;
  #disabled = false;
  #lastDemand: MapDemandSummary | null = null;

  constructor(options: {
    provision: (id: string, signal: AbortSignal) => Promise<MapSessionProvisioning>;
    create: CreateMapAcquisition;
    timings: MapTimings;
    now: () => number;
    lifetime: AbortSignal;
  }) {
    this.#provision = options.provision;
    this.#create = options.create;
    this.#timings = options.timings;
    this.#now = options.now;
    this.#lifetime = options.lifetime;
  }

  /** Records the request's mode and starts one demand for this mower when one is due. Never waits. */
  request(id: string, mode: MapMode): void {
    const now = this.#now();
    if (mode === 'stream') this.#leaseUntil = now + this.#timings.streamLeaseMs;
    if (this.#due(id, now)) this.#start(id, now);
  }

  /** The last good bundle for this mower with its age, as `200`, or `304` when the tag still matches. */
  reply(id: string, ifNoneMatch: string | undefined): RawReply {
    const bundle = this.#bundle;
    if (!bundle || bundle.deviceId !== id) throw new ApiError(503, this.#error ?? 'map_unavailable');
    const headers: Record<string, string> = {
      ETag: bundle.etag,
      'X-Eufy-Map-Captured-At': iso(bundle.receivedAt),
      'X-Eufy-Map-Age-Ms': String(Math.max(0, this.#now() - bundle.receivedAt)),
      'X-Eufy-Map-Stale': this.#error === null ? 'false' : 'true',
    };
    if (this.#error !== null) headers['X-Eufy-Map-Error'] = this.#error;
    if (etagMatches(ifNoneMatch, bundle.etag)) return new RawReply(304, headers, null);
    return new RawReply(200, { ...headers, 'Content-Type': MAP_CONTENT_TYPE }, bundle.body);
  }

  status(): MapStatus {
    const now = this.#now();
    const bundle = this.#bundle;
    return {
      captured_at: bundle ? iso(bundle.receivedAt) : null,
      age_ms: bundle ? Math.max(0, now - bundle.receivedAt) : null,
      stale: bundle !== undefined && this.#error !== null,
      error: this.#error,
      acquiring: this.#active !== undefined,
      streaming: now < this.#leaseUntil,
      last_demand: this.#lastDemand ? { ...this.#lastDemand } : null,
    };
  }

  /** Waits for the running demand, which the bridge lifetime already aborted, then drops the bundle. */
  async close(): Promise<void> {
    await this.#active?.done;
    this.#bundle = undefined;
  }

  #due(id: string, now: number): boolean {
    if (this.#disabled || this.#active || this.#lifetime.aborted || now < this.#retryAt) return false;
    if (now < this.#leaseUntil) return true;
    const last = this.#lastStartAt;
    return this.#bundle?.deviceId !== id || last === undefined || now - last >= this.#timings.idleIntervalMs;
  }

  #start(id: string, now: number): void {
    const demand: Demand = {
      id,
      controller: new AbortController(),
      startedAt: now,
      revision: 0,
      published: 0,
      rejected: 0,
      rejection: null,
      fullPath: false,
      done: Promise.resolve(),
    };
    this.#lastStartAt = now;
    this.#active = demand;
    demand.done = this.#run(demand).finally(() => {
      if (this.#active === demand) this.#active = undefined;
    });
  }

  async #run(demand: Demand): Promise<void> {
    let maps: MapAcquisitionPort;
    const signal = AbortSignal.any([this.#lifetime, demand.controller.signal]);
    try {
      const provisioning = await this.#provision(demand.id, signal);
      if (signal.aborted) throw new BridgeError('request_aborted');
      maps = this.#create(provisioning);
    } catch (error) {
      this.#finish(errorCode(error));
      return;
    }
    const watch = setInterval(() => this.#watch(demand, maps), this.#timings.watchIntervalMs);
    let result: MapAcquisitionResult | undefined;
    let failure: string | null = null;
    try {
      result = await maps.acquire({ demandMs: this.#timings.demandMs, signal });
    } catch (error) {
      failure = errorCode(error);
    } finally {
      clearInterval(watch);
    }
    if (result?.lastComplete) this.#consider(demand, result.lastComplete);
    let cleanupConfirmed = result?.cleanupConfirmed ?? true;
    try {
      maps.clearLastComplete();
    } catch {
      // Only a running demand refuses, and this one has ended.
    }
    try {
      await maps.shutdown();
    } catch {
      cleanupConfirmed = false;
    }
    if (result) {
      this.#lastDemand = {
        started_at: iso(demand.startedAt),
        ended_at: iso(this.#now()),
        end: result.reason,
        cancellation_confirmed: result.cancellationConfirmed,
        ...(result.cancellationFailure ? { cancellation_failure: result.cancellationFailure } : {}),
        cleanup_confirmed: cleanupConfirmed,
        published: demand.published,
        rejected: demand.rejected,
      };
    }
    if (!cleanupConfirmed) {
      // The library refuses further acquisition on an instance without confirmed cleanup. The
      // bridge stops acquiring as well until it restarts, the last good bundle stays served.
      this.#disabled = true;
      failure = 'mower_map_cleanup_unconfirmed';
    } else if (result?.reason === 'cancel_unconfirmed') {
      // Closing our sockets does not confirm the peer stopped its transfer. Do not start
      // another demand after uncertain cancellation, even when this one produced a map.
      this.#disabled = true;
      failure = 'mower_map_cancel_unconfirmed';
    } else if (failure === null && result && !['demand_expired', 'aborted', 'disconnected', 'shutdown', 'stream_ended'].includes(result.reason)) {
      failure = `mower_map_${result.reason}`;
    }
    if (failure === null && demand.published === 0) failure = result ? endCode(demand, result.reason) : 'internal_error';
    this.#finish(failure);
  }

  #watch(demand: Demand, maps: MapAcquisitionPort): void {
    let snapshot: MapAcquisitionSnapshot | undefined;
    try {
      snapshot = maps.lastComplete;
    } catch {
      return;
    }
    if (snapshot) this.#consider(demand, snapshot);
    // Without a stream lease one full cleaning path is enough, as the retained source closed its
    // idle stream after one complete snapshot. A realtime placeholder alone keeps the demand open.
    if (demand.fullPath && this.#now() >= this.#leaseUntil) demand.controller.abort();
  }

  /** Publishes a newer snapshot of this demand when it passes the gate. Zeroes the copy either way. */
  #consider(demand: Demand, snapshot: MapAcquisitionSnapshot): void {
    try {
      if (snapshot.revision <= demand.revision) return;
      demand.revision = snapshot.revision;
      const check = checkMapSnapshot(snapshot);
      if (!check.ok) {
        demand.rejected += 1;
        demand.rejection = check.code;
        return;
      }
      const bundle = buildMapBundle(demand.id, snapshot);
      const current = this.#bundle;
      if (!current || current.deviceId !== demand.id || bundle.receivedAt >= current.receivedAt) this.#bundle = bundle;
      demand.published += 1;
      if (check.fullPath) demand.fullPath = true;
    } catch {
      demand.rejected += 1;
      demand.rejection = 'map_undecodable';
    } finally {
      for (const name of MAP_FILES) snapshot.files[name]?.fill(0);
    }
  }

  #finish(failure: string | null): void {
    if (failure === null) {
      this.#error = null;
      this.#retryAt = Number.NEGATIVE_INFINITY;
      return;
    }
    this.#error = failure;
    this.#retryAt = this.#now() + this.#timings.retryIntervalMs;
  }
}

/** Why a demand that reached the library published nothing, as a stable code. */
function endCode(demand: Demand, reason: MapAcquisitionEnd): string {
  if (demand.rejection) return demand.rejection;
  if (reason === 'demand_expired') return 'mower_map_incomplete';
  if (reason === 'aborted' || reason === 'shutdown' || reason === 'disconnected') return 'request_aborted';
  return `mower_map_${reason}`;
}
