import assert from 'node:assert/strict';
import { readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { test } from 'node:test';
import { IDENTITY_FILE, MowerSessionFile, PrivateDirectory, SESSION_FILE, bridgeIdentity } from '../src/storage.ts';
import { temporaryDirectory } from './helpers.ts';

async function mode(path: string): Promise<number> {
  return (await stat(path)).mode & 0o777;
}

test('private directory and files are created with owner-only permissions and no partial writes', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const storage = new PrivateDirectory(directory.path);
  await storage.prepare();
  await storage.prepare();
  assert.equal(await mode(directory.path), 0o700);
  assert.equal(await storage.read('missing'), undefined);
  await Promise.all([storage.write('value', 'one'), storage.write('value', 'two')]);
  assert.equal(await storage.read('value'), 'two');
  assert.equal(await mode(join(directory.path, 'value')), 0o600);
  assert.equal(await storage.writeOnce('once', 'first'), true);
  assert.equal(await storage.writeOnce('once', 'second'), false);
  assert.equal(await storage.read('once'), 'first');
  await storage.flush();
  assert.deepEqual((await readdir(directory.path)).sort(), ['once', 'value'], 'no temporary files remain');
});

test('the mower session file round-trips the opaque secret and rejects unreadable content', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const storage = new PrivateDirectory(directory.path);
  await storage.prepare();
  const sessions = new MowerSessionFile(storage);
  assert.equal(await sessions.load(), undefined);
  await sessions.save({ version: 1, data: 'SYNTHETIC-OPAQUE' });
  assert.deepEqual(await sessions.load(), { version: 1, data: 'SYNTHETIC-OPAQUE' });
  assert.equal(await mode(join(directory.path, SESSION_FILE)), 0o600);
  assert.deepEqual(JSON.parse(await readFile(join(directory.path, SESSION_FILE), 'utf8')), { version: 1, data: 'SYNTHETIC-OPAQUE' });
  await assert.rejects(sessions.save({ version: 2, data: 'x' } as unknown as { version: 1; data: string }));
  await writeFile(join(directory.path, SESSION_FILE), '{"version":1}');
  await assert.rejects(sessions.load());
  await writeFile(join(directory.path, SESSION_FILE), 'broken');
  await assert.rejects(sessions.load());
});

test('the bridge identity is created once and stays stable', async (t) => {
  const directory = await temporaryDirectory();
  t.after(directory.remove);
  const storage = new PrivateDirectory(directory.path);
  await storage.prepare();
  const first = await bridgeIdentity(storage);
  assert.match(first, /^[0-9a-f-]{36}$/);
  assert.equal(await bridgeIdentity(storage), first);
  assert.equal(await mode(join(directory.path, IDENTITY_FILE)), 0o600);
  await writeFile(join(directory.path, IDENTITY_FILE), 'not an identity');
  await assert.rejects(bridgeIdentity(storage));
});
