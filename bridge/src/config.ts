import { readFile } from 'node:fs/promises';
import { isIP } from 'node:net';
import { isAbsolute } from 'node:path';

/** Physical control is not part of this bridge version. Only observe-only configurations start. */
export const OPERATING_MODE_OBSERVE_ONLY = 'observe_only';
export type OperatingMode = typeof OPERATING_MODE_OBSERVE_ONLY;

export const DEFAULT_PORT = 8090;
export const DEFAULT_BIND_ADDRESS = '127.0.0.1';
export const DEFAULT_DATA_DIR = '/data/eufy-mower';
export const DEFAULT_CLOUD_TIMEOUT_MS = 15_000;
export const DEFAULT_LOCAL_TIMEOUT_MS = 5_000;
export const MAX_OPTIONS_FILE_BYTES = 65_536;

/** Mower account credentials. Secret. Never logged, never part of any state response. */
export interface MowerCredentials {
  email: string;
  password: string;
  country: string;
}

export interface BridgeConfig {
  /** Bearer token that protects the private HTTP API. Secret. */
  token: string;
  credentials: MowerCredentials;
  port: number;
  bindAddress: string;
  /** Private directory that holds the mower session and bridge identity. */
  dataDir: string;
  operatingMode: OperatingMode;
  /** Deadline for each Eufy Home/Tuya request issued by the library. */
  cloudTimeoutMs: number;
  /** Deadline for connecting to a mower on the LAN and for each local query. */
  localTimeoutMs: number;
  /** LAN host per discovered mower id. Ids are the library's opaque 64-character identifiers. */
  hosts: Readonly<Record<string, string>>;
  /** LAN host used when exactly one mower is discovered and it has no entry in `hosts`. */
  host: string | null;
}

export class ConfigError extends Error {
  readonly key: string;
  readonly reason: string;
  constructor(key: string, reason: string) {
    super(`${key}: ${reason}`);
    this.key = key;
    this.reason = reason;
    this.name = 'ConfigError';
  }
}

/** Environment variable names. Every key is specific to the mower bridge. */
export const ENV = {
  token: 'EUFY_MOWER_BRIDGE_TOKEN',
  email: 'EUFY_MOWER_EMAIL',
  password: 'EUFY_MOWER_PASSWORD',
  country: 'EUFY_MOWER_COUNTRY',
  port: 'EUFY_MOWER_PORT',
  bind_address: 'EUFY_MOWER_BIND_ADDRESS',
  data_dir: 'EUFY_MOWER_DATA_DIR',
  operating_mode: 'EUFY_MOWER_OPERATING_MODE',
  cloud_timeout_ms: 'EUFY_MOWER_CLOUD_TIMEOUT_MS',
  local_timeout_ms: 'EUFY_MOWER_LOCAL_TIMEOUT_MS',
  host: 'EUFY_MOWER_HOST',
  hosts: 'EUFY_MOWER_HOSTS',
  options_file: 'EUFY_MOWER_OPTIONS_FILE',
} as const;

/** Keys accepted in the JSON options file. Values are strings or, for numbers, integers. */
export type OptionKey = Exclude<keyof typeof ENV, 'options_file'>;
const OPTION_KEYS: readonly OptionKey[] = [
  'token',
  'email',
  'password',
  'country',
  'port',
  'bind_address',
  'data_dir',
  'operating_mode',
  'cloud_timeout_ms',
  'local_timeout_ms',
  'host',
  'hosts',
];

/** `hosts` is `id=host` pairs separated by commas, or an object of id to host in the options file. */
export type OptionValues = Partial<Record<Exclude<OptionKey, 'hosts'>, string | number>> & {
  hosts?: string | Readonly<Record<string, string>>;
};

export const MOWER_ID = /^[a-f0-9]{64}$/;
const HOSTNAME = /^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/;

function host(key: OptionKey, value: string): string {
  if (isIP(value) !== 0 || HOSTNAME.test(value)) return value;
  throw new ConfigError(key, 'must be an IP address or host name');
}

function hosts(value: OptionValues['hosts']): Record<string, string> {
  const result: Record<string, string> = {};
  const pairs: [string, string][] =
    value === undefined || value === ''
      ? []
      : typeof value === 'string'
        ? value.split(',').map((pair) => {
            const separator = pair.indexOf('=');
            if (separator < 1) throw new ConfigError('hosts', 'must be id=host pairs separated by commas');
            return [pair.slice(0, separator).trim(), pair.slice(separator + 1).trim()];
          })
        : Object.entries(value);
  for (const [id, target] of pairs) {
    if (!MOWER_ID.test(id)) throw new ConfigError('hosts', 'contains an id that is not a 64-character mower id');
    if (id in result) throw new ConfigError('hosts', 'lists the same mower id twice');
    result[id] = host('hosts', target);
  }
  return result;
}

type Environment = Record<string, string | undefined>;

function text(values: OptionValues, key: Exclude<OptionKey, 'hosts'>): string | undefined {
  const value = values[key];
  if (value === undefined) return undefined;
  return typeof value === 'number' ? String(value) : value;
}

function integer(values: OptionValues, key: Exclude<OptionKey, 'hosts'>, fallback: number, min: number, max: number): number {
  const raw = text(values, key);
  if (raw === undefined || raw === '') return fallback;
  if (!/^-?\d{1,10}$/.test(raw)) throw new ConfigError(key, 'must be an integer');
  const value = Number(raw);
  if (value < min || value > max) throw new ConfigError(key, `must be between ${min} and ${max}`);
  return value;
}

