import { request } from 'node:http';
import { loadConfig } from './config.ts';

/**
 * Container health check. Loads the same configuration as the bridge, asks the private state
 * route on loopback with the bearer token and exits 0 only when the bridge reports `running`.
 * It prints one JSON line without the token, credentials or session.
 */
const TIMEOUT_MS = 4_000;

function loopback(bindAddress: string): string {
  if (bindAddress === '0.0.0.0') return '127.0.0.1';
  if (bindAddress === '::') return '::1';
  return bindAddress;
}

async function main(): Promise<number> {
  let config;
  try {
    config = await loadConfig(process.env);
  } catch (error) {
    console.error(`healthcheck: configuration invalid: ${error instanceof Error ? error.message : 'unexpected error'}`);
    return 1;
  }
  const state = await new Promise<{ status: number; body: string }>((resolve, reject) => {
    const client = request(
      {
        host: loopback(config.bindAddress),
        port: config.port,
        path: '/v1/state',
        method: 'GET',
        headers: { Authorization: `Bearer ${config.token}`, Accept: 'application/json' },
        timeout: TIMEOUT_MS,
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on('data', (chunk: Buffer) => chunks.push(chunk));
        response.on('end', () => resolve({ status: response.statusCode ?? 0, body: Buffer.concat(chunks).toString('utf8') }));
        response.on('error', reject);
      },
    );
    client.on('timeout', () => client.destroy(new Error('timeout')));
    client.on('error', reject);
    client.end();
  }).catch((error: unknown) => {
    console.error(`healthcheck: state route unreachable: ${error instanceof Error ? error.message : 'unexpected error'}`);
    return null;
  });
  if (!state) return 1;
  let document: { lifecycle?: unknown; auth?: { state?: unknown; last_error?: unknown }; version?: unknown } = {};
  try {
    document = JSON.parse(state.body) as typeof document;
  } catch {
    document = {};
  }
  const summary = {
    status: state.status,
    lifecycle: document.lifecycle ?? null,
    version: document.version ?? null,
    auth: document.auth?.state ?? null,
    last_error: document.auth?.last_error ?? null,
  };
  console.log(JSON.stringify(summary));
  return state.status === 200 && document.lifecycle === 'running' ? 0 : 1;
}

process.exitCode = await main();
