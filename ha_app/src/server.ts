import { timingSafeEqual } from 'node:crypto';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { ApiError } from './errors.ts';

export const STATE_PATH = '/v1/state';
export const MOWERS_PATH = '/v1/mowers';
const MOWER_STATE_PATH = /^\/v1\/mowers\/([^/]{1,128})\/state$/;
/** `POST /v1/mowers/{id}/commands/{class}`. The class is addressed by path, bodies are discarded. */
const MOWER_COMMAND_PATH = /^\/v1\/mowers\/([^/]{1,128})\/commands\/([a-z]{1,16})$/;
/** `GET /v1/mowers/{id}/map`, the read-only map bundle. */
const MOWER_MAP_PATH = /^\/v1\/mowers\/([^/]{1,128})\/map$/;

/** The two request headers the map route reads. */
export interface MapRequest {
  /** `X-Eufy-Map-Mode`: `idle`, `stream` or absent. */
  mode: string | undefined;
  ifNoneMatch: string | undefined;
}

/** An answer that is not JSON: the map bundle with its headers, or its `304`. */
export class RawReply {
  readonly status: 200 | 304;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: Buffer | null;
  constructor(status: 200 | 304, headers: Readonly<Record<string, string>>, body: Buffer | null) {
    this.status = status;
    this.headers = headers;
    this.body = body;
  }
}

/** Operations behind the private API. Every method may throw an ApiError. */
export interface PrivateApi {
  state(): unknown;
  discover(): Promise<unknown>;
  mowerState(id: string): Promise<unknown>;
  /** The only write route. Refused with 403 unless the bridge runs in `control` mode. */
  command(id: string, kind: string): Promise<unknown>;
  /** Read-only. Refused with 404 unless the bridge is configured for maps. */
  map(id: string, request: MapRequest): Promise<unknown>;
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

/** The bundle's own headers, the no-store rule and its length. A 304 carries no body. */
function raw(response: ServerResponse, reply: RawReply): void {
  const headers: Record<string, string | number> = { 'Cache-Control': 'no-store', ...reply.headers };
  if (reply.body) headers['Content-Length'] = reply.body.length;
  response.writeHead(reply.status, headers);
  response.end(reply.body ?? undefined);
}

function header(request: IncomingMessage, name: string): string | undefined {
  const value = request.headers[name];
  return typeof value === 'string' ? value : undefined;
}

interface Route {
  method: 'GET' | 'POST';
  handler: () => unknown;
}

function route(api: PrivateApi, request: IncomingMessage, pathname: string): Route | undefined {
  if (pathname === STATE_PATH) return { method: 'GET', handler: () => api.state() };
  if (pathname === MOWERS_PATH) return { method: 'GET', handler: () => api.discover() };
  const mower = MOWER_STATE_PATH.exec(pathname);
  if (mower) return { method: 'GET', handler: () => api.mowerState(mower[1]!) };
  const command = MOWER_COMMAND_PATH.exec(pathname);
  if (command) return { method: 'POST', handler: () => api.command(command[1]!, command[2]!) };
  const map = MOWER_MAP_PATH.exec(pathname);
  if (map)
    return {
      method: 'GET',
      handler: () => api.map(map[1]!, { mode: header(request, 'x-eufy-map-mode'), ifNoneMatch: header(request, 'if-none-match') }),
    };
  return undefined;
}

/**
 * Private HTTP shell. Every request needs the bearer token before any route is visible. The
 * read routes are GET, the command route is POST. Request bodies are discarded and every
 * timeout is bounded. Answers are JSON except the map bundle.
 */
export function createPrivateServer(token: string, api: PrivateApi): Server {
  const server = createServer((request, response) => {
    request.resume();
    if (!authorized(request, token)) {
      json(response, 401, { error: 'unauthorized' }, { 'WWW-Authenticate': 'Bearer' });
      return;
    }
    const url = new URL(request.url ?? '/', 'http://bridge');
    const matched = route(api, request, url.pathname);
    if (!matched) {
      json(response, 404, { error: 'not_found' });
      return;
    }
    if (request.method !== matched.method) {
      json(response, 405, { error: 'method_not_allowed' }, { Allow: matched.method });
      return;
    }
    Promise.resolve()
      .then(matched.handler)
      .then(
        (value) => (value instanceof RawReply ? raw(response, value) : json(response, 200, value)),
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