function required(values: OptionValues, key: Exclude<OptionKey, 'hosts'>): string {
  const value = text(values, key);
  if (value === undefined || value === '') throw new ConfigError(key, 'is required');
  return value;
}

/** Validates merged option values. Pure, so tests can cover every rule without files or processes. */
export function resolveConfig(values: OptionValues): BridgeConfig {
  const token = required(values, 'token');
  if (!/^[\x21-\x7e]{32,256}$/.test(token))
    throw new ConfigError('token', 'must contain 32 to 256 printable ASCII characters without spaces');
  const email = required(values, 'email');
  if (email.length > 254 || /\s/.test(email) || !email.includes('@'))
    throw new ConfigError('email', 'must be an email address');
  const password = required(values, 'password');
  if (password.length > 1024) throw new ConfigError('password', 'is too long');
  const country = required(values, 'country');
  if (!/^[A-Za-z]{2}$/.test(country)) throw new ConfigError('country', 'must be a two-letter country code');
  const port = integer(values, 'port', DEFAULT_PORT, 1, 65_535);
  const bindAddress = text(values, 'bind_address') || DEFAULT_BIND_ADDRESS;
  if (bindAddress !== 'localhost' && isIP(bindAddress) === 0)
    throw new ConfigError('bind_address', 'must be an IP address or localhost');
  const dataDir = text(values, 'data_dir') || DEFAULT_DATA_DIR;
  if (!isAbsolute(dataDir) || dataDir.includes('\0'))
    throw new ConfigError('data_dir', 'must be an absolute path');
  const operatingMode = text(values, 'operating_mode') || OPERATING_MODE_OBSERVE_ONLY;
  if (operatingMode !== OPERATING_MODE_OBSERVE_ONLY)
    throw new ConfigError('operating_mode', `only ${OPERATING_MODE_OBSERVE_ONLY} is available in this bridge version`);
  const cloudTimeoutMs = integer(values, 'cloud_timeout_ms', DEFAULT_CLOUD_TIMEOUT_MS, 1000, 60_000);
  const localTimeoutMs = integer(values, 'local_timeout_ms', DEFAULT_LOCAL_TIMEOUT_MS, 1000, 60_000);
  const single = text(values, 'host');
  return {
    token,
    credentials: { email, password, country: country.toUpperCase() },
    port,
    bindAddress,
    dataDir,
    operatingMode: OPERATING_MODE_OBSERVE_ONLY,
    cloudTimeoutMs,
    localTimeoutMs,
    hosts: hosts(values.hosts),
    host: single ? host('host', single) : null,
  };
}

/** Parses the JSON options file. Unknown keys and non-scalar values are rejected. */
export function parseOptions(source: string): OptionValues {
  if (Buffer.byteLength(source, 'utf8') > MAX_OPTIONS_FILE_BYTES)
    throw new ConfigError('options_file', 'is too large');
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw new ConfigError('options_file', 'is not valid JSON');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
    throw new ConfigError('options_file', 'must contain a JSON object');
  const values: OptionValues = {};
  for (const [key, value] of Object.entries(parsed)) {
    if (!OPTION_KEYS.includes(key as OptionKey)) throw new ConfigError('options_file', `has unknown key ${key}`);
    if (value === null) continue;
    if (key === 'hosts') {
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ConfigError(key, 'must be an object of id to host');
      const entries = Object.entries(value as Record<string, unknown>);
      for (const [, target] of entries) if (typeof target !== 'string') throw new ConfigError(key, 'must map every id to a host string');
      values.hosts = Object.fromEntries(entries as [string, string][]);
      continue;
    }
    if (typeof value !== 'string' && !(typeof value === 'number' && Number.isInteger(value)))
      throw new ConfigError(key, 'must be a string or an integer');
    values[key as Exclude<OptionKey, 'hosts'>] = value;
  }
  return values;
}

function fromEnvironment(env: Environment): OptionValues {
  const values: OptionValues = {};
  for (const key of OPTION_KEYS) {
    const value = env[ENV[key]];
    if (value !== undefined) values[key] = value;
  }
  return values;
}

/**
 * Loads the configuration from the optional JSON options file and the environment.
 * Environment values override file values. Every rule is checked before anything starts.
 */
export async function loadConfig(
  env: Environment,
  io: { readFile: (path: string) => Promise<string> } = { readFile: (path) => readFile(path, 'utf8') },
): Promise<BridgeConfig> {
  let fileValues: OptionValues = {};
  const optionsFile = env[ENV.options_file];
  if (optionsFile) {
    let source: string;
    try {
      source = await io.readFile(optionsFile);
    } catch {
      throw new ConfigError('options_file', 'could not be read');
    }
    fileValues = parseOptions(source);
  }
  return resolveConfig({ ...fileValues, ...fromEnvironment(env) });
}

/** Non-secret summary for startup logging. Credentials and the token are deliberately absent. */
export function describeConfig(config: BridgeConfig): Record<string, string | number> {
  return {
    port: config.port,
    bind_address: config.bindAddress,
    data_dir: config.dataDir,
    operating_mode: config.operatingMode,
    country: config.credentials.country,
    cloud_timeout_ms: config.cloudTimeoutMs,
    local_timeout_ms: config.localTimeoutMs,
    configured_hosts: Object.keys(config.hosts).length + (config.host ? 1 : 0),
  };
}
