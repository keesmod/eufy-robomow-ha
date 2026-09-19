import type { AddressInfo } from 'node:net';
import type { Server } from 'node:http';
import {
  EufyClient,
  EufyError,
  type AuthState,
  type ModuleLifecycleState,
  type MowerActivity,
  type MowerAdapter,
  type MowerAdapterContext,
  type MowerDevice,
  type MowerHomeOptions,
  type MowerLocalSession,
  type MowerLocalSessionOptions,
  type MowerModule,
  type MowerNetworkKind,
  type MowerTelemetry,
  type MowerTelemetryField,
} from '@keesmod/eufy-mega-client';
import { MOWER_ID, type BridgeConfig } from './config.ts';
import { ApiError, BridgeError } from './errors.ts';
import { createPrivateServer } from './server.ts';
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

export { ApiError, BridgeError } from './errors.ts';

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
  startupTimeoutMs?: number;
  connectTimeoutMs?: number;
  shutdownTimeoutMs?: number;
  discoveryTimeoutMs?: number;
  discoveryCacheMs?: number;
  discoveryIntervalMs?: number;
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

/** The four typed fields of one query. Raw data points are deliberately absent. */
export interface TelemetryFields {
  observed_at: string;
  status: MowerTelemetryField<MowerActivity>;
  battery: MowerTelemetryField<{ percent: number }>;
  progress: MowerTelemetryField<{ percent: number }>;
  network: MowerTelemetryField<{ kind?: MowerNetworkKind; signalDbm?: number; signalPercent?: number }>;
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
}

export type BridgeLifecycle = 'created' | 'starting' | 'running' | 'stopping' | 'stopped';

/** Authentication summary. Captcha images and verification prompts are not exposed by this version. */
export interface AuthStatus {
  state: AuthState['state'];
  /** Stable library or bridge error code of the last explicit attempt, or null when it succeeded. */
  last_error: string | null;
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
  /** Control and map routes arrive in later steps. Physical control is never part of observe-only operation. */
  routes: { discovery: true; state: true; control: false; maps: false };
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
 * shutdown are each bounded. Nothing is retried and no mower command exists in this version.
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
  #lastGood = new Map<string, TelemetryFields>();
  #queries = new Map<string, Promise<MowerStateDocument>>();

  constructor(config: BridgeConfig, dependencies: BridgeDependencies = {}) {
    this.#config = config;
    this.#dependencies = dependencies;
    this.#directory = new PrivateDirectory(config.dataDir);
    this.#sessions = new MowerSessionFile(this.#directory);
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
      auth: { ...this.#auth },
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
      routes: { discovery: true, state: true, control: false, maps: false },
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

  #refreshDiscovery(mowers: MowerModule): Promise<void> {
    if (this.#discovering) return this.#discovering;
    const now = this.#now();
    const cacheMs = this.#dependencies.discoveryCacheMs ?? DEFAULT_DISCOVERY_CACHE_MS;
    const intervalMs = this.#dependencies.discoveryIntervalMs ?? DEFAULT_DISCOVERY_INTERVAL_MS;
    const wanted = !this.#discovery || now - this.#discovery.at >= cacheMs;
    const allowed = this.#discoveryAttemptAt === undefined || now - this.#discoveryAttemptAt >= intervalMs;
    if (!wanted || !allowed) return Promise.resolve();
    this.#discoveryAttemptAt = now;
    const bound = deadline(this.#dependencies.discoveryTimeoutMs ?? DEFAULT_DISCOVERY_TIMEOUT_MS);
    const run = (async () => {
      try {
        const devices = await mowers.discover(AbortSignal.any([this.#lifetime.signal, bound.signal]));
        this.#discovery = { at: this.#now(), mowers: devices.map((device) => ({ ...device })) };
        this.#discoveryError = null;
      } catch (error) {
        let code = errorCode(error);
        if (code === 'request_aborted' && bound.expired()) code = 'request_timeout';
        this.#discoveryError = code;
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
    if (!this.#discovery) await this.#refreshDiscovery(mowers);
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
      try {
        telemetry = await session.queryTelemetry(signal);
      } finally {
        await session.disconnect();
      }
      const fields: TelemetryFields = {
        observed_at: telemetry.observedAt,
        status: structuredClone(telemetry.status),
        battery: structuredClone(telemetry.battery),
        progress: structuredClone(telemetry.progress),
        network: structuredClone(telemetry.network),
      };
      this.#lastGood.set(id, fields);
      return this.#stateDocument(id, fields, null);
    } catch (error) {
      const code = errorCode(error);
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
    };
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
        },
      });
      this.#server = createPrivateServer(this.#config.token, {
        state: () => this.state(),
        discover: () => this.discover(),
        mowerState: (id) => this.mowerState(id),
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
   * One explicit authentication attempt through the library. The result is recorded in the state
   * document. Failures never throw and are never retried. A concurrent call joins the attempt.
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
        // A fresh session may discover at once. Earlier failed attempts must not delay it.
        if (result.state === 'connected') this.#discoveryAttemptAt = undefined;
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
    await Promise.allSettled([this.#discovering, ...this.#queries.values()]);
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
