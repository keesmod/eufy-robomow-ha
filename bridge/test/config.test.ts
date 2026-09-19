import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ConfigError,
  DEFAULT_BIND_ADDRESS,
  DEFAULT_CLOUD_TIMEOUT_MS,
  DEFAULT_DATA_DIR,
  DEFAULT_PORT,
  ENV,
  describeConfig,
  loadConfig,
  parseOptions,
  resolveConfig,
} from '../src/config.ts';
import { EMAIL, PASSWORD, TOKEN, assertNoSecrets, syntheticValues } from './helpers.ts';

function rejects(values: Parameters<typeof resolveConfig>[0], key: string): void {
  assert.throws(() => resolveConfig(values), (error: unknown) => error instanceof ConfigError && error.key === key, key);
}

test('minimal values resolve to observe-only defaults with a separate mower data path', () => {
  const config = resolveConfig(syntheticValues());
  assert.deepEqual(config, {
    token: TOKEN,
    credentials: { email: EMAIL, password: PASSWORD, country: 'NL' },
    port: DEFAULT_PORT,
    bindAddress: DEFAULT_BIND_ADDRESS,
    dataDir: DEFAULT_DATA_DIR,
    operatingMode: 'observe_only',
    cloudTimeoutMs: DEFAULT_CLOUD_TIMEOUT_MS,
  });
  assert.equal(config.bindAddress, '127.0.0.1', 'the private API binds to loopback unless configured otherwise');
  assert.notEqual(config.dataDir, '/data', 'the mower keeps its own data path');
});

test('every required value and format is validated before startup', () => {
  rejects(syntheticValues({ token: '' }), 'token');
  rejects(syntheticValues({ token: 'short' }), 'token');
  rejects(syntheticValues({ token: `${TOKEN} with space` }), 'token');
  rejects(syntheticValues({ email: 'not-an-address' }), 'email');
  rejects(syntheticValues({ email: '' }), 'email');
  rejects(syntheticValues({ password: 'x'.repeat(1025) }), 'password');
  rejects(syntheticValues({ password: '' }), 'password');
  rejects(syntheticValues({ country: 'NLD' }), 'country');
  rejects(syntheticValues({ country: '' }), 'country');
  rejects(syntheticValues({ port: 0 }), 'port');
  rejects(syntheticValues({ port: 70_000 }), 'port');
  rejects(syntheticValues({ port: 'abc' }), 'port');
  rejects(syntheticValues({ bind_address: 'example.com' }), 'bind_address');
  rejects(syntheticValues({ data_dir: 'relative/path' }), 'data_dir');
  rejects(syntheticValues({ cloud_timeout_ms: 500 }), 'cloud_timeout_ms');
  rejects(syntheticValues({ cloud_timeout_ms: 60_001 }), 'cloud_timeout_ms');
  assert.equal(resolveConfig(syntheticValues({ bind_address: '::1' })).bindAddress, '::1');
  assert.equal(resolveConfig(syntheticValues({ bind_address: 'localhost' })).bindAddress, 'localhost');
  assert.equal(resolveConfig(syntheticValues({ port: '8091' })).port, 8091);
});

test('E15 stays observe_only: any other operating mode is refused at startup', () => {
  assert.throws(
    () => resolveConfig(syntheticValues({ operating_mode: 'control' })),
    (error: unknown) => error instanceof ConfigError && error.key === 'operating_mode' && error.message.includes('observe_only'),
  );
  assert.equal(resolveConfig(syntheticValues({ operating_mode: 'observe_only' })).operatingMode, 'observe_only');
  assert.equal(resolveConfig(syntheticValues({ operating_mode: '' })).operatingMode, 'observe_only');
});

test('options file values are merged and overridden by the environment', async () => {
  const files: Record<string, string> = {
    '/options.json': JSON.stringify({ token: TOKEN, email: EMAIL, password: PASSWORD, country: 'de', port: 9000, data_dir: null }),
  };
  const io = { readFile: async (path: string) => files[path] ?? Promise.reject(new Error('ENOENT')) };
  const fromFile = await loadConfig({ [ENV.options_file]: '/options.json' }, io);
  assert.equal(fromFile.port, 9000);
  assert.equal(fromFile.credentials.country, 'DE');
  assert.equal(fromFile.dataDir, DEFAULT_DATA_DIR);
  const overridden = await loadConfig({ [ENV.options_file]: '/options.json', [ENV.port]: '9001', [ENV.country]: 'nl' }, io);
  assert.equal(overridden.port, 9001);
  assert.equal(overridden.credentials.country, 'NL');
  const fromEnvironment = await loadConfig(
    { [ENV.token]: TOKEN, [ENV.email]: EMAIL, [ENV.password]: PASSWORD, [ENV.country]: 'nl', [ENV.data_dir]: '/private/mower' },
    io,
  );
  assert.equal(fromEnvironment.dataDir, '/private/mower');
  await assert.rejects(loadConfig({ [ENV.options_file]: '/missing.json' }, io), (error: unknown) => error instanceof ConfigError && error.key === 'options_file');
  await assert.rejects(loadConfig({}, io), (error: unknown) => error instanceof ConfigError && error.key === 'token');
});

test('options file rejects unknown keys, nested values, non-objects and oversized input', () => {
  assert.throws(() => parseOptions('[]'), (error: unknown) => error instanceof ConfigError && error.key === 'options_file');
  assert.throws(() => parseOptions('not json'), (error: unknown) => error instanceof ConfigError && error.key === 'options_file');
  assert.throws(() => parseOptions('{"camera_token":"x"}'), (error: unknown) => error instanceof ConfigError && error.message.includes('camera_token'));
  assert.throws(() => parseOptions('{"token":{"value":"x"}}'), (error: unknown) => error instanceof ConfigError && error.key === 'token');
  assert.throws(() => parseOptions('{"port":8.5}'), (error: unknown) => error instanceof ConfigError && error.key === 'port');
  assert.throws(() => parseOptions(`{"token":"${'x'.repeat(70_000)}"}`), (error: unknown) => error instanceof ConfigError && error.message.includes('too large'));
  assert.deepEqual(parseOptions('{"port":8090,"country":"nl","token":null}'), { port: 8090, country: 'nl' });
});

test('the startup description never contains the token or credentials', () => {
  const description = describeConfig(resolveConfig(syntheticValues()));
  assertNoSecrets(JSON.stringify(description));
  assert.deepEqual(Object.keys(description).sort(), ['bind_address', 'cloud_timeout_ms', 'country', 'data_dir', 'operating_mode', 'port']);
});
