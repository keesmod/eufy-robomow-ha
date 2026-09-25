import type { AddressInfo } from 'node:net';
import type { Server } from 'node:http';
import {
  EufyClient,
  EufyError,
  decodeMowerSettings,
  type AuthState,
  type ModuleLifecycleState,
  type MowerActivity,
  type MowerAdapter,
  type MowerAdapterContext,
  type MowerCommandEnd,
  type MowerCommandKind,
  type MowerCommandOutcome,
  type MowerCommandStage,
  type MowerDevice,
  type MowerHomeOptions,
  type MowerLocalSession,
  type MowerLocalSessionOptions,
  type MowerModule,
  type MowerNetworkKind,
  type MowerSettingEnd,
  type MowerSettingName,
  type MowerSettingOutcome,
  type MowerSettings,
  type MowerSettingStage,
  type MowerTelemetry,
  type MowerTelemetryField,
} from '@keesmod/eufy-mega-client';
import { MOWER_ID, SETTINGS_MODE_WRITE, type BridgeConfig, type ControlConfig } from './config.ts';
import { ApiError, BridgeError } from './errors.ts';
import {
  DEFAULT_MAP_DEMAND_MS,
  DEFAULT_MAP_IDLE_INTERVAL_MS,
  DEFAULT_MAP_RETRY_INTERVAL_MS,
  DEFAULT_MAP_STREAM_LEASE_MS,
  DEFAULT_MAP_WATCH_INTERVAL_MS,
  MowerMaps,
  libraryMapAcquisition,
  mapMode,
  type CreateMapAcquisition,
  type MapMode,
  type MapStatus,
} from './maps.ts';
import { createPrivateServer, type MapRequest, type RawReply } from './server.ts';
import { MowerSessionFile, PrivateDirectory, bridgeIdentity } from './storage.ts';
import { BRIDGE_NAME, BRIDGE_VERSION, CLIENT_PACKAGE, CLIENT_VERSION, PROTOCOL_VERSION } from './version.ts';

export const DEFAULT_STARTUP_TIMEOUT_MS = 10_000;
export const DEFAULT_CONNECT_TIMEOUT_MS = 30_000;
export const DEFAULT_SHUTDOWN_TIMEOUT_MS = 15_000;
export const DEFAULT_DISCOVERY_TIMEOUT_MS = 30_000;
/** How long in-flight responses may finish after the listener closes at shutdown. */
export const SERVER_CLOSE_GRACE_MS = 1_000;
/** Age after which a request may trigger a new discovery. */
export const DEFAULT_DISCOVERY_CACHE_MS = 10 * 60_000;
/** Minimum spacing between discovery attempts, so failing requests cannot hammer the cloud. */
export const DEFAULT_DISCOVERY_INTERVAL_MS = 60_000;
/**
 * Minimum spacing between automatic re-authentication attempts after an attempt failed for a
 * transient reason. A session that was good and has lapsed is renewed at once.
 */
export const DEFAULT_REAUTH_INTERVAL_MS = 60_000;
/** Sign-in outcomes that need the operator. They are never repeated automatically. */
const REFUSED_SIGN_IN_CODES: ReadonlySet<string> = new Set([
  'authentication_failed',
  'mower_authentication_failed',
  'mower_invalid_options',
  'mower_region_unsupported',
]);
const REFUSED_SIGN_IN_STATES: ReadonlySet<string> = new Set(['captcha_required', 'verification_required', 'locked']);

/** Whether a recorded attempt was refused in a way that only the operator can resolve. */
export function signInRefused(status: { state: string; last_error: string | null }): boolean {
  return REFUSED_SIGN_IN_STATES.has(status.state) || (status.last_error !== null && REFUSED_SIGN_IN_CODES.has(status.last_error));
}

export { ApiError, BridgeError } from './errors.ts';

/**
 * Command classes with a route. `stop` writes DP 1 `switch_go` false: on the owned E15 with firmware
 * 6.9.28 it ends the task and the mower returns to the dock by itself, reflected by the map-saving
 * payload at dock arrival (library receipt of 2026-09-20). `return` is declared by the library but
 * DP 3 was ignored from `paused` and from the stopped task, so it answers `command_unsupported`.
 */
export const ROUTED_COMMAND_CLASSES = ['start', 'pause', 'resume', 'stop'] as const satisfies readonly MowerCommandKind[];
const COMMAND_CLASSES: readonly string[] = ['start', 'pause', 'resume', 'stop', 'return'] satisfies readonly MowerCommandKind[];
type RoutedCommandClass = (typeof ROUTED_COMMAND_CLASSES)[number];

/**
 * The settings the state route reads, keyed in snake case, with the library setting each one
 * serves. The library writes only the four in `WRITABLE_SETTING_KEYS`. Rain and child protection
 * and the bird-view capture are read only in the library itself, whatever the settings mode.
 */
export const SETTING_KEYS = {
  mow_height: 'mowHeight',
  volume: 'volume',
  smart_no_go_zones: 'smartNoGoZones',
  sparse_lawn_optimization: 'sparseLawnOptimization',
  rain_auto_return: 'rainAutoReturn',
  child_lock: 'childLock',
  bird_view_capture: 'birdViewCapture',
} as const satisfies Record<string, MowerSettingName>;
export type SettingKey = keyof typeof SETTING_KEYS;
export const WRITABLE_SETTING_KEYS = ['mow_height', 'volume', 'smart_no_go_zones', 'sparse_lawn_optimization'] as const satisfies readonly SettingKey[];
/** A switch value is `true` or `false`, a number setting a plain integer. Nothing else reaches the library. */
const SETTING_VALUE = /^(true|false|-?\d{1,6})$/;

/**
 * One setting of one state query, as the library decoded it. Only `reported` carries a value.
 * `writable` means the library writes it and the device declares it writable. The route itself
 * also needs `settings_mode: write`, reported in `routes.settings`.
 */
