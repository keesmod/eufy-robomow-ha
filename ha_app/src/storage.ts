import { randomUUID } from 'node:crypto';
import { mkdir, readFile, rename, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import type { MowerSession, MowerSessionStore } from '@keesmod/eufy-mega-client';

export const SESSION_FILE = 'mower-session.json';
export const IDENTITY_FILE = 'bridge-id';

function isMissing(error: unknown): boolean {
  return (error as NodeJS.ErrnoException).code === 'ENOENT';
}

/** Private files owned by this bridge. Directories are created 0700 and files are written 0600 atomically. */
export class PrivateDirectory {
  readonly path: string;
  #writes: Promise<void> = Promise.resolve();

  constructor(path: string) {
    this.path = path;
  }

  async prepare(): Promise<void> {
    await mkdir(this.path, { recursive: true, mode: 0o700 });
    if (!(await stat(this.path)).isDirectory()) throw new Error(`${this.path} is not a directory`);
  }

  async read(name: string): Promise<string | undefined> {
    try {
      return await readFile(join(this.path, name), 'utf8');
    } catch (error) {
      if (isMissing(error)) return undefined;
      throw error;
    }
  }

  /** Serialized atomic replace so a crash never leaves a partial file. */
  write(name: string, value: string): Promise<void> {
    const job = this.#writes.then(async () => {
      const temporary = join(this.path, `.${randomUUID()}.tmp`);
      await writeFile(temporary, value, { mode: 0o600 });
      await rename(temporary, join(this.path, name));
    });
    this.#writes = job.catch(() => {});
    return job;
  }

  /** Creates the file only when it does not exist yet. Returns false when another writer won. */
  async writeOnce(name: string, value: string): Promise<boolean> {
    try {
      await writeFile(join(this.path, name), value, { mode: 0o600, flag: 'wx' });
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EEXIST') return false;
      throw error;
    }
  }

  async flush(): Promise<void> {
    await this.#writes;
  }
}

function isSession(value: unknown): value is MowerSession {
  return (
    !!value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    (value as MowerSession).version === 1 &&
    typeof (value as MowerSession).data === 'string'
  );
}

/**
 * MowerSessionStore backed by one private file in the mower's own data directory.
 * The session is an opaque secret owned by the library adapter. It is never inspected or logged.
 */
export class MowerSessionFile implements MowerSessionStore {
  readonly #directory: PrivateDirectory;
  readonly #name: string;

  constructor(directory: PrivateDirectory, name: string = SESSION_FILE) {
    this.#directory = directory;
    this.#name = name;
  }

  async load(): Promise<MowerSession | undefined> {
    const source = await this.#directory.read(this.#name);
    if (source === undefined) return undefined;
    const parsed: unknown = JSON.parse(source);
    if (!isSession(parsed)) throw new Error('session_unreadable');
    return { version: 1, data: parsed.data };
  }

  async save(session: MowerSession): Promise<void> {
    if (!isSession(session)) throw new Error('session_invalid');
    await this.#directory.write(this.#name, JSON.stringify({ version: 1, data: session.data }));
  }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Stable random identity for this installation, created once and kept in the data directory. */
export async function bridgeIdentity(directory: PrivateDirectory): Promise<string> {
  const existing = await directory.read(IDENTITY_FILE);
  if (existing !== undefined) {
    const value = existing.trim();
    if (!UUID.test(value)) throw new Error(`${IDENTITY_FILE} is not a valid identity`);
    return value;
  }
  const created = randomUUID();
  if (await directory.writeOnce(IDENTITY_FILE, `${created}\n`)) return created;
  return bridgeIdentity(directory);
}
