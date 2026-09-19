# Eufy Robomow mower bridge

Dedicated Node 24 service that owns the mower side of this project. It consumes
[`@keesmod/eufy-mega-client`](https://github.com/keesmod/eufy-mega-client) as a
library and instantiates only its mower module (`EufyClient.mowers`). It has its
own configuration, credentials, session file, private HTTP endpoint and
lifecycle. It works with no camera bridge, camera credentials or camera
repository present, and the camera bridge in `ha-eufy-cam` works without it.

Version 0.2.0 added the first read-only routes from issue #17 on the lifecycle
foundation from issue #12: discovery of the account's E15 mowers and one typed
state query per mower over the LAN. Version 0.3.0 added the container image,
the local Home Assistant app candidate and the health check from issue #23.
Version 0.4.0 pins library 0.15.0, whose E15 registry confirms the DP 107
activities `mowing`, `paused` and `returning`, so the state route reports
`status` for those payloads. Version 0.5.0 pins library 0.16.0 and adds the
opt-in control routes from issue #19: `operating_mode: control` with a
required stop route, and `POST /v1/mowers/{id}/commands/{class}` for `start`,
`pause` and `resume`. In the default `observe_only` mode nothing changed and
every command route answers `403`.

## What it does

- Validates every option at startup and refuses to start on any invalid value.
- Creates the mower data directory with mode `0700` and writes the session and
  the bridge identity as `0600` files with atomic replacement.
- Constructs one library client with only the mower module. Construction makes
  no network request.
- Listens on a private HTTP port. Every request needs the bearer token, checked
  in constant time. The read routes are `GET`. The single write route is
  `POST` and works only in `control` mode.
- Performs exactly one explicit authentication attempt after listening, then
  one discovery when that succeeded. Results are recorded in the state
  document. Nothing is retried.
- Serves the discovered mower list from a cache. A request refreshes it only
  when the list is older than ten minutes, at most once per minute, and an
  older list stays available with the failure code when a refresh fails.
- Answers a state request with one bounded LAN session: open, one typed query,
  close. Concurrent requests for the same mower share that session. After a
  failure the last good result is served as stale with the failure code.
- In `control` mode, answers a command request with one bounded LAN session
  after checking on the bridge that the class is routed, the mower is
  discovered with a configured host, no other command owns it and the last
  successful state observation is younger than the documented maximum age.
  The library runs one fresh status query, writes one declared boolean point
  and reads the lifecycle back from fresh reports. The outcome is served as
  `confirmed`, `failed` or `uncertain` and is never retried or replayed.
- Stops on `SIGTERM` or `SIGINT`: cancels an in-flight authentication, closes
  the server and all connections, shuts the library client down and flushes
  files. Startup, authentication and shutdown each have a deadline.

## What it does not do yet

- No settings or map routes. The state document reports `routes.maps` as
  `false`.
- No physical mower control in the default `observe_only` mode. Every command
  route answers `403 control_disabled` there and never reaches the library.
  `control` mode needs the explicit stop route opt-in at startup.
- No `return` route. The library declares `return` over DP 3 `switch_charge`,
  but the owned E15 on firmware 6.9.28 ignored that write in the library's
  [command window receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_COMMAND_WINDOW_2026-09-19.md),
  so `POST …/commands/return` answers `409 command_unsupported` until the
  library has a working return route (keesmod/eufy-mega-client#173). The
  official app remains the return route.
- No stop, settings, zone or scheduling command.
- No polling, reconnect or spontaneous report stream. Every LAN session is
  opened by a request and closed after its query.
- `status` reports only the three confirmed E15 activities. No E15 payload
  identifies `docked`, `charging`, `idle` or `error`, so those values are
  never served and never inferred from age, absence or inactivity. Mowing
  progress is `unconfirmed` in the library's E15 registry, so `progress`
  carries no value. See [E15 activity](#e15-activity).
- No captcha or verification flow. Those authentication states are reported
  but cannot be answered through this version.
- No published image and no app repository listing. The image and the app
  candidate are built locally, see the
  [deployment guide](../docs/bridge-deployment.md).

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
| `EUFY_MOWER_OPERATING_MODE`   | `operating_mode`  | no       | `observe_only`     | `observe_only` or `control`                            |
| `EUFY_MOWER_CLOUD_TIMEOUT_MS` | `cloud_timeout_ms`| no       | `15000`            | 1000 to 60000, deadline per Eufy Home or Tuya request |
| `EUFY_MOWER_LOCAL_TIMEOUT_MS` | `local_timeout_ms`| no       | `5000`             | 1000 to 60000, deadline for connecting and for each LAN query |
| `EUFY_MOWER_HOST`             | `host`            | no       |                    | LAN address of the mower, used only when exactly one mower is discovered |
| `EUFY_MOWER_HOSTS`            | `hosts`           | no       |                    | `id=host` pairs separated by commas, or an object of id to host in the file |
| `EUFY_MOWER_CONTROL_STOP_ROUTE` | `control_stop_route` | in `control` mode | | 1 to 200 printable ASCII characters. The operator's own words for how the mower is stopped when a command misbehaves, for example `pause here, then Stop and Charge in the eufy app`. Passed to the library opt-in, never logged or served |
| `EUFY_MOWER_CONTROL_MAX_STATE_AGE_MS` | `control_max_state_age_ms` | no | `30000` | 1000 to 300000. A command is refused when the last successful state observation of that mower is older |
| `EUFY_MOWER_CONTROL_READ_BACK_MS` | `control_read_back_ms` | no | `20000` | 1000 to 60000. How long the library waits for fresh reports after every write |

Mower ids are the opaque 64-character identifiers from `GET /v1/mowers`. They
are account-scoped and stable while the private session identity is kept. A
household with one mower sets `EUFY_MOWER_HOST` once. `hosts` takes precedence
and is required when more than one mower is discovered. Hosts are IP literals
or host names and are never served by the API.

Use a token, credentials, data directory and port that are different from any
camera bridge installation. The account password is needed by the library for
session renewal. Protect the environment, the options file and the data
directory accordingly. Nothing in the state document or the logs contains the
token, the credentials or the session.

## Private API

Every request carries `Authorization: Bearer <token>`. A missing or wrong token
gets `401`, an unknown path `404`, another method `405`, a command outside
`control` mode `403`, a command the bridge refuses before any write `409` and a
route failure `503`, each with only a stable code in `{ "error": "…" }`.
Responses are never cached.

### `GET /v1/state`

Bridge state, for example:

```json
{
  "protocol": 1,
  "bridge": "eufy-robomow-bridge",
  "version": "0.5.0",
  "bridge_id": "00000000-0000-4000-8000-000000000000",
  "lifecycle": "running",
  "operating_mode": "observe_only",
  "auth": { "state": "disconnected", "last_error": "authentication_failed", "attempted_at": "2026-09-19T10:00:00.000Z" },
  "client": { "package": "@keesmod/eufy-mega-client", "version": "0.16.0", "module": "mowers", "lifecycle": "open", "connected": false },
  "mowers": { "count": null, "discovered_at": null, "error": "authentication_required" },
  "routes": { "discovery": true, "state": true, "control": false, "maps": false },
  "control": null
}
```

`auth.state` is the library's authentication state. `auth.last_error` is the
stable library or bridge error code of the last explicit attempt, or `null`
after success. `mowers` summarises the discovery cache. In `control` mode
`routes.control` is `true` and `control` carries the opt-in in effect, for
example `{ "classes": ["start", "pause", "resume"], "max_state_age_ms": 30000, "read_back_ms": 20000 }`.
The stop route text is never served.

### `GET /v1/mowers`

Contract 1. The discovered E15 mowers, for example:

```json
{
  "contract": 1,
  "discovered_at": "2026-09-19T10:00:00.000Z",
  "fresh": true,
  "error": null,
  "mowers": [
    { "id": "<64 hex characters>", "kind": "mower", "model": "E15", "productCode": "T2880", "state_available": true }
  ]
}
```

`id` is the library's opaque account-scoped identifier. `state_available` is
true when a LAN host is configured for that id. `fresh` becomes false when the
list is older than ten minutes or the last refresh failed, in which case
`error` carries the failure code and the older list is still served. Without
any list the response is `503` with the code, for example
`authentication_required` before the module is connected. No local key, cloud
session, user name, host or raw cloud response is ever included.

### `GET /v1/mowers/{id}/state`

Contract 1. One read-only local query, for example:

```json
{
  "contract": 1,
  "id": "<64 hex characters>",
  "source": "local-tuya-3.5",
  "observed_at": "2026-09-19T10:00:01.250Z",
  "age_ms": 12,
  "stale": false,
  "error": null,
  "status": { "state": "reported", "value": "mowing", "dp": ["107", "107", "107"], "source": "local-tuya-3.5", "observedAt": "2026-09-19T10:00:01.250Z" },
  "battery": { "state": "reported", "value": { "percent": 85 }, "dp": ["8"], "source": "local-tuya-3.5", "observedAt": "2026-09-19T10:00:01.250Z" },
  "progress": { "state": "unconfirmed" },
  "network": { "state": "reported", "value": { "kind": "wifi", "signalPercent": 70 }, "dp": ["134", "109"], "source": "local-tuya-3.5", "observedAt": "2026-09-19T10:00:01.250Z" }
}
```

The four typed fields are the library's `MowerTelemetry` fields, each in the
state `reported`, `missing`, `invalid` or `unconfirmed`. `observed_at` is the
local receipt time of the query reply and `age_ms` its age against the bridge
clock when the response was built. Raw data points are never included, so the
DP 107 payload itself is never served.

#### E15 activity

`status` is whatever library 0.16.0 reports, unchanged. The library is pinned
to the release tarball with SHA-256 `021186b38ff4d4c058e7e0f8406214737a61e5b9248235556bf6affd3180d184`
(source commit `f29df02e`). Its E15 registry confirms three DP 107
`robot_status` payloads on the owned E15 (T2880, firmware 6.9.28, Anker eufy
app 6.1.00): fields 1 = 2 and 3 = 1 `mowing`, fields 1 = 2 and 3 = 2 `paused`
and fields 1 = 1 and 3 = 1 `returning`, each reproduced through owner-operated
start, pause and return cycles in the library's
[reproduction receipt](https://github.com/keesmod/eufy-mega-client/blob/ee1ac36bead945445aee63fe049b295e81b9eafc/docs/research/E15_ROBOT_STATUS_REPRODUCTION_2026-09-19.md)
on top of its [contract receipt](https://github.com/keesmod/eufy-mega-client/blob/ee1ac36bead945445aee63fe049b295e81b9eafc/docs/research/E15_ROBOT_STATUS_CONTRACT_2026-09-16.md).
`dp` lists the data point once per confirmed definition.

- `reported` carries the activity and the observation time of the query that
  contained the payload. The app's Defogging phase shares the `mowing` payload
  and is reported as `mowing`.
- `missing` means the query carried no DP 107.
- `invalid` means the query carried a DP 107 payload the library withholds: a
  transitional first frame, the map-saving payload, field 6 = 1 or the default
  payload.

Exact limits: no payload identifies `docked`, `charging`, `idle` or `error`,
so the bridge never reports them and dock arrival is never inferred from
inactivity. Mowing progress has no identified source and stays `unconfirmed`.
A stale document keeps the earlier `status` with `stale: true` and the failure
code, its age changes nothing.

When the query fails and an earlier query for the same id succeeded, the
earlier result is served with `stale: true`, its original `observed_at`, the
grown `age_ms` and the failure code in `error`. Without an earlier result the
response is `503` with the code. Other outcomes: `400 invalid_mower_id`,
`404 unknown_mower` for an id that discovery did not return, and
`503 mower_host_unconfigured` for a discovered mower without a LAN host.
Library codes include `authentication_required`, `mower_protocol_unavailable`,
`mower_local_unreachable`, `mower_local_authentication_failed`,
`mower_local_protocol_error`, `request_timeout` and `request_aborted`.

### `POST /v1/mowers/{id}/commands/{class}`

Contract 1. One opt-in command, `control` mode only. The class is `start`,
`pause` or `resume` in the path. Request bodies are ignored. Every check below
runs on the bridge before the library is touched, in this order:

| Status | Code                       | Reason                                                                                              |
| ------ | -------------------------- | --------------------------------------------------------------------------------------------------- |
| `403`  | `control_disabled`         | The bridge runs in `observe_only`. Nothing else is checked                                          |
| `404`  | `not_found`                | The class is not one the library declares                                                           |
| `409`  | `command_unsupported`      | `return`, which the owned firmware does not honour, or a mower that is not an E15                   |
| `400`  | `invalid_mower_id`         | Not a 64-character id                                                                               |
| `409`  | `command_in_progress`      | Another command owns this mower. One command per mower at a time                                    |
| `404`  | `unknown_mower`            | Discovery did not return the id                                                                     |
| `503`  | `mower_host_unconfigured`  | No LAN host for the id                                                                              |
| `409`  | `telemetry_stale`          | No successful state query yet, the last one failed, or its observation is older than `control_max_state_age_ms` |
| `409`  | library `mower_command_*`  | The library refused before any frame was written: `mower_command_undeclared`, `mower_command_evidence_missing`, `mower_command_map_saving` or `mower_command_already_set` |
| `503`  | library or bridge code     | The session could not be opened or was lost, for example `mower_local_unreachable`, `mower_local_disconnected` or `request_aborted` |

Poll `GET /v1/mowers/{id}/state` first. The freshness check is the age of that
observation against the bridge clock, the same clock as `age_ms`. The library
then runs its own fresh status query on the command session before the write.
A `200` carries the library's outcome:

```json
{
  "contract": 1,
  "id": "<64 hex characters>",
  "command": "start",
  "result": "confirmed",
  "write": { "dp": "1", "code": "switch_go", "value": true },
  "sent_at": "2026-09-19T16:42:38.199Z",
  "stage": "reflected",
  "end": "reflected",
  "before_observed_at": "2026-09-19T16:42:38.198Z",
  "reply": { "observed_at": "2026-09-19T16:42:38.202Z", "return_code_zero": true, "rejected": false },
  "acknowledgement": { "observed_at": "2026-09-19T16:42:39.150Z", "sequence": 63859, "dp": "1" },
  "activity": { "observed_at": "2026-09-19T16:42:39.351Z", "sequence": 63860, "value": "mowing" },
  "reports": 3
}
```

`result` is `confirmed` when a fresh DP 107 report reflected the expected
activity (`mowing` for start and resume, `paused` for pause) within the
read-back bound, `failed` when the device rejected the control frame, and
`uncertain` when the bound passed (`end: "timed_out"`) or the report limit was
reached. An uncertain command was written and must never be repeated
automatically. `stage` is the furthest stage evidenced by fresh reports,
`sent`, `acknowledged` or `reflected`, and the frame `reply` is not an
acknowledgement. Raw data points and the reports themselves are never served,
only their count. The bridge never retries, replays or reconnects, and never
touches rain or child protection. Dock arrival is not part of any command.

Evidence: the library's
[command window receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_COMMAND_WINDOW_2026-09-19.md)
recorded `start`, `pause` and `resume` reflected within 1.2 seconds on the
owned E15 (firmware 6.9.28, app 6.1.00). The bridge itself has not been run
against the mower in `control` mode, that is the hardware acceptance in
keesmod/eufy-robomow-ha#8.

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

As a container, `docker build -t eufy-mower-bridge ./bridge` produces the
pinned Node 24 image with a health check. `node dist/healthcheck.js` loads the
same configuration, calls the state route on loopback and exits 0 only while
the bridge reports `running`. The [deployment guide](../docs/bridge-deployment.md)
covers Docker, the local Home Assistant app candidate under `ha_app/`, upgrade,
backup and rollback.

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
synthetic authentication, bounded and cancelled authentication, a port in use,
incomplete or overdue shutdown, discovery caching and spacing, and the state
route's contract, error mapping, single LAN session per mower, stale
last-good results and cancellation at shutdown, and the command route's 403 in
`observe_only`, its class, id, host, freshness and ownership checks, the
confirmed, failed and uncertain outcomes, the library's typed refusals, session
failures and cancellation at shutdown. Route tests use the `openLocalSession`
seam with a synthetic session, while the library's own refusals
(`authentication_required`, `mower_protocol_unavailable`) run through the real
module. No test sends anything to a mower. Every lifecycle test asserts that no socket, listener or
referenced timer remains afterwards.

The library is pinned to the exact release tarball and its `sha512` integrity
in `package-lock.json`. A test fails when the pin becomes a branch, tag or
commit reference. Bump the pin by changing the URL in `package.json`, the
constants in `src/version.ts` and running `npm install --ignore-scripts`.
