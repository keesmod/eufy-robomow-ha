import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { promisify } from 'node:util';
import { MowerBridge } from '../src/bridge.ts';
import { ENV } from '../src/config.ts';
import { EMAIL, PASSWORD, TOKEN, assertNoSecrets, temporaryDirectory, testConfig } from './helpers.ts';

const run = promisify(execFile);
const script = join(import.meta.dirname, '..', 'src', 'healthcheck.ts');

async function healthcheck(env: Record<string, string>): Promise<{ code: number; stdout: string; stderr: string }> {
  try {
    const { stdout, stderr } = await run(process.execPath, [script], { env: { PATH: process.env.PATH ?? '', ...env }, timeout: 15_000 });
    return { code: 0, stdout, stderr };
  } catch (error) {
    const failure = error as { code?: number; stdout?: string; stderr?: string };
    return { code: typeof failure.code === 'number' ? failure.code : 1, stdout: failure.stdout ?? '', stderr: failure.stderr ?? '' };
  }
}

function environment(port: number, overrides: Record<string, string> = {}): Record<string, string> {
  return {
    [ENV.token]: TOKEN,
    [ENV.email]: EMAIL,
    [ENV.password]: PASSWORD,
    [ENV.country]: 'NL',
    [ENV.port]: String(port),
    [ENV.bind_address]: '0.0.0.0',
    [ENV.data_dir]: '/unused',
    ...overrides,
  };
}

async function runningBridge(t: TestContext): Promise<number> {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const bridge = new MowerBridge(testConfig(directory.path), {
    adapter: () => {
      throw new Error('the health check never authenticates');
    },
  });
  t.after(() => bridge.stop().catch(() => {}));
  await bridge.start();
  const address = bridge.address;
  assert.ok(address);
  return address.port;
}

test('the health check passes only for a running bridge reached with the token on loopback', async (t) => {
  const port = await runningBridge(t);
  const healthy = await healthcheck(environment(port));
  assert.equal(healthy.code, 0, healthy.stderr);
  const summary = JSON.parse(healthy.stdout.trim()) as Record<string, unknown>;
  assert.equal(summary.status, 200);
  assert.equal(summary.lifecycle, 'running');
  assert.equal(summary.auth, 'disconnected');
  assert.equal(summary.last_error, null);
  assert.equal(typeof summary.version, 'string');
  assertNoSecrets(healthy.stdout + healthy.stderr);

  const wrongToken = await healthcheck(environment(port, { [ENV.token]: 'wrong-synthetic-token-0123456789abcdef' }));
  assert.equal(wrongToken.code, 1);
  assert.equal((JSON.parse(wrongToken.stdout.trim()) as { status: number }).status, 401);
  assertNoSecrets(wrongToken.stdout + wrongToken.stderr);
});

test('the health check fails without a listener or with an invalid configuration', async (t) => {
  const port = await runningBridge(t);
  const unreachable = await healthcheck(environment(port + 1 > 65_535 ? port - 1 : port + 1));
  assert.equal(unreachable.code, 1);
  assert.match(unreachable.stderr, /state route unreachable/);
  const invalid = await healthcheck(environment(port, { [ENV.token]: '' }));
  assert.equal(invalid.code, 1);
  assert.match(invalid.stderr, /configuration invalid: token/);
  assertNoSecrets(unreachable.stderr + invalid.stderr);
});