export type SettingDocument =
  | { state: 'reported'; value: boolean | number; writable: boolean; min?: number; max?: number; step?: number; unit?: string }
  | { state: 'missing' }
  | { state: 'invalid' };

export type OpenLocalSession = (
  id: string,
  options: MowerLocalSessionOptions,
  signal: AbortSignal,
) => Promise<MowerLocalSession>;

/** Test seams and bounds. Production uses the library's own Home/Tuya adapter, sessions and global fetch. */
export interface BridgeDependencies {
  adapter?: (context: MowerAdapterContext) => MowerAdapter;
  fetch?: typeof fetch;
  /** Replaces `mowers.openLocalSession` so route tests need no LAN peer. */
  openLocalSession?: OpenLocalSession;
  /** Replaces `new PortableMapAcquisition(provisioning)` so map tests need no relay peer. */
  mapAcquisition?: CreateMapAcquisition;
  startupTimeoutMs?: number;
  connectTimeoutMs?: number;
  shutdownTimeoutMs?: number;
  discoveryTimeoutMs?: number;
  discoveryCacheMs?: number;
  discoveryIntervalMs?: number;
  reauthIntervalMs?: number;
  /** Receives one line per automatic re-authentication. Never receives credentials or session data. */
  log?: (level: 'info' | 'warn', message: string) => void;
  mapDemandMs?: number;
  mapStreamLeaseMs?: number;
  mapIdleIntervalMs?: number;
  mapRetryIntervalMs?: number;
  mapWatchIntervalMs?: number;
  now?: () => number;
}

export interface DiscoveredMower {
  id: string;
  kind: 'mower';
  model: 'E15';
  productCode: 'T2880';
  /** True when a LAN host is configured for this id, so the state route can serve it. */
  state_available: boolean;
}

/** Contract 1 of `GET /v1/mowers`. Never contains a local key, session, host or raw response. */
export interface DiscoveryDocument {
  contract: 1;
  discovered_at: string;
  /** False once the list is older than the cache age or the last refresh failed. */
  fresh: boolean;
  /** Stable code of the last failed refresh while an older list is still served. */
  error: string | null;
  mowers: DiscoveredMower[];
}

/** The typed fields and settings of one query. Raw data points are deliberately absent. */
export interface TelemetryFields {
  observed_at: string;
  status: MowerTelemetryField<MowerActivity>;
  battery: MowerTelemetryField<{ percent: number }>;
  progress: MowerTelemetryField<{ percent: number }>;
  network: MowerTelemetryField<{ kind?: MowerNetworkKind; signalDbm?: number; signalPercent?: number }>;
  /** The library's typed settings from the same query, since bridge 0.10.0. */
  settings: Record<SettingKey, SettingDocument>;
}

/**
 * The command that owns a mower while its read-back runs, with the library's progress so far:
 * the acknowledgement time and the latest confirmed DP 107 activity. After a `stop` on the owned
 * E15 that activity is `returning` within a second, long before the outcome at the dock arrival.
 */
export interface InFlightCommandDocument {
  command: RoutedCommandClass;
  acknowledged_at: string | null;
  activity: { observed_at: string; sequence: number; value: MowerActivity } | null;
}

/** Contract 1 of `GET /v1/mowers/{id}/state`. Freshness is measured against the bridge clock. */
export interface MowerStateDocument extends TelemetryFields {
  contract: 1;
  id: string;
  source: 'local-tuya-3.5';
  /** Milliseconds between the observation and this response, or null when the observation time is unreadable. */
  age_ms: number | null;
  /** True when this is the last good query served after the current query failed. */
  stale: boolean;
  /** Stable code of the failed query when stale, otherwise null. */
  error: string | null;
  /** The command running for this mower now, or null. Never a finished or replayed command. */
  command: InFlightCommandDocument | null;
}

/** Contract 1 of `POST /v1/mowers/{id}/commands/{class}`. Raw reports and data points are never served. */
export interface MowerCommandDocument {
  contract: 1;
  id: string;
  command: MowerCommandKind;
  /**
   * `confirmed` when a fresh report reflected the expected activity, or the map-saving payload for
   * `stop`, `failed` when the device rejected the control frame, `uncertain` when the bound passed
   * or the report limit was reached after the write. An uncertain command was written and must
   * never be repeated automatically.
   */
  result: 'confirmed' | 'failed' | 'uncertain';
  write: { dp: string; code: string; value: boolean };
  sent_at: string;
  stage: MowerCommandStage;
  end: MowerCommandEnd;
  /** Observation time of the fresh status query the library ran before the write. */
  before_observed_at: string;
  reply: { observed_at: string; return_code_zero: boolean; rejected: boolean } | null;
  acknowledgement: { observed_at: string; sequence: number; dp: string } | null;
  activity: { observed_at: string; sequence: number; value: MowerActivity } | null;
  /**
   * The DP 107 payload that reflected a `stop`, the map-saving payload. On the owned E15 it marks
   * the dock arrival about 30 seconds after the write, because a stop ends the task and the mower
   * returns to the dock by itself. `stop` has no `activity`, every other class has no `payload`.
   */
  payload: { observed_at: string; sequence: number; name: 'map_saving' } | null;
  /** Number of fresh reports received during the read-back. */
  reports: number;
}

/** Contract 1 of `POST /v1/mowers/{id}/settings/{key}`. Raw reports and data points are never served. */
export interface MowerSettingDocument {
  contract: 1;
  id: string;
  setting: SettingKey;
  /**
   * `confirmed` when a fresh report carried the written value, `failed` when the device rejected
   * the control frame, `uncertain` when the bound passed or the report limit was reached after the
   * write. An uncertain write happened and must never be repeated automatically.
   */
  result: 'confirmed' | 'failed' | 'uncertain';
  write: { dp: string; code: string; value: boolean | number };
  /** The value on the library's fresh query before the write, the value a deliberate restore writes back. */
  previous: boolean | number;
  sent_at: string;
  stage: MowerSettingStage;
  end: MowerSettingEnd;
  /** Observation time of the fresh status query the library ran before the write. */
  before_observed_at: string;
  reply: { observed_at: string; return_code_zero: boolean; rejected: boolean } | null;
  /** The first fresh report that carried the written value. */
  reflection: { observed_at: string; sequence: number; value: boolean | number } | null;
  /** The latest fresh report that carried another value. A value of another type is served as null. */
  other: { observed_at: string; sequence: number; value: boolean | number | null } | null;
  /** Number of fresh reports received during the read-back. */
  reports: number;
}

