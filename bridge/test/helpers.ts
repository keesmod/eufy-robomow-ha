import { mkdtemp, rm } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resolveConfig, type BridgeConfig, type OptionValues } from '../src/config.ts';

/** Synthetic values only. They resemble no real account, token or device. */
export const TOKEN = 'synthetic-bridge-token-0123456789abcdef';
export const EMAIL = 'synthetic@example.invalid';
export const PASSWORD = 'PRIVATE-PASSWORD';
export const SECRETS = [TOKEN, EMAIL, PASSWORD, 'PRIVATE-'];

export function syntheticValues(overrides: OptionValues = {}): OptionValues {
  return { token: TOKEN, email: EMAIL, password: PASSWORD, country: 'nl', ...overrides };
}

/** Test configuration on an ephemeral loopback port with a private temporary data directory. */
export function testConfig(dataDir: string, overrides: Partial<BridgeConfig> = {}): BridgeConfig {
  return {
    ...resolveConfig(syntheticValues({ data_dir: dataDir, bind_address: '127.0.0.1' })),
    port: 0,
    ...overrides,
  };
}

export async function temporaryDirectory(): Promise<{ path: string; remove: () => Promise<void> }> {
  const root = await mkdtemp(join(tmpdir(), 'eufy-mower-bridge-'));
  return { path: join(root, 'data'), remove: () => rm(root, { recursive: true, force: true }) };
}

export interface Reply {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  text: string;
  json: unknown;
}

/** One request over a fresh connection that is closed before the promise resolves. */
export function call(
  base: string,
  path: string,
  options: { token?: string; authorization?: string; method?: string; body?: string } = {},
): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const headers: Record<string, string> = {};
    if (options.token !== undefined) headers.authorization = `Bearer ${options.token}`;
    if (options.authorization !== undefined) headers.authorization = options.authorization;
    const request = httpRequest(new URL(path, base), { method: options.method ?? 'GET', agent: false, headers }, (response) => {
      const chunks: Buffer[] = [];
      response.on('data', (chunk: Buffer) => chunks.push(chunk));
      response.on('error', reject);
      response.on('close', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json: unknown;
        try {
          json = JSON.parse(text);
        } catch {
          json = undefined;
        }
        resolve({ status: response.statusCode ?? 0, headers: response.headers, text, json });
      });
    });
    request.on('error', reject);
    request.end(options.body);
  });
}

/** Sockets, listeners and referenced timers. Identical before and after a lifecycle means nothing leaked. */
export function transportHandles(): string[] {
  return process
    .getActiveResourcesInfo()
    .filter((type) => type === 'TCPServerWrap' || type === 'TCPSocketWrap' || type === 'Timeout')
    .sort();
}

export function assertNoSecrets(value: string): void {
  for (const secret of SECRETS) if (value.includes(secret)) throw new Error(`secret material leaked: ${secret}`);
}

/**
 * Closed handles leave the active list one loop iteration after their close callback. Wait briefly
 * for that, then return what is still open. A leaked server, socket or timer never disappears.
 */
export async function settledHandles(baseline: string[]): Promise<string[]> {
  const until = Date.now() + 2000;
  let current = transportHandles();
  while (Date.now() < until && JSON.stringify(current) !== JSON.stringify(baseline)) {
    await new Promise((resolve) => setTimeout(resolve, 5));
    current = transportHandles();
  }
  return current;
}

/** Baseline taken after handles closed by an earlier test have left the active list. */
export async function baselineHandles(): Promise<string[]> {
  await new Promise((resolve) => setTimeout(resolve, 20));
  return transportHandles();
}
