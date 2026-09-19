import type { AddressInfo } from 'node:net';
import type { Server } from 'node:http';
import {
  EufyClient,
  EufyError,
  type AuthState,
  type ModuleLifecycleState,
  type MowerAdapter,
  type MowerAdapterContext,
  type MowerHomeOptions,
} from '@keesmod/eufy-mega-client';
import type { BridgeConfig } from './config.ts';
import { createPrivateServer } from './server.ts';
import { MowerSessionFile, PrivateDirectory, bridgeIdentity } from './storage.ts';
import { BRIDGE_NAME, BRIDGE_VERSION, CLIENT_PACKAGE, CLIENT_VERSION, PROTOCOL_VERSION } from './version.ts';

export const DEFAULT_STARTUP_TIMEOUT_MS = 10_000;
export const DEFAULT_CONNECT_TIMEOUT_MS = 30_000;
export const DEFAULT_SHUTDOWN_TIMEOUT_MS = 15_000;

export class BridgeError extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.code = code;
    this.name = 'BridgeError';
  }
}

/** Test seams and bounds. Production uses the library's own Home/Tuya adapter and global fetch. */
export interface BridgeDependencies {
  adapter?: (context: MowerAdapterContext) => MowerAdapter;
  fetch?: typeof fetch;
  startupTimeoutMs?: number;
  connectTimeoutMs?: number;
  shutdownTimeoutMs?: number;
  now?: () => number;
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
  /** Feature routes arrive in later steps. Physical control is never part of observe-only operation. */
  routes: { discovery: false; state: false; control: false; maps: false };
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

function closeServer(server: Server): Promise<void> {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve) => {
    server.close(() => resolve());
    server.closeAllConnections();
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
      routes: { discovery: false, state: false, control: false, maps: false },
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
      this.#server = createPrivateServer(this.#config.token, () => this.state());
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
    const attemptedAt = new Date((this.#dependencies.now ?? Date.now)()).toISOString();
    const done = (async () => {
      try {
        const result = await mowers.connect(undefined, controller.signal);
        this.#auth = { state: result.state, last_error: null, attempted_at: attemptedAt };
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
    this.#connecting?.controller.abort();
    await this.#connecting?.done;
    const server = this.#server;
    const client = this.#client;
    this.#server = undefined;
    this.#client = undefined;
    let failure: BridgeError | undefined;
    if (server) await closeServer(server);
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