export type BridgeLifecycle = 'created' | 'starting' | 'running' | 'stopping' | 'stopped';

/**
 * Authentication summary. `state` follows the library: a session that has lapsed reads as
 * `disconnected`. Captcha images and verification prompts are not exposed by this version.
 */
export interface AuthStatus {
  state: AuthState['state'];
  /** Stable library or bridge error code of the last attempt, or null when it succeeded. */
  last_error: string | null;
  /** Time of the last attempt, the startup attempt or an automatic re-authentication. */
  attempted_at: string | null;
}

export interface BridgeState {
  protocol: typeof PROTOCOL_VERSION;
  bridge: typeof BRIDGE_NAME;
  version: string;
  bridge_id: string | null;
  lifecycle: BridgeLifecycle;
  operating_mode: BridgeConfig['operatingMode'];
  auth: AuthStatus;
  client: {
    package: typeof CLIENT_PACKAGE;
    version: string;
    module: 'mowers';
    lifecycle: ModuleLifecycleState;
    connected: boolean;
  };
  mowers: { count: number | null; discovered_at: string | null; error: string | null };
  /**
   * `control` is true only in `control` mode, `maps` only with a map provisioning file and
   * `settings` only with `settings_mode: write`.
   */
  routes: { discovery: true; state: true; control: boolean; maps: boolean; settings: boolean };
  /** The command opt-in in effect, or null in `observe_only`. The stop route text is not served. */
  control: { classes: RoutedCommandClass[]; max_state_age_ms: number; read_back_ms: number } | null;
  /** Map acquisition status without any geometry, or null without map provisioning. */
  maps: MapStatus | null;
}

interface Deadline {
  signal: AbortSignal;
  expired: () => boolean;
  clear: () => void;
}

function deadline(ms: number): Deadline {
  const controller = new AbortController();
  let expired = false;
  const timer = setTimeout(() => {
    expired = true;
    controller.abort();
  }, ms);
  return { signal: controller.signal, expired: () => expired, clear: () => clearTimeout(timer) };
}

function listen(server: Server, port: number, host: string, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      server.off('error', onError);
      server.off('listening', onListening);
      signal.removeEventListener('abort', onAbort);
    };
    const onError = (error: NodeJS.ErrnoException) => {
      cleanup();
      reject(new BridgeError(error.code === 'EADDRINUSE' ? 'address_in_use' : 'listen_failed'));
    };
    const onListening = () => {
      cleanup();
      resolve();
    };
    const onAbort = () => {
      cleanup();
      reject(new BridgeError('startup_timeout'));
    };
    if (signal.aborted) {
      onAbort();
      return;
    }
    server.once('error', onError);
    server.once('listening', onListening);
    signal.addEventListener('abort', onAbort, { once: true });
    server.listen({ port, host });
  });
}

function errorCode(error: unknown): string {
  if (error instanceof EufyError || error instanceof BridgeError) return error.code;
  return 'internal_error';
}

function commandDocument(id: string, outcome: MowerCommandOutcome): MowerCommandDocument {
  const result = outcome.end === 'reflected' ? 'confirmed' : outcome.end === 'rejected' ? 'failed' : 'uncertain';
  return {
    contract: 1,
    id,
    command: outcome.command,
    result,
    write: { dp: outcome.write.dp, code: outcome.write.code, value: outcome.write.value },
    sent_at: outcome.sentAt,
    stage: outcome.stage,
    end: outcome.end,
    before_observed_at: outcome.before.observedAt,
    reply: outcome.reply
      ? { observed_at: outcome.reply.observedAt, return_code_zero: outcome.reply.returnCodeZero, rejected: outcome.reply.rejected }
      : null,
    acknowledgement: outcome.acknowledgement
      ? { observed_at: outcome.acknowledgement.observedAt, sequence: outcome.acknowledgement.sequence, dp: outcome.acknowledgement.dp }
      : null,
    activity: outcome.activity
      ? { observed_at: outcome.activity.observedAt, sequence: outcome.activity.sequence, value: outcome.activity.value }
      : null,
    payload: outcome.payload
      ? { observed_at: outcome.payload.observedAt, sequence: outcome.payload.sequence, name: outcome.payload.name }
      : null,
    reports: outcome.reports.length,
  };
}

/** The library's typed settings as the state route serves them. */
function settingsDocument(decoded: MowerSettings): Record<SettingKey, SettingDocument> {
  const result = {} as Record<SettingKey, SettingDocument>;
  for (const [key, name] of Object.entries(SETTING_KEYS) as [SettingKey, MowerSettingName][]) {
    const field = decoded.settings[name];
    if (field.state !== 'reported') {
      result[key] = { state: field.state };
      continue;
    }
    const setting: SettingDocument = { state: 'reported', value: field.value, writable: field.writable };
    if (field.min !== undefined) setting.min = field.min;
    if (field.max !== undefined) setting.max = field.max;
    if (field.step !== undefined) setting.step = field.step;
    if (field.unit !== undefined) setting.unit = field.unit;
    result[key] = setting;
  }
  return result;
}

function settingValue(raw: string | null): boolean | number | undefined {
  if (raw === null || !SETTING_VALUE.test(raw)) return undefined;
  return raw === 'true' ? true : raw === 'false' ? false : Number(raw);
}

