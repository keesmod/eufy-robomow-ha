import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { test } from 'node:test';
import { BRIDGE_VERSION, CLIENT_INTEGRITY, CLIENT_PACKAGE, CLIENT_TARBALL, CLIENT_VERSION } from '../src/version.ts';

const root = join(import.meta.dirname, '..');

async function json(name: string): Promise<Record<string, unknown>> {
  return JSON.parse(await readFile(join(root, name), 'utf8')) as Record<string, unknown>;
}

test('the client is pinned to the exact release tarball with its recorded integrity', async () => {
  const manifest = await json('package.json');
  const lock = await json('package-lock.json');
  assert.equal(manifest.version, BRIDGE_VERSION);
  assert.deepEqual(manifest.engines, { node: '>=24.0.0' });
  const dependencies = manifest.dependencies as Record<string, string>;
  assert.deepEqual(Object.keys(dependencies), [CLIENT_PACKAGE], 'the library is the only runtime dependency');
  assert.equal(dependencies[CLIENT_PACKAGE], CLIENT_TARBALL);
  assert.match(CLIENT_TARBALL, /^https:\/\/github\.com\/keesmod\/eufy-mega-client\/releases\/download\/v(\d+\.\d+\.\d+)\/keesmod-eufy-mega-client-\1\.tgz$/);
  assert.ok(!/github:|#|\/tree\/|\/archive\//.test(dependencies[CLIENT_PACKAGE] ?? ''), 'never a branch, tag or commit reference');
  const packages = lock.packages as Record<string, Record<string, unknown>>;
  const locked = packages[`node_modules/${CLIENT_PACKAGE}`];
  assert.ok(locked, 'the lockfile records the library');
  assert.equal(locked.version, CLIENT_VERSION);
  assert.equal(locked.resolved, CLIENT_TARBALL);
  assert.equal(locked.integrity, CLIENT_INTEGRITY);
  assert.match(CLIENT_INTEGRITY, /^sha512-[A-Za-z0-9+/]{86}==$/);
  assert.equal((packages['']?.dependencies as Record<string, string>)[CLIENT_PACKAGE], CLIENT_TARBALL);
});

test('bridge sources instantiate only the mower module and no camera code', async () => {
  const directory = join(root, 'src');
  for (const name of await readdir(directory)) {
    const source = await readFile(join(directory, name), 'utf8');
    for (const forbidden of ['security:', 'EufyMegaClient', 'FileSessionStore', 'camera', 'listDevices', 'ws']) {
      assert.ok(!new RegExp(`\\b${forbidden.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(source), `${name} references ${forbidden}`);
    }
  }
  const bridge = await readFile(join(directory, 'bridge.ts'), 'utf8');
  assert.match(bridge, /new EufyClient\(\{\s*mowers:/);
  assert.equal(bridge.match(/new EufyClient\(/g)?.length, 1);
});
