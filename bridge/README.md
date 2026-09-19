# Eufy Robomow mower bridge

Dedicated Node 24 service that owns the mower side of this project. It consumes
[`@keesmod/eufy-mega-client`](https://github.com/keesmod/eufy-mega-client) as a
library and instantiates only its mower module (`EufyClient.mowers`). It has its
own configuration, credentials, session file, private HTTP endpoint and
lifecycle. It works with no camera bridge, camera credentials or camera
repository present, and the camera bridge in `ha-eufy-cam` works without it.

Version 0.1.0 is the lifecycle foundation from issue #12. It starts, keeps one
private session, answers one authenticated state request and stops cleanly. It
exposes no mower data and issues no mower command.

## What it does

- Validates every option at startup and refuses to start on any invalid value.
- Creates the mower data directory with mode `0700` and writes the session and
  the bridge identity as `0600` files with atomic replacement.
- Constructs one library client with only the mower module. Construction makes
  no network request.
- Listens on a private HTTP port. Every request needs the bearer token, checked
  in constant time. The only route is `GET /v1/state`.
- Performs exactly one explicit authentication attempt after listening. The
  result is recorded in the state document. Nothing is retried.
- Stops on `SIGTERM` or `SIGINT`: cancels an in-flight authentication, closes
  the server and all connections, shuts the library client down and flushes
  files. Startup, authentication and shutdown each have a deadline.

## What it does not do yet

- No discovery, telemetry, control, settings or map routes. The state document
  reports `routes` as all `false`.
- No physical mower control. `operating_mode` accepts only `observe_only` and
  the bridge refuses to start with any other value.
- No captcha or verification flow. Those authentication states are reported
  but cannot be answered through this version.
- No container image or Home Assistant app packaging. No connection from the
  Python integration, which still uses its own local backend.

The follow-up order is recorded in issue #12.

## Configuration

Values come from the environment or from a JSON options file named by
`EUFY_MOWER_OPTIONS_FILE`. Environment values override file values. The file
may contain only the keys below, as strings or integers, and must stay under
64 KiB.

| Environment variable          | Options key       | Required | Default            | Rule                                                  |
| ----------------------------- | ----------------- | -------- | ------------------ | ----------------------------------------------------- |
| `EUFY_MOWER_BRIDGE_TOKEN`     | `token`           | yes      |                    | 32 to 256 printable ASCII characters, no spaces       |
| `EUFY_MOWER_EMAIL`            | `email`           | yes      |                    | Eufy account email                                    |
| `EUFY_MOWER_PASSWORD`         | `password`        | yes      |                    | Eufy account password, at most 1024 characters        |
| `EUFY_MOWER_COUNTRY`          | `country`         | yes      |                    | Two-letter country code                               |
| `EUFY_MOWER_PORT`             | `port`            | no       | `8090`             | 1 to 65535                                            |
| `EUFY_MOWER_BIND_ADDRESS`     | `bind_address`    | no       | `127.0.0.1`        | IP address or `localhost`                             |
| `EUFY_MOWER_DATA_DIR`         | `data_dir`        | no       | `/data/eufy-mower` | Absolute path, private to this bridge                 |
| `EUFY_MOWER_OPERATING_MODE`   | `operating_mode`  | no       | `observe_only`     | Only `observe_only` is accepted in this version       |
| `EUFY_MOWER_CLOUD_TIMEOUT_MS` | `cloud_timeout_ms`| no       | `15000`            | 1000 to 60000, deadline per Eufy Home or Tuya request |

Use a token, credentials, data directory and port that are different from any
camera bridge installation. The account password is needed by the library for
session renewal. Protect the environment, the options file and the data
directory accordingly. Nothing in the state document or the logs contains the
token, the credentials or the session.

## Private API

`GET /v1/state` with header `Authorization: Bearer <token>` returns, for example:

```json
{
  "protocol": 1,
  "bridge": "eufy-robomow-bridge",
  "version": "0.1.0",
  "bridge_id": "00000000-0000-4000-8000-000000000000",
  "lifecycle": "running",
  "operating_mode": "observe_only",
  "auth": { "state": "disconnected", "last_error": "authentication_failed", "attempted_at": "2026-09-19T10:00:00.000Z" },
  "client": { "package": "@keesmod/eufy-mega-client", "version": "0.13.0", "module": "mowers", "lifecycle": "open", "connected": false },
  "routes": { "discovery": false, "state": false, "control": false, "maps": false }
}
```

`auth.state` is the library's authentication state. `auth.last_error` is the
stable library or bridge error code of the last explicit attempt, or `null`
after success. A missing or wrong token gets `401`, another path `404` and
another method `405`. Responses are never cached.

## Data directory

| File                 | Mode   | Content                                                 |
| -------------------- | ------ | ------------------------------------------------------- |
| `mower-session.json` | `0600` | Opaque library session `{ "version": 1, "data": "…" }` |
| `bridge-id`          | `0600` | Random UUID created once, reported as `bridge_id`      |

The session belongs to the library adapter and is never inspected, logged or
served. Delete the file to force a fresh login on the next start.

## Running

```bash
npm ci --ignore-scripts
npm run build
EUFY_MOWER_BRIDGE_TOKEN=… EUFY_MOWER_EMAIL=… EUFY_MOWER_PASSWORD=… EUFY_MOWER_COUNTRY=NL EUFY_MOWER_DATA_DIR=/private/eufy-mower npm start
```

Exit codes: `78` for an invalid configuration, `1` for a failed startup or an
incomplete shutdown, `0` after a clean stop. Shutdown waits at most 15 seconds
for the library and exits with `1` twenty seconds after the signal at the
latest.

## Development

```bash
npm run typecheck
npm test
npm audit --package-lock-only --ignore-scripts --include=dev --include=optional --include=peer
```

Tests run on Node's own TypeScript type stripping and need no network, no
camera service and no hardware. They cover configuration rules, private file
permissions, the bearer check, the idle lifecycle, a failed and a successful
synthetic authentication, bounded and cancelled authentication, a port in use
and incomplete or overdue shutdown. Every lifecycle test asserts that no
socket, listener or referenced timer remains afterwards.

The library is pinned to the exact release tarball and its `sha512` integrity
in `package-lock.json`. A test fails when the pin becomes a branch, tag or
commit reference. Bump the pin by changing the URL in `package.json`, the
constants in `src/version.ts` and running `npm install --ignore-scripts`.