function settingOutcomeDocument(id: string, key: SettingKey, outcome: MowerSettingOutcome): MowerSettingDocument {
  const result = outcome.end === 'reflected' ? 'confirmed' : outcome.end === 'rejected' ? 'failed' : 'uncertain';
  const other = outcome.other;
  return {
    contract: 1,
    id,
    setting: key,
    result,
    write: { dp: outcome.write.dp, code: outcome.write.code, value: outcome.write.value },
    previous: outcome.previous,
    sent_at: outcome.sentAt,
    stage: outcome.stage,
    end: outcome.end,
    before_observed_at: outcome.before.observedAt,
    reply: outcome.reply
      ? { observed_at: outcome.reply.observedAt, return_code_zero: outcome.reply.returnCodeZero, rejected: outcome.reply.rejected }
      : null,
    reflection: outcome.reflection
      ? { observed_at: outcome.reflection.observedAt, sequence: outcome.reflection.sequence, value: outcome.reflection.value }
      : null,
    other: other
      ? {
          observed_at: other.observedAt,
          sequence: other.sequence,
          value: typeof other.value === 'boolean' || typeof other.value === 'number' ? other.value : null,
        }
      : null,
    reports: outcome.reports.length,
  };
}

/** Stops accepting connections, lets in-flight responses finish briefly, then forces the rest closed. */
function closeServer(server: Server, graceMs: number): Promise<void> {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve) => {
    const force = setTimeout(() => server.closeAllConnections(), graceMs);
    server.close(() => {
      clearTimeout(force);
      resolve();
    });
    server.closeIdleConnections();
  });
}

/**
 * Owns exactly one library client with only the mower module, one private session file, one
 * identity and one private HTTP server. Startup, one explicit authentication attempt and
 * shutdown are each bounded. The library's cloud session is a bounded reuse window, so a route
 * that needs it renews a lapsed session through one bounded attempt, spaced after a failure and
 * never after a refused sign-in. Commands exist only behind the `control` opt-in and setting writes
 * only behind `settings_mode: write`. One write owns a mower at a time and nothing is retried or
 * replayed.
 */
export class MowerBridge {
  readonly #config: BridgeConfig;
  readonly #dependencies: BridgeDependencies;
  readonly #directory: PrivateDirectory;
  readonly #sessions: MowerSessionFile;
  #lifecycle: BridgeLifecycle = 'created';
  #bridgeId: string | null = null;
  #client: EufyClient | undefined;
  #server: Server | undefined;
  #auth: AuthStatus = { state: 'disconnected', last_error: null, attempted_at: null };
  #connecting: { controller: AbortController; done: Promise<AuthStatus> } | undefined;
  #stopping: Promise<void> | undefined;
  /** Aborted at teardown so every discovery and local query ends before the client closes. */
  readonly #lifetime = new AbortController();
  #discovery: { at: number; mowers: MowerDevice[] } | undefined;
  #discoveryError: string | null = null;
  #discoveryAttemptAt: number | undefined;
  #discovering: Promise<void> | undefined;
  /**
   * True after a new cloud session or a failed discovery. The library then holds no device binding
   * until discovery succeeds again, so a LAN route runs discovery first.
   */
  #bindingsStale = false;
  #lastGood = new Map<string, TelemetryFields>();
  /** Ids whose most recent state query failed, so their last good result is only served as stale. */
  #staleIds = new Set<string>();
  #queries = new Map<string, Promise<MowerStateDocument>>();
  /** The write, a command or a setting, that owns each mower now. A second write answers 409. */
  #writes = new Map<string, Promise<MowerCommandDocument | MowerSettingDocument>>();
  /** Progress of the running command per mower id, served by the state route until it ends. */
  #inFlight = new Map<string, InFlightCommandDocument>();
  /** Present only with map provisioning. Construction does no I/O. */
  readonly #maps: MowerMaps | undefined;

  constructor(config: BridgeConfig, dependencies: BridgeDependencies = {}) {
    this.#config = config;
    this.#dependencies = dependencies;
    this.#directory = new PrivateDirectory(config.dataDir);
    this.#sessions = new MowerSessionFile(this.#directory);
    this.#maps = config.maps
      ? new MowerMaps({
          provisioningFile: config.maps.provisioningFile,
          create: dependencies.mapAcquisition ?? libraryMapAcquisition,
          timings: {
            demandMs: dependencies.mapDemandMs ?? DEFAULT_MAP_DEMAND_MS,
            streamLeaseMs: dependencies.mapStreamLeaseMs ?? DEFAULT_MAP_STREAM_LEASE_MS,
            idleIntervalMs: dependencies.mapIdleIntervalMs ?? DEFAULT_MAP_IDLE_INTERVAL_MS,
            retryIntervalMs: dependencies.mapRetryIntervalMs ?? DEFAULT_MAP_RETRY_INTERVAL_MS,
            watchIntervalMs: dependencies.mapWatchIntervalMs ?? DEFAULT_MAP_WATCH_INTERVAL_MS,
          },
          now: () => this.#now(),
          lifetime: this.#lifetime.signal,
        })
      : undefined;
  }

  get lifecycle(): BridgeLifecycle {
    return this.#lifecycle;
  }

  /** Bound address while running, otherwise null. */
  get address(): AddressInfo | null {
    const address = this.#server?.address();
    return address && typeof address === 'object' ? address : null;
  }

  get bridgeId(): string | null {
    return this.#bridgeId;
  }

  /** Read-only state document served by the private API. Contains no credentials or session data. */
  state(): BridgeState {
    const mowers = this.#client?.mowers;
    return {
      protocol: PROTOCOL_VERSION,
      bridge: BRIDGE_NAME,
      version: BRIDGE_VERSION,
      bridge_id: this.#bridgeId,
      lifecycle: this.#lifecycle,
      operating_mode: this.#config.operatingMode,
      auth: this.#authStatus(),
      client: {
        package: CLIENT_PACKAGE,
        version: CLIENT_VERSION,
        module: 'mowers',
        lifecycle: mowers?.lifecycle ?? 'closed',
        connected: mowers?.connected ?? false,
      },
      mowers: {
        count: this.#discovery?.mowers.length ?? null,
        discovered_at: this.#discovery ? new Date(this.#discovery.at).toISOString() : null,
        error: this.#discoveryError,
      },
      routes: {
        discovery: true,
        state: true,
        control: this.#config.control !== null,
        maps: this.#maps !== undefined,
        settings: this.#config.settingsMode === SETTINGS_MODE_WRITE,
      },
      control: this.#config.control
        ? {
            classes: [...ROUTED_COMMAND_CLASSES],
            max_state_age_ms: this.#config.control.maxStateAgeMs,
            read_back_ms: this.#config.control.readBackMs,
          }
        : null,
      maps: this.#maps?.status() ?? null,
    };
  }

