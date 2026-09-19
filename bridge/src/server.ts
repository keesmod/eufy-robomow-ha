import { timingSafeEqual } from 'node:crypto';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { ApiError } from './errors.ts';

export const STATE_PATH = '/v1/state';
export const MOWERS_PATH = '/v1/mowers';
const MOWER_STATE_PATH = /^\/v1\/mowers\/([^/]{1,128})\/state$/;

/** Read-only operations behind the private API. Every method may throw an ApiError. */
export interface PrivateApi {
  state(): unknown;
  discover(): Promise<unknown>;
  mowerState(id: string): Promise<unknown>;
}

/** Constant-time bearer comparison so the token cannot be recovered through response timing. */
export function authorized(request: IncomingMessage, token: string): boolean {
  const supplied = Buffer.from(request.headers.authorization ?? '');
  const expected = Buffer.from(`Bearer ${token}`);
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

function json(response: ServerResponse, status: number, value: unknown, headers: Record<string, string> = {}): void {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
    ...headers,
  });
  response.end(body);
}

function route(api: PrivateApi, pathname: string): (() => unknown) | undefined {
  if (pathname === STATE_PATH) return () => api.state();
  if (pathname === MOWERS_PATH) return () => api.discover();
  const mower = MOWER_STATE_PATH.exec(pathname);
  if (mower) return () => api.mowerState(mower[1]!);
  return undefined;
}

/**
 * Private HTTP shell. Every request needs the bearer token before any route is visible. All
 * routes are GET and read-only. Request bodies are discarded and every timeout is bounded.
 */
export function createPrivateServer(token: string, api: PrivateApi): Server {
  const server = createServer((request, response) => {
    request.resume();
    if (!authorized(request, token)) {
      json(response, 401, { error: 'unauthorized' }, { 'WWW-Authenticate': 'Bearer' });
      return;
    }
    const url = new URL(request.url ?? '/', 'http://bridge');
    const handler = route(api, url.pathname);
    if (!handler) {
      json(response, 404, { error: 'not_found' });
      return;
    }
    if (request.method !== 'GET') {
      json(response, 405, { error: 'method_not_allowed' }, { Allow: 'GET' });
      return;
    }
    Promise.resolve()
      .then(handler)
      .then(
        (value) => json(response, 200, value),
        (error: unknown) => {
          if (response.headersSent) {
            response.destroy();
            return;
          }
          if (error instanceof ApiError) json(response, error.status, { error: error.code });
          else json(response, 500, { error: 'internal_error' });
        },
      );
  });
  server.requestTimeout = 10_000;
  server.headersTimeout = 5_000;
  server.keepAliveTimeout = 5_000;
  server.maxHeadersCount = 64;
  return server;
}
