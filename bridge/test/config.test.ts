import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ConfigError,
  DEFAULT_BIND_ADDRESS,
  DEFAULT_CLOUD_TIMEOUT_MS,
  DEFAULT_CONTROL_MAX_STATE_AGE_MS,
  DEFAULT_CONTROL_READ_BACK_MS,
  DEFAULT_DATA_DIR,
  DEFAULT_LOCAL_TIMEOUT_MS,
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
    localTimeoutMs: DEFAULT_LOCAL_TIMEOUT_MS,
    hosts: {},
    host: null,
    control: null,
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

test('observe_only is the default and control needs the explicit stop route opt-in', () => {
  assert.equal(resolveConfig(syntheticValues({ operating_mode: 'observe_only' })).operatingMode, 'observe_only');
  assert.equal(resolveConfig(syntheticValues({ operating_mode: '' })).operatingMode, 'observe_only');
  rejects(syntheticValues({ operating_mode: 'manual' }), 'operating_mode');
  rejects(syntheticValues({ operating_mode: 'Control' }), 'operating_mode');
  rejects(syntheticValues({ operating_mode: 'control' }), 'control_stop_route');
  rejects(syntheticValues({ operating_mode: 'control', control_stop_route: '   ' }), 'control_stop_route');
  rejects(syntheticValues({ operating_mode: 'control', control_stop_route: 'x'.repeat(201) }), 'control_stop_route');
  rejects(syntheticValues({ operating_mode: 'control', control_stop_route: 'pause\nthen return' }), 'control_stop_route');
  rejects(syntheticValues({ operating_mode: 'control', control_stop_route: 'pause then the app', control_max_state_age_ms: 999 }), 'control_max_state_age_ms');
  rejects(syntheticValues({ operating_mode: 'control', control_stop_route: 'pause then the app', control_max_state_age_ms: 300_001 }), 'control_max_state_age_ms');
  rejects(syntheticValues({ operating_mode: 'control', control_stop_route: 'pause then the app', control_read_back_ms: 60_001 }), 'control_read_back_ms');
  const control = resolveConfig(syntheticValues({ operating_mode: 'control', control_stop_route: '  pause then the app  ' }));
  assert.equal(control.operatingMode, 'control');
  assert.deepEqual(control.control, { stopRoute: 'pause then the app', maxStateAgeMs: DEFAULT_CONTROL_MAX_STATE_AGE_MS, readBackMs: DEFAULT_CONTROL_READ_BACK_MS });
  const tuned = resolveConfig(syntheticValues({ operating_mode: 'control', control_stop_route: 'pause then the app', control_max_state_age_ms: '15000', control_read_back_ms: 45_000 }));
  assert.deepEqual(tuned.control, { stopRoute: 'pause then the app', maxStateAgeMs: 15_000, readBackMs: 45_000 });
  const ignored = resolveConfig(syntheticValues({ control_stop_route: 'pause then the app', control_read_back_ms: 45_000 }));
  assert.equal(ignored.control, null, 'control options without the mode change nothing');
  const description = describeConfig(control);
  assert.equal(description.operating_mode, 'control');
  assert.equal(description.control_max_state_age_ms, DEFAULT_CONTROL_MAX_STATE_AGE_MS);
  assert.equal(description.control_read_back_ms, DEFAULT_CONTROL_READ_BACK_MS);
  assert.ok(!JSON.stringify(description).includes('pause then the app'), 'the stop route is not logged');
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
  const controlled = await loadConfig(
    { [ENV.options_file]: '/options.json', [ENV.operating_mode]: 'control', [ENV.control_stop_route]: 'pause then the app', [ENV.control_read_back_ms]: '30000' },
    io,
  );
  assert.deepEqual(controlled.control, { stopRoute: 'pause then the app', maxStateAgeMs: DEFAULT_CONTROL_MAX_STATE_AGE_MS, readBackMs: 30_000 });
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
  assert.deepEqual(parseOptions('{"operating_mode":"control","control_stop_route":"pause then the app","control_max_state_age_ms":15000}'), {
    operating_mode: 'control',
    control_stop_route: 'pause then the app',
    control_max_state_age_ms: 15000,
  });
});

test('the startup description never contains the token or credentials', () => {
  const description = describeConfig(resolveConfig(syntheticValues()));
  assertNoSecrets(JSON.stringify(description));
  assert.deepEqual(Object.keys(description).sort(), ['bind_address', 'cloud_timeout_ms', 'configured_hosts', 'country', 'data_dir', 'local_timeout_ms', 'operating_mode', 'port']);
  const withHosts = describeConfig(resolveConfig(syntheticValues({ host: '192.0.2.10', hosts: `${'a'.repeat(64)}=192.0.2.11` })));
  assert.equal(withHosts.configured_hosts, 2);
  assert.ok(!JSON.stringify(withHosts).includes('192.0.2.1'), 'hosts are counted, never listed');
});

test('LAN hosts are validated per 64-character mower id and the single host stays optional', () => {
  const idA = 'a'.repeat(64);
  const idB = 'b'.repeat(64);
  assert.deepEqual(resolveConfig(syntheticValues({ hosts: `${idA}=192.0.2.10, ${idB}=mower.lan` })).hosts, { [idA]: '192.0.2.10', [idB]: 'mower.lan' });
  assert.deepEqual(resolveConfig(syntheticValues({ hosts: { [idA]: '2001:db8::10' } })).hosts, { [idA]: '2001:db8::10' });
  assert.deepEqual(resolveConfig(syntheticValues({ hosts: '' })).hosts, {});
  assert.equal(resolveConfig(syntheticValues({ host: '192.0.2.10' })).host, '192.0.2.10');
  assert.equal(resolveConfig(syntheticValues({ local_timeout_ms: 8000 })).localTimeoutMs, 8000);
  rejects(syntheticValues({ hosts: 'x=192.0.2.10' }), 'hosts');
  rejects(syntheticValues({ hosts: `${idA}=not a host` }), 'hosts');
  rejects(syntheticValues({ hosts: `${idA}=192.0.2.10,${idA}=192.0.2.11` }), 'hosts');
  rejects(syntheticValues({ hosts: idA }), 'hosts');
  rejects(syntheticValues({ host: 'bad host' }), 'host');
  rejects(syntheticValues({ local_timeout_ms: 500 }), 'local_timeout_ms');
  assert.deepEqual(parseOptions(`{"hosts":{"${idA}":"192.0.2.10"}}`), { hosts: { [idA]: '192.0.2.10' } });
  assert.throws(() => parseOptions('{"hosts":"x"}'), (error: unknown) => error instanceof ConfigError && error.key === 'hosts');
  assert.throws(() => parseOptions('{"hosts":{"a":1}}'), (error: unknown) => error instanceof ConfigError && error.key === 'hosts');
});