  #now(): number {
    return (this.#dependencies.now ?? Date.now)();
  }

  #running(): MowerModule {
    const mowers = this.#client?.mowers;
    if (this.#lifecycle !== 'running' || !mowers) throw new ApiError(503, 'bridge_not_running');
    return mowers;
  }

  /** The last attempt, with the library's live state: a session that has lapsed reads as disconnected. */
  #authStatus(): AuthStatus {
    if (this.#auth.state === 'connected' && !this.#client?.mowers?.connected) return { ...this.#auth, state: 'disconnected' };
    return { ...this.#auth };
  }

  /**
   * Whether a route may renew the cloud session now. The explicit startup attempt comes first. A
   * session that was good and has lapsed is renewed at once, a transient failure after the
   * re-authentication interval, and a refused sign-in never, because it needs the operator.
   */
  #reauthenticationAllowed(): boolean {
    const { last_error: lastError, attempted_at: attemptedAt } = this.#auth;
    if (attemptedAt === null || signInRefused(this.#auth)) return false;
    if (lastError === null) return true;
    const interval = this.#dependencies.reauthIntervalMs ?? DEFAULT_REAUTH_INTERVAL_MS;
    return this.#now() - Date.parse(attemptedAt) >= interval;
  }

  /**
   * Renews a lapsed or failed cloud session before a route needs it, through one bounded attempt
   * that concurrent routes join. It never throws: without a session the route fails as before,
   * before any write. A new session leaves the library without device bindings until discovery
   * runs again. Nothing about a command is repeated.
   */
  async #renewSession(mowers: MowerModule): Promise<void> {
    if (mowers.connected) return;
    if (this.#connecting) {
      await this.#connecting.done;
      return;
    }
    if (!this.#reauthenticationAllowed()) return;
    const result = await this.connect().catch(() => null);
    if (!result) return;
    if (result.state === 'connected') this.#dependencies.log?.('info', 'mower cloud session renewed');
    else this.#dependencies.log?.('warn', `mower cloud session renewal failed (${result.last_error ?? result.state})`);
  }

  /**
   * A LAN session needs a live cloud session and the library's device binding from discovery. The
   * lapsed session is renewed first, then discovery runs again while the bindings are stale.
   */
  async #prepareLocal(mowers: MowerModule): Promise<void> {
    await this.#renewSession(mowers);
    if (!this.#discovery || this.#bindingsStale) await this.#refreshDiscovery(mowers, this.#bindingsStale);
  }

  /**
   * Discovered mowers from the library. A request runs discovery only when no list exists or the
   * list is older than the cache age, at most once per interval. A failed refresh keeps serving
   * the older list with `fresh: false` and the failure code. Nothing runs without a request.
   */
  async discover(): Promise<DiscoveryDocument> {
    const mowers = this.#running();
    await this.#refreshDiscovery(mowers);
    if (!this.#discovery) throw new ApiError(503, this.#discoveryError ?? 'mower_request_failed');
    return this.#discoveryDocument(this.#discovery);
  }

  /** `force` refreshes a list younger than the cache age, for stale bindings. The spacing still applies. */
  #refreshDiscovery(mowers: MowerModule, force = false): Promise<void> {
    if (this.#discovering) return this.#discovering;
    const now = this.#now();
    const cacheMs = this.#dependencies.discoveryCacheMs ?? DEFAULT_DISCOVERY_CACHE_MS;
    const intervalMs = this.#dependencies.discoveryIntervalMs ?? DEFAULT_DISCOVERY_INTERVAL_MS;
    const wanted = force || !this.#discovery || now - this.#discovery.at >= cacheMs;
    const allowed = this.#discoveryAttemptAt === undefined || now - this.#discoveryAttemptAt >= intervalMs;
    if (!wanted || !allowed) return Promise.resolve();
    this.#discoveryAttemptAt = now;
    const run = (async () => {
      // Discovery needs the cloud session. A renewal inside this run keeps the spacing of this attempt.
      await this.#renewSession(mowers);
      this.#discoveryAttemptAt = now;
      const bound = deadline(this.#dependencies.discoveryTimeoutMs ?? DEFAULT_DISCOVERY_TIMEOUT_MS);
      try {
        const devices = await mowers.discover(AbortSignal.any([this.#lifetime.signal, bound.signal]));
        this.#discovery = { at: this.#now(), mowers: devices.map((device) => ({ ...device })) };
        this.#discoveryError = null;
        this.#bindingsStale = false;
      } catch (error) {
        let code = errorCode(error);
        if (code === 'request_aborted' && bound.expired()) code = 'request_timeout';
        this.#discoveryError = code;
        // The library revokes its bindings when a discovery starts and holds none after a failure.
        this.#bindingsStale = true;
      } finally {
        bound.clear();
      }
    })();
    this.#discovering = run;
    void run.finally(() => {
      if (this.#discovering === run) this.#discovering = undefined;
    });
    return run;
  }

  #discoveryDocument(discovery: { at: number; mowers: MowerDevice[] }): DiscoveryDocument {
    const cacheMs = this.#dependencies.discoveryCacheMs ?? DEFAULT_DISCOVERY_CACHE_MS;
    return {
      contract: 1,
      discovered_at: new Date(discovery.at).toISOString(),
      fresh: this.#discoveryError === null && this.#now() - discovery.at < cacheMs,
      error: this.#discoveryError,
      mowers: discovery.mowers.map((mower) => ({
        id: mower.id,
        kind: mower.kind,
        model: mower.model,
        productCode: mower.productCode,
        state_available: this.#hostFor(mower.id) !== null,
      })),
    };
  }

  /** The configured LAN host for one discovered id. The single host applies only to a sole mower. */
  #hostFor(id: string): string | null {
    const configured = this.#config.hosts[id];
    if (configured) return configured;
    const mowers = this.#discovery?.mowers;
    if (this.#config.host && mowers?.length === 1 && mowers[0]?.id === id) return this.#config.host;
    return null;
  }

  /**
   * One read-only local query for one discovered mower. Concurrent requests for the same id join
   * the running query, so at most one LAN session per mower is open at a time. After a failure the
   * last good result is served as stale with the failure code. Nothing is retried or polled.
   */
  mowerState(id: string): Promise<MowerStateDocument> {
    let mowers: MowerModule;
    try {
      mowers = this.#running();
    } catch (error) {
      return Promise.reject(error);
    }
    if (!MOWER_ID.test(id)) return Promise.reject(new ApiError(400, 'invalid_mower_id'));
    const running = this.#queries.get(id);
    if (running) return running;
    const query = this.#queryState(mowers, id);
    this.#queries.set(id, query);
    void query.then(
      () => this.#queries.delete(id),
      () => this.#queries.delete(id),
    );
    return query;
  }

  async #queryState(mowers: MowerModule, id: string): Promise<MowerStateDocument> {
    await this.#prepareLocal(mowers);
    if (!this.#discovery) throw new ApiError(503, this.#discoveryError ?? 'mower_request_failed');
    if (!this.#discovery.mowers.some((mower) => mower.id === id)) throw new ApiError(404, 'unknown_mower');
    const host = this.#hostFor(id);
    if (!host) throw new ApiError(503, 'mower_host_unconfigured');
    const open: OpenLocalSession =
      this.#dependencies.openLocalSession ?? ((target, options, signal) => mowers.openLocalSession(target, options, signal));
    const signal = this.#lifetime.signal;
    try {
      const session = await open(id, { host, timeoutMs: this.#config.localTimeoutMs }, signal);
      let telemetry: MowerTelemetry;
      let settings: MowerSettings;
      try {
        telemetry = await session.queryTelemetry(signal);
        // The settings come from the same snapshot, decoded with the device's own declaration.
        const schema = session.schema;
        settings = decodeMowerSettings(telemetry, schema ? { schema } : {});
      } finally {
        await session.disconnect();
      }
      const fields: TelemetryFields = {
        observed_at: telemetry.observedAt,
        status: structuredClone(telemetry.status),
        battery: structuredClone(telemetry.battery),
        progress: structuredClone(telemetry.progress),
        network: structuredClone(telemetry.network),
        settings: settingsDocument(settings),
      };
      this.#lastGood.set(id, fields);
      this.#staleIds.delete(id);
      return this.#stateDocument(id, fields, null);
    } catch (error) {
      const code = errorCode(error);
      this.#staleIds.add(id);
      const last = this.#lastGood.get(id);
      if (last) return this.#stateDocument(id, last, code);
      throw new ApiError(503, code);
    }
  }

  #stateDocument(id: string, fields: TelemetryFields, error: string | null): MowerStateDocument {
    const observed = Date.parse(fields.observed_at);
    return {
      contract: 1,
      id,
      source: 'local-tuya-3.5',
      age_ms: Number.isFinite(observed) ? Math.max(0, this.#now() - observed) : null,
      stale: error !== null,
      error,
      ...structuredClone(fields),
      command: this.#inFlightDocument(id),
    };
  }

  #inFlightDocument(id: string): InFlightCommandDocument | null {
    const running = this.#inFlight.get(id);
    return running ? structuredClone(running) : null;
  }

  /**
   * One opt-in command for one discovered mower, `POST /v1/mowers/{id}/commands/{class}`. Every
   * check runs on the bridge before the library is touched: the operating mode, the class, the
   * mower and its host, and the age of the last successful state observation against
   * `control.maxStateAgeMs`. One command owns a mower at a time. The library's outcome is served
   * as confirmed, failed or uncertain and is never retried or replayed.
   */
  command(id: string, kind: string): Promise<MowerCommandDocument> {
    let mowers: MowerModule;
    try {
      mowers = this.#running();
    } catch (error) {
      return Promise.reject(error);
    }
    const control = this.#config.control;
    if (!control) return Promise.reject(new ApiError(403, 'control_disabled'));
    if (!COMMAND_CLASSES.includes(kind)) return Promise.reject(new ApiError(404, 'not_found'));
    if (!(ROUTED_COMMAND_CLASSES as readonly string[]).includes(kind)) return Promise.reject(new ApiError(409, 'command_unsupported'));
    if (!MOWER_ID.test(id)) return Promise.reject(new ApiError(400, 'invalid_mower_id'));
    if (this.#writes.has(id)) return Promise.reject(new ApiError(409, 'command_in_progress'));
    const run = this.#runCommand(mowers, control, id, kind as RoutedCommandClass);
    this.#own(id, run);
    return run;
  }

  /** Records the write that owns a mower until it settles. */
  #own(id: string, run: Promise<MowerCommandDocument | MowerSettingDocument>): void {
    this.#writes.set(id, run);
    void run.then(
      () => this.#writes.delete(id),
      () => this.#writes.delete(id),
    );
  }

  /**
   * One opt-in setting write for one discovered mower, `POST /v1/mowers/{id}/settings/{key}` with
   * the new value in the `value` query parameter. The settings mode, the key, the value's format,
   * the mower and its host are checked on the bridge before the library is touched, and a
   * read-only setting is refused there with the library's own code. The library then runs its
   * fresh query, refusals, single write and read-back. The outcome is served as confirmed, failed
   * or uncertain, is never retried or replayed, and a restore is a second deliberate request.
   */
  setting(id: string, key: string, value: string | null): Promise<MowerSettingDocument> {
    let mowers: MowerModule;
    try {
      mowers = this.#running();
    } catch (error) {
      return Promise.reject(error);
    }
    if (this.#config.settingsMode !== SETTINGS_MODE_WRITE) return Promise.reject(new ApiError(403, 'settings_disabled'));
    if (!Object.hasOwn(SETTING_KEYS, key)) return Promise.reject(new ApiError(404, 'not_found'));
    if (!(WRITABLE_SETTING_KEYS as readonly string[]).includes(key))
      return Promise.reject(new ApiError(409, 'mower_setting_read_only'));
    const parsed = settingValue(value);
    if (parsed === undefined) return Promise.reject(new ApiError(400, 'invalid_setting_value'));
    if (!MOWER_ID.test(id)) return Promise.reject(new ApiError(400, 'invalid_mower_id'));
    if (this.#writes.has(id)) return Promise.reject(new ApiError(409, 'command_in_progress'));
    const run = this.#runSetting(mowers, id, key as SettingKey, parsed);
    this.#own(id, run);
    return run;
  }

  async #runSetting(mowers: MowerModule, id: string, key: SettingKey, value: boolean | number): Promise<MowerSettingDocument> {
    // A renewal or discovery here runs before any write. The write itself is never repeated.
    await this.#prepareLocal(mowers);
    if (!this.#discovery) throw new ApiError(503, this.#discoveryError ?? 'mower_request_failed');
    if (!this.#discovery.mowers.some((entry) => entry.id === id)) throw new ApiError(404, 'unknown_mower');
    const host = this.#hostFor(id);
    if (!host) throw new ApiError(503, 'mower_host_unconfigured');
    const open: OpenLocalSession =
      this.#dependencies.openLocalSession ?? ((target, options, signal) => mowers.openLocalSession(target, options, signal));
    const signal = this.#lifetime.signal;
    let outcome: MowerSettingOutcome;
    try {
      const session = await open(id, { host, timeoutMs: this.#config.localTimeoutMs }, signal);
      try {
        outcome = await session.setSetting({ name: SETTING_KEYS[key], value }, signal);
      } finally {
        await session.disconnect();
      }
    } catch (error) {
      const code = errorCode(error);
      // The library's typed refusals happen before any frame is written. Everything else is a transport or session failure.
      throw new ApiError(code.startsWith('mower_setting') ? 409 : 503, code);
    }
    return settingOutcomeDocument(id, key, outcome);
  }

  async #runCommand(mowers: MowerModule, control: ControlConfig, id: string, kind: RoutedCommandClass): Promise<MowerCommandDocument> {
    // A renewal or discovery here runs before any write. The command itself is never repeated.
    await this.#prepareLocal(mowers);
    if (!this.#discovery) throw new ApiError(503, this.#discoveryError ?? 'mower_request_failed');
    const mower = this.#discovery.mowers.find((entry) => entry.id === id);
    if (!mower) throw new ApiError(404, 'unknown_mower');
    if (mower.model !== 'E15') throw new ApiError(409, 'command_unsupported');
    const host = this.#hostFor(id);
    if (!host) throw new ApiError(503, 'mower_host_unconfigured');
    const last = this.#lastGood.get(id);
    const observed = last ? Date.parse(last.observed_at) : Number.NaN;
    if (this.#staleIds.has(id) || !Number.isFinite(observed) || this.#now() - observed > control.maxStateAgeMs)
      throw new ApiError(409, 'telemetry_stale');
    const open: OpenLocalSession =
      this.#dependencies.openLocalSession ?? ((target, options, signal) => mowers.openLocalSession(target, options, signal));
    const signal = this.#lifetime.signal;
    let outcome: MowerCommandOutcome;
    const progress: InFlightCommandDocument = { command: kind, acknowledged_at: null, activity: null };
    this.#inFlight.set(id, progress);
    try {
      const session = await open(id, { host, timeoutMs: this.#config.localTimeoutMs }, signal);
      try {
        outcome = await session.sendCommand(
          {
            kind,
            // Progress only informs the state route while the read-back runs. It never ends,
            // repeats or changes the command, and the outcome stays the result.
            onProgress: (event) => {
              if (event.kind === 'acknowledged') progress.acknowledged_at ??= event.observedAt;
              else progress.activity = { observed_at: event.observedAt, sequence: event.sequence, value: event.value };
            },
          },
          signal,
        );
      } finally {
        await session.disconnect();
      }
    } catch (error) {
      const code = errorCode(error);
      // The library's typed refusals happen before any frame is written. Everything else is a transport or session failure.
      throw new ApiError(code.startsWith('mower_command') ? 409 : 503, code);
    } finally {
      if (this.#inFlight.get(id) === progress) this.#inFlight.delete(id);
    }
    return commandDocument(id, outcome);
  }

  /**
   * The read-only map bundle of the mower the provisioning belongs to, `GET /v1/mowers/{id}/map`.
   * The request may start one acquisition demand in the background and is answered at once with
   * the last good bundle, its entity tag and age, or `304` when the client's tag still matches.
   * Without any bundle the answer is `503` with the last failure code. Nothing is written to the
   * mower and nothing about the map is logged.
   */
  map(id: string, request: MapRequest): Promise<RawReply> {
    let mowers: MowerModule;
    let mode: MapMode;
    try {
      mowers = this.#running();
      if (!this.#maps) throw new ApiError(404, 'map_unconfigured');
      if (!MOWER_ID.test(id)) throw new ApiError(400, 'invalid_mower_id');
      mode = mapMode(request.mode);
    } catch (error) {
      return Promise.reject(error);
    }
    return this.#serveMap(mowers, this.#maps, id, mode, request.ifNoneMatch);
  }

  async #serveMap(mowers: MowerModule, maps: MowerMaps, id: string, mode: MapMode, ifNoneMatch: string | undefined): Promise<RawReply> {
    if (!this.#discovery) await this.#refreshDiscovery(mowers);
    if (!this.#discovery) throw new ApiError(503, this.#discoveryError ?? 'mower_request_failed');
    if (!this.#discovery.mowers.some((mower) => mower.id === id)) throw new ApiError(404, 'unknown_mower');
    const owner = this.#mapMower();
    if (owner === null) throw new ApiError(409, 'map_mower_unresolved');
    if (owner !== id) throw new ApiError(404, 'map_unconfigured');
    maps.request(id, mode);
    return maps.reply(id, ifNoneMatch);
  }

  /** The mower the map provisioning belongs to: the configured id, otherwise the sole discovered mower. */
  #mapMower(): string | null {
    const configured = this.#config.maps?.mowerId;
    if (configured) return configured;
    const mowers = this.#discovery?.mowers;
    return mowers?.length === 1 ? (mowers[0]?.id ?? null) : null;
  }

  /** Prepares private storage, constructs the mower-only client and starts listening. Bounded. */
  async start(): Promise<void> {
    if (this.#lifecycle !== 'created') throw new BridgeError('bridge_not_restartable');
    this.#lifecycle = 'starting';
    const bound = deadline(this.#dependencies.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS);
    try {
      await this.#directory.prepare();
      this.#bridgeId = await bridgeIdentity(this.#directory);
      if (bound.signal.aborted) throw new BridgeError('startup_timeout');
      const home: MowerHomeOptions = { requestTimeoutMs: this.#config.cloudTimeoutMs };
      if (this.#dependencies.fetch) home.fetch = this.#dependencies.fetch;
      this.#client = new EufyClient({
        mowers: {
          credentials: { ...this.#config.credentials },
          sessionStore: this.#sessions,
          home,
          ...(this.#dependencies.adapter ? { adapter: this.#dependencies.adapter } : {}),
          ...(this.#config.control
            ? { commands: { enabled: true as const, stopRoute: this.#config.control.stopRoute, readBackMs: this.#config.control.readBackMs } }
            : {}),
          ...(this.#config.settingsMode === SETTINGS_MODE_WRITE ? { settings: { enabled: true as const } } : {}),
        },
      });
      this.#server = createPrivateServer(this.#config.token, {
        state: () => this.state(),
        discover: () => this.discover(),
        mowerState: (id) => this.mowerState(id),
        command: (id, kind) => this.command(id, kind),
        setting: (id, key, value) => this.setting(id, key, value),
        map: (id, request) => this.map(id, request),
      });
      await listen(this.#server, this.#config.port, this.#config.bindAddress, bound.signal);
      this.#lifecycle = 'running';
    } catch (error) {
      await this.#teardown().catch(() => {});
      this.#lifecycle = 'stopped';
      if (error instanceof BridgeError) throw error;
      throw new BridgeError(bound.expired() ? 'startup_timeout' : 'startup_failed');
    } finally {
      bound.clear();
    }
  }

  /**
   * One authentication attempt through the library, explicit at startup or a renewal by a route.
   * The result is recorded in the state document. Failures never throw and this call never
   * retries. A concurrent call joins the attempt.
   */
  connect(): Promise<AuthStatus> {
    if (this.#lifecycle !== 'running' || !this.#client?.mowers) return Promise.reject(new BridgeError('bridge_not_running'));
    if (this.#connecting) return this.#connecting.done;
    const mowers = this.#client.mowers;
    const controller = new AbortController();
    const bound = deadline(this.#dependencies.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS);
    bound.signal.addEventListener('abort', () => controller.abort(), { once: true });
    const attemptedAt = new Date(this.#now()).toISOString();
    const done = (async () => {
      try {
        const result = await mowers.connect(undefined, controller.signal);
        this.#auth = { state: result.state, last_error: null, attempted_at: attemptedAt };
        if (result.state === 'connected') {
          // A fresh session may discover at once. Earlier failed attempts must not delay it.
          this.#discoveryAttemptAt = undefined;
          // A new session holds no device binding until discovery runs again.
          this.#bindingsStale = true;
        }
      } catch (error) {
        let code = error instanceof EufyError ? error.code : 'mower_authentication_failed';
        if (code === 'request_aborted' && bound.expired()) code = 'request_timeout';
        this.#auth = { state: 'disconnected', last_error: code, attempted_at: attemptedAt };
      } finally {
        bound.clear();
      }
      return { ...this.#auth };
    })();
    this.#connecting = { controller, done };
    void done.finally(() => {
      if (this.#connecting?.done === done) this.#connecting = undefined;
    });
    return done;
  }

  /** Idempotent bounded shutdown: cancel authentication, close the server, close the client, flush files. */
  stop(): Promise<void> {
    if (this.#stopping) return this.#stopping;
    this.#stopping = this.#stop();
    return this.#stopping;
  }

  async #stop(): Promise<void> {
    if (this.#lifecycle === 'stopped') return;
    this.#lifecycle = 'stopping';
    const bound = deadline(this.#dependencies.shutdownTimeoutMs ?? DEFAULT_SHUTDOWN_TIMEOUT_MS);
    const expiry = new Promise<never>((_, reject) => {
      bound.signal.addEventListener('abort', () => reject(new BridgeError('shutdown_timeout')), { once: true });
    });
    try {
      await Promise.race([this.#teardown(), expiry]);
    } finally {
      bound.clear();
      this.#lifecycle = 'stopped';
    }
  }

  async #teardown(): Promise<void> {
    this.#lifetime.abort();
    this.#connecting?.controller.abort();
    await this.#connecting?.done;
    await Promise.allSettled([this.#discovering, ...this.#queries.values(), ...this.#writes.values(), this.#maps?.close()]);
    const server = this.#server;
    const client = this.#client;
    this.#server = undefined;
    this.#client = undefined;
    let failure: BridgeError | undefined;
    if (server) await closeServer(server, SERVER_CLOSE_GRACE_MS);
    if (client) {
      try {
        await client.shutdown();
      } catch {
        failure = new BridgeError('shutdown_incomplete');
      }
    }
    await this.#directory.flush();
    this.#auth = { ...this.#auth, state: 'disconnected' };
    if (failure) throw failure;
  }
}
