import { timingSafeEqual } from 'node:crypto';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';

export const STATE_PATH = '/v1/state';

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

/**
 * Private HTTP shell. Every request needs the bearer token. The only route is the read-only state
 * document. Request bodies are discarded and every timeout is bounded.
 */
export function createPrivateServer(token: string, state: () => unknown): Server {
  const server = createServer((request, response) => {
    request.resume();
    if (!authorized(request, token)) {
      json(response, 401, { error: 'unauthorized' }, { 'WWW-Authenticate': 'Bearer' });
      return;
    }
    const url = new URL(request.url ?? '/', 'http://bridge');
    if (url.pathname !== STATE_PATH) {
      json(response, 404, { error: 'not_found' });
      return;
    }
    if (request.method !== 'GET') {
      json(response, 405, { error: 'method_not_allowed' }, { Allow: 'GET' });
      return;
    }
    json(response, 200, state());
  });
  server.requestTimeout = 10_000;
  server.headersTimeout = 5_000;
  server.keepAliveTimeout = 5_000;
  server.maxHeadersCount = 64;
  return server;
}
