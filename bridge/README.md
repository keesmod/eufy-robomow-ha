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
every command route answers `403`. Version 0.6.0 pins library 0.17.0 and adds
the `stop` route from issue #31: on the owned E15 a stop over DP 1 false ends
the task and the mower returns to the dock by itself, so `stop` is the route
that brings the mower home. `return` stays unsupported. Version 0.7.0 pins
library 0.18.0 and adds the read-only map route from issue #25:
`GET /v1/mowers/{id}/map` serves the map bundle the integration already
validates, built from the library's portable map acquisition after the
library's decoder accepted the snapshot. The route exists only when the
operator supplies map provisioning, see [Map provisioning](#map-provisioning).
Version 0.7.1 names every routed command class in the `control` startup log,
`stop` included, taken from the routes themselves. Version 0.8.0 renews a
lapsed cloud session, a finding of the 2026-09-24 control window in issue #8:
the library reuses a session for at most one hour after its sign-in, and
0.7.1 signed in once, so every route answered `authentication_required` about
an hour later until a restart. The state document's `auth.state` now follows
the library. Version 0.9.0 pins library 0.19.0 and serves the running
command's progress in the state route's `command` field, so the integration
shows the drive home after a dock instead of the earlier activity. Version
0.10.0 pins library 0.20.0 and adds the settings workstream of issue #8: the
state route serves the library's typed settings, and the opt-in
`settings_mode: write` enables `POST /v1/mowers/{id}/settings/{key}` for mow
height, volume, smart no-go zones and sparse lawn optimization. Rain and child
protection and the bird-view capture stay read only in either mode. Version
0.10.1 pins library 0.22.0, which reads DP 107 as the mower's mission status:
every mowing mission, the Box, zone and scheduled tasks included, reports
`mowing` or `paused`, and a message without a mission reports `idle`.

## What it does

- Validates every option at startup and refuses to start on any invalid value.
- Creates the mower data directory with mode `0700` and writes the session and
  the bridge identity as `0600` files with atomic replacement.
- Constructs one library client with only the mower module. Construction makes
  no network request.
- Listens on a private HTTP port. Every request needs the bearer token, checked
  in constant time. The read routes are `GET`. The two write routes are
  `POST`: commands work only in `control` mode, settings only with
  `settings_mode: write`.
- Performs one explicit authentication attempt after listening, then one
  discovery when that succeeded. Results are recorded in the state document.
- Renews the cloud session when a route needs it and the library reports it
  lapsed, through one bounded attempt that concurrent routes join. A session
  that was good is renewed at once. After a failed attempt the next one waits
  at least a minute. A refused sign-in (`authentication_failed`, a captcha,
  verification or lock, an unsupported region or invalid options) is never
  repeated, restart the bridge after resolving it. No route signs in before
  the explicit startup attempt. A new session holds no device binding, so the
  next state or command route runs discovery again before its LAN session.
  Each renewal logs one line without any account data. A command is never
  retried or replayed, a renewal only happens before its write.
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
- Serves the library's typed settings with every state query, decoded from the
  same snapshot with the mower's own declaration. With `settings_mode: write`,
  answers a setting request with one bounded LAN session after checking on the
  bridge that the setting is one of the four writable ones, the value is a
  boolean or an integer, the mower is discovered with a configured host and no
  other write owns it. The library runs one fresh status query, its typed
  refusals, one write and a bounded read-back of the written value. The
  outcome is served as `confirmed`, `failed` or `uncertain` with the previous
  value, and is never retried, replayed or restored.
- With map provisioning, answers a map request at once from memory with the
  last good bundle, its entity tag and its age. The request may start one
  acquisition demand of the library's `PortableMapAcquisition` in the
  background. Only a snapshot that the library's decoder accepts replaces the
  bundle, and a failed demand keeps the last good bundle with its failure code.
- Stops on `SIGTERM` or `SIGINT`: cancels an in-flight authentication, closes
  the server and all connections, shuts the library client down and flushes
  files. Startup, authentication and shutdown each have a deadline.

## What it does not do yet

- No setting beyond the library's four writable ones: no DP 155 work
  parameters, no edge distance and no other declared point. Rain and child
  protection and the bird-view capture are read only, in either settings mode.
  Settings writes need no `control` mode and `control` mode opens no settings
  write. The map route exists only with map provisioning, which
  the bridge cannot obtain itself: the library's acquisition needs private,
  expiring `MapSessionProvisioning` from the current relay route, and the
  operator supplies it as a file. Without it the state document reports
  `routes.maps` as `false` and the map route answers `404`.
- No live map acquisition has run through the bridge. The library's
  acquisition and decoder are software-verified, a fresh acquisition decoded
  end to end on the owned E15 is still open in the library, and this bridge
  tests the route, the bundle and the demand scheduling with synthetic
  snapshots only. Map acceptance on the owned E15 is issue #8.
- No map editing, zone or selection, and no path history across demands. The
  bundle carries the three transport files as the library retained them. The
  library's `MowerPathAccumulator` is not part of this contract, the
  integration merges live coverage itself.
- No physical mower control in the default `observe_only` mode. Every command
  route answers `403 control_disabled` there and never reaches the library.
  `control` mode needs the explicit stop route opt-in at startup.
- No `return` route. The library declares `return` over DP 3 `switch_charge`,
  but the owned E15 on firmware 6.9.28 ignored that write from `paused` in the
  library's
  [command window receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_COMMAND_WINDOW_2026-09-19.md)
  and from the stopped task in its
  [stop and return receipt](https://github.com/keesmod/eufy-mega-client/blob/19d47a7144e505702e1ef98dfc7d84dfeb956cd6/docs/research/E15_STOP_RETURN_WINDOW_2026-09-20.md),
  so `POST …/commands/return` answers `409 command_unsupported`. The library
  has no DP 3 return route on this firmware. `stop` brings the mower home and
  the official app remains the only way back from a task stopped in place.
- No stop in place. The library's `stop` ends the task and the mower returns
  to the dock by itself, it never keeps the mower where it stands. No zone or
  scheduling command.
- No polling, reconnect or spontaneous report stream. Every LAN session is
  opened by a request and closed after its query. A map demand starts only
  when a map request finds one due, never on a timer of its own.
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
| `EUFY_MOWER_SETTINGS_MODE`    | `settings_mode`   | no       | `read_only`        | `read_only` or `write`. `write` enables the settings route and the library's separate settings opt-in. Independent of `operating_mode` |
| `EUFY_MOWER_CLOUD_TIMEOUT_MS` | `cloud_timeout_ms`| no       | `15000`            | 1000 to 60000, deadline per Eufy Home or Tuya request |
| `EUFY_MOWER_LOCAL_TIMEOUT_MS` | `local_timeout_ms`| no       | `5000`             | 1000 to 60000, deadline for connecting and for each LAN query |
| `EUFY_MOWER_HOST`             | `host`            | no       |                    | LAN address of the mower, used only when exactly one mower is discovered |
| `EUFY_MOWER_HOSTS`            | `hosts`           | no       |                    | `id=host` pairs separated by commas, or an object of id to host in the file |
| `EUFY_MOWER_CONTROL_STOP_ROUTE` | `control_stop_route` | in `control` mode | | 1 to 200 printable ASCII characters. The operator's own words for how the mower is stopped when a command misbehaves, for example `pause here, then Stop and Charge in the eufy app`. Passed to the library opt-in, never logged or served |
| `EUFY_MOWER_CONTROL_MAX_STATE_AGE_MS` | `control_max_state_age_ms` | no | `30000` | 1000 to 300000. A command is refused when the last successful state observation of that mower is older |
| `EUFY_MOWER_CONTROL_READ_BACK_MS` | `control_read_back_ms` | no | `20000` | 1000 to 60000. How long the library waits for fresh reports after every write |
| `EUFY_MOWER_MAP_PROVISIONING_FILE` | `map_provisioning_file` | no | | Absolute path of the operator's private map provisioning file. Enables the read-only map route. Read for every acquisition, never logged or served |
| `EUFY_MOWER_MAP_MOWER_ID` | `map_mower_id` | no | | 64-character id of the mower the provisioning belongs to. Required only when more than one mower is discovered, needs `map_provisioning_file` |

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

### Map provisioning

The library's `PortableMapAcquisition` needs private provisioning from the
verified account and the current relay route of the mower: the library's
`MapSessionProvisioning` object with `expiresAt`, `accountUid`, `peer`,
`localKey`, `password`, `motoId`, `preconnect`, `iceTokens`, `tcpToken`,
`mqtt`, `mqttHeader`, `subscribeTopics` and `publishTopic`, see the library's
[portable map acquisition](https://github.com/keesmod/eufy-mega-client/blob/1792bbc5bbe420adc54c6329bf9ea72e4e222a02/docs/MAP_ACQUISITION.md).
It expires, and the library refuses it when less than 65 seconds of validity
remain. Neither the library nor this bridge obtains it. The operator writes it
as one JSON object into a file and keeps that file fresh, for example with a
private tool of their own.

The bridge reads the file anew for every acquisition demand, so a replaced file
takes effect without a restart. The file must be a regular file of at most
64 KiB, readable by the bridge's user, not writable by group or others and not
readable by others, for example mode `0600` owned by the bridge's user.
Anything else fails that demand with `map_provisioning_unreadable` or
`map_provisioning_insecure`, and the library's own validation fails it with
`mower_map_invalid_provisioning` before any network I/O. The content is handed
to the library and never logged, served, copied into the data directory or
kept after the demand.

The provisioning belongs to one mower. With one discovered mower that is the
mower. With several, set `map_mower_id`, otherwise the map route answers
`409 map_mower_unresolved`.

## Private API

Every request carries `Authorization: Bearer <token>`. A missing or wrong token
gets `401`, an unknown path `404`, another method `405`, a command outside
`control` mode or a setting outside `settings_mode: write` `403`, a write the
bridge or the library refuses before any frame `409` and a route failure
`503`, each with only a stable code in `{ "error": "…" }`.
Responses are never cached. The map bundle is the only answer that is not
JSON.

### `GET /v1/state`

Bridge state, for example:

```json
{
  "protocol": 1,
  "bridge": "eufy-robomow-bridge",
  "version": "0.10.1",
  "bridge_id": "00000000-0000-4000-8000-000000000000",
  "lifecycle": "running",
  "operating_mode": "observe_only",
  "auth": { "state": "disconnected", "last_error": "authentication_failed", "attempted_at": "2026-09-19T10:00:00.000Z" },
  "client": { "package": "@keesmod/eufy-mega-client", "version": "0.22.0", "module": "mowers", "lifecycle": "open", "connected": false },
  "mowers": { "count": null, "discovered_at": null, "error": "authentication_required" },
  "routes": { "discovery": true, "state": true, "control": false, "maps": false, "settings": false },
  "control": null,
  "maps": null
}
```

`auth.state` is the library's authentication state, so a session whose reuse
window has passed reads as `disconnected` until a route renews it.
`auth.last_error` is the stable library or bridge error code of the last
attempt, the startup attempt or a renewal, or `null` after success, and
`auth.attempted_at` is the time of that attempt. `mowers` summarises the discovery cache. In `control` mode
`routes.control` is `true` and `control` carries the opt-in in effect, for
example `{ "classes": ["start", "pause", "resume", "stop"], "max_state_age_ms": 30000, "read_back_ms": 20000 }`.
The stop route text is never served. With `settings_mode: write`
`routes.settings` is `true`.

With map provisioning `routes.maps` is `true` and `maps` reports the
acquisition without any geometry, for example:

```json
{
  "captured_at": "2026-09-23T10:00:00.250Z",
  "age_ms": 41250,
  "stale": false,
  "error": null,
  "acquiring": false,
  "streaming": false,
  "last_demand": { "started_at": "2026-09-23T09:59:48.000Z", "ended_at": "2026-09-23T10:00:01.000Z", "end": "aborted", "cancellation_confirmed": true, "cleanup_confirmed": true, "published": 2, "rejected": 0 }
}
```

`captured_at` is the library's receipt time of the served snapshot and
`age_ms` its age against the bridge clock. `stale` is true while a bundle is
served but the last demand failed, with that demand's code in `error`.
`acquiring` shows a running demand and `streaming` an active stream lease.
`last_demand` is the last demand that reached the library: its end reason as
the library reports it, whether the peer confirmed the cancellation and the
local cleanup, and how many snapshots it published and rejected.

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
  "network": { "state": "reported", "value": { "kind": "wifi", "signalPercent": 70 }, "dp": ["134", "109"], "source": "local-tuya-3.5", "observedAt": "2026-09-19T10:00:01.250Z" },
  "settings": {
    "mow_height": { "state": "reported", "value": 40, "writable": true, "min": 25, "max": 75, "step": 1, "unit": "mm" },
    "volume": { "state": "reported", "value": 20, "writable": true, "min": 0, "max": 100, "step": 1, "unit": "%" },
    "smart_no_go_zones": { "state": "reported", "value": true, "writable": true },
    "sparse_lawn_optimization": { "state": "reported", "value": false, "writable": true },
    "rain_auto_return": { "state": "reported", "value": true, "writable": false },
    "child_lock": { "state": "reported", "value": true, "writable": false },
    "bird_view_capture": { "state": "missing" }
  },
  "command": null
}
```

The four typed fields are the library's `MowerTelemetry` fields, each in the
state `reported`, `missing`, `invalid` or `unconfirmed`. `observed_at` is the
local receipt time of the query reply and `age_ms` its age against the bridge
clock when the response was built. Raw data points are never included, so the
DP 107 payload itself is never served.

`settings` holds the library's typed settings from the same query, since
0.10.0. Each is `reported` with its `value`, `missing` when the reply did not
carry the point, or `invalid` when the value had another type or lay outside
its bound. `writable` is true when the library writes the setting and the
mower declares the point writable with the expected code and type, the
settings route itself also needs `routes.settings`. The value settings carry
the app's input bound narrowed by the mower's declaration and its unit.
`rain_auto_return` and `child_lock` are the rain and child protection, read
only in either settings mode.

`command` is the command that owns this mower right now, or `null`. While its
read-back runs it carries the library's progress, for example
`{ "command": "stop", "acknowledged_at": "…", "activity": { "observed_at": "…", "sequence": 12, "value": "returning" } }`:
the time of the acknowledgement and the latest fresh DP 107 report that decodes
to a confirmed activity. After a `stop` on the owned E15 that is `returning`
within a second, while the outcome only arrives with the map-saving payload at
the dock arrival about 30 seconds later. It comes from the library's
`onProgress` callback, never ends, repeats or changes the command, and
disappears when the command ends. The typed `status` stays what the query
reported.

#### E15 activity

`status` is whatever library 0.22.0 reports, unchanged. Since 0.22.0 the library reads DP 107 as the mower's mission status, so every mowing mission, the Box, zone and scheduled tasks included, reports `mowing` or `paused`, and a message without a mission reports `idle`. The library is pinned
to the release tarball with SHA-256 `a47771ef8cbde4f169b1e281cf0fa8d4b10b90284bd85cfed5c0b9a0bf531c93`
(source commit `ac93dc8`). Its E15 registry confirms three DP 107
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
`pause`, `resume` or `stop` in the path. Request bodies are ignored. Every check below
runs on the bridge before the library is touched, in this order:

| Status | Code                       | Reason                                                                                              |
| ------ | -------------------------- | --------------------------------------------------------------------------------------------------- |
| `403`  | `control_disabled`         | The bridge runs in `observe_only`. Nothing else is checked                                          |
| `404`  | `not_found`                | The class is not one the library declares                                                           |
| `409`  | `command_unsupported`      | `return`, whose DP 3 write the owned firmware ignores from `paused` and from the stopped task, or a mower that is not an E15 |
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
  "payload": null,
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
touches rain or child protection.

`stop` is reflected by the map-saving DP 107 payload instead of an activity,
served in `payload` as `{ "observed_at", "sequence", "name": "map_saving" }`
with `activity` null. On the owned E15 the write of DP 1 false ends the task,
the mower reports `returning` within a second and drives to the dock by
itself, and the map-saving payload is the dock arrival about 30 seconds after
the write, followed by the map save, DP 1 false and the default payload. A
confirmed `stop` therefore means the mower reached the dock, never a stop in
place. A `stop` whose read-back passed without that payload is `uncertain` and
was written. Dock arrival is not inferred from anything else and is not part
of any other command.

Evidence: the library's
[command window receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_COMMAND_WINDOW_2026-09-19.md)
recorded `start`, `pause` and `resume` reflected within 1.2 seconds on the
owned E15 (firmware 6.9.28, app 6.1.00), and its
[stop and return receipt](https://github.com/keesmod/eufy-mega-client/blob/19d47a7144e505702e1ef98dfc7d84dfeb956cd6/docs/research/E15_STOP_RETURN_WINDOW_2026-09-20.md)
recorded `stop` with `returning` 0.28 seconds after the write and the
map-saving payload at dock arrival 29.8 seconds after it, and `return` ignored
from the stopped task. The bridge itself has not been run against the mower
in `control` mode, that is the hardware acceptance in
keesmod/eufy-robomow-ha#8.

### `POST /v1/mowers/{id}/settings/{key}`

Contract 1. One opt-in setting write, `settings_mode: write` only, since
0.10.0. The key is `mow_height`, `volume`, `smart_no_go_zones` or
`sparse_lawn_optimization` in the path and the new value is the `value` query
parameter, `true` or `false` for a switch and a plain integer for a number, for
example `POST /v1/mowers/{id}/settings/mow_height?value=45`. Request bodies are
ignored. Every check below runs on the bridge before the library is touched,
in this order:

| Status | Code                         | Reason                                                                                              |
| ------ | ---------------------------- | --------------------------------------------------------------------------------------------------- |
| `403`  | `settings_disabled`          | The bridge runs with `settings_mode: read_only`. Nothing else is checked                            |
| `404`  | `not_found`                  | The key is not one of the seven settings the state route serves                                     |
| `409`  | `mower_setting_read_only`    | `rain_auto_return`, `child_lock` or `bird_view_capture`, whatever the value                          |
| `400`  | `invalid_setting_value`      | No `value`, or neither `true`, `false` nor an integer of at most six digits                          |
| `400`  | `invalid_mower_id`           | Not a 64-character id                                                                               |
| `409`  | `command_in_progress`        | Another write, a command or a setting, owns this mower. One write per mower at a time               |
| `404`  | `unknown_mower`              | Discovery did not return the id                                                                     |
| `503`  | `mower_host_unconfigured`    | No LAN host for the id                                                                              |
| `409`  | library `mower_setting_*`    | The library refused before any frame was written: `mower_setting_invalid` for a wrong type or a value outside the app's bound, `mower_setting_undeclared`, `mower_setting_evidence_missing`, `mower_setting_map_saving` or `mower_setting_already_set` |
| `503`  | library or bridge code       | The session could not be opened or was lost, for example `mower_local_unreachable`, `mower_local_disconnected` or `request_aborted` |

There is no freshness check on the bridge: the library runs its own fresh
status query on the setting session and decides its refusals on it. A `200`
carries the library's outcome:

```json
{
  "contract": 1,
  "id": "<64 hex characters>",
  "setting": "mow_height",
  "result": "confirmed",
  "write": { "dp": "110", "code": "mow_height", "value": 45 },
  "previous": 40,
  "sent_at": "2026-09-25T09:00:00.100Z",
  "stage": "reflected",
  "end": "reflected",
  "before_observed_at": "2026-09-25T09:00:00.050Z",
  "reply": { "observed_at": "2026-09-25T09:00:00.120Z", "return_code_zero": true, "rejected": false },
  "reflection": { "observed_at": "2026-09-25T09:00:00.600Z", "sequence": 71, "value": 45 },
  "other": null,
  "reports": 1
}
```

`result` is `confirmed` when a fresh report carried the written value within
the library's read-back bound of 10 seconds, `failed` when the device rejected
the control frame, and `uncertain` when the bound passed or the report limit
was reached. An uncertain write happened and must never be repeated
automatically. `previous` is the value on the library's fresh query before the
write. A restore is a second deliberate request with that value, which runs
its own fresh query and refusals. `other` is the latest fresh report of
another value, served only as a boolean or a number. Raw data points and the
reports themselves are never served, only their count.

Evidence: the library's
[settings contract](https://github.com/keesmod/eufy-mega-client/blob/main/docs/MOWER_SETTINGS.md)
and its
[settings schema receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_SETTINGS_SCHEMA_2026-09-25.md)
record the data points, the owned E15's declarations and the app's input
checks. On 2026-09-25 the route changed the owned E15's mow height from 40 to
45 mm and back to 40 through Home Assistant, with the mower in the dock and
the owner at the mower. Both writes were `confirmed`, a later fresh query
reported each value and the official app showed each one, see the library's
[settings window receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_SETTINGS_WINDOW_2026-09-25.md).
Volume and the two switches have not been written on the mower yet.

### `GET /v1/mowers/{id}/map`

Read-only, only with [map provisioning](#map-provisioning). The current map
bundle of the mower the provisioning belongs to, in the format the
integration validates for every compatible map source. Two request headers
count: `X-Eufy-Map-Mode`, `idle` (the default when absent) or `stream`, and
`If-None-Match`. The request is answered at once from memory and never waits
for an acquisition.

A `200` carries the bundle with these headers:

| Header                   | Value                                                                                  |
| ------------------------ | -------------------------------------------------------------------------------------- |
| `Content-Type`           | `application/vnd.eufy-robomow-map+zip`                                                 |
| `ETag`                   | `"<snapshot_id>-<captured_at>"`, a new tag for new files or a later capture             |
| `X-Eufy-Map-Captured-At` | The library's receipt time of the snapshot                                             |
| `X-Eufy-Map-Age-Ms`      | Its age against the bridge clock when the answer was built                             |
| `X-Eufy-Map-Stale`       | `true` while the last demand failed and the last good bundle is served, else `false`   |
| `X-Eufy-Map-Error`       | The failed demand's code, only while stale                                              |

When `If-None-Match` matches, the answer is `304` with the same headers and no
body. Before the first snapshot the answer is `503` with `map_unavailable` or
the code of the failed demand. Other refusals: `404 map_unconfigured` without
provisioning or for another mower than the provisioned one, `400
invalid_mower_id`, `400 invalid_map_mode`, `404 unknown_mower`, `409
map_mower_unresolved` and the discovery codes of the state route.

The bundle is a ZIP archive whose members are stored without compression:
`manifest.json` and the three transport files `map.bin.stream`,
`cleanPath.bin.stream` and `navPath.bin.stream` exactly as the library
retained them. The manifest holds `schema_version` 1, `device_id`, the mower
id of the route, `captured_at`, the receipt time in whole Unix seconds,
`snapshot_id`, the SHA-256 over every file name, its length as eight
big-endian bytes and its bytes in that file order, and `files` with every
file's `size` and `sha256`. Identical input gives identical bytes.

Acquisition follows the requests, one demand at a time:

- An idle request starts a demand when none ran in the last five minutes or
  no bundle exists. The demand ends once a snapshot with a `history` or
  `complete` cleaning path was published, as the retained source closed its
  idle stream after one complete snapshot, otherwise after 30 seconds. The
  empty `realtime` placeholder that opens every demand does not end it.
- A `stream` request keeps demands running for a 30-second lease. Each demand
  lasts up to 30 seconds and the next stream request after it starts the
  next one.
- After a demand that published nothing the next one waits a minute, in both
  modes.
- Every demand reads the provisioning file, constructs one
  `PortableMapAcquisition`, checks its retained files every second and
  publishes each newer complete snapshot that passes the gate. Afterwards it
  releases the library's retained bytes and shuts the instance down. An
  unconfirmed local cleanup stops all further demands until a restart, as the
  library refuses further acquisition on that instance.

The gate runs the library's `decodeMowerMapSnapshot`. Every file holds 1 byte
to 5 MiB, the integration's limit, all three files decode without a fault and
the map file carries a realtime map with at least one region and no
degenerate region boundary. A refused snapshot never replaces the bundle.

Failure codes, in `X-Eufy-Map-Error`, in the `503` body and in the state
document's `maps.error`:

| Code                                                                                                                          | Meaning                                                                    |
| ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `map_provisioning_unreadable`                                                                                                 | The file is missing, not a regular file, too large, empty or not a JSON object |
| `map_provisioning_insecure`                                                                                                   | Group or others may write the file, or others may read it                  |
| `mower_map_invalid_provisioning`                                                                                              | The library refused the provisioning, for example because it expired, before any network I/O |
| `mower_map_incomplete`                                                                                                        | The demand ended without a complete snapshot                               |
| `mower_map_negotiation_timeout`, `mower_map_connection_failed`, `mower_map_protocol_error`, `mower_map_stream_ended`, `mower_map_cancel_unconfirmed` | The library's end reason of a demand that published nothing |
| `mower_map_cleanup_unconfirmed`                                                                                               | Local cleanup was not confirmed, no further demand until a restart         |
| `map_undecodable`, `map_boundary_missing`, `map_file_size`                                                                    | The demand's snapshots failed the gate                                     |
| `request_aborted`                                                                                                             | The bridge stopped during the demand                                       |

The bundle is private lawn geometry. It is served only with the bearer token,
never logged and never part of the state document. The bridge keeps the last
good bundle in memory only and writes nothing about the map to disk, so after
a restart the route answers `503` until the first snapshot while the
integration keeps showing its own last good copy.

## Data directory

| File                 | Mode   | Content                                                 |
| -------------------- | ------ | ------------------------------------------------------- |
| `mower-session.json` | `0600` | Opaque library session `{ "version": 1, "data": "…" }` |
| `bridge-id`          | `0600` | Random UUID created once, reported as `bridge_id`      |

The session belongs to the library adapter and is never inspected, logged or
served. Delete the file to force a fresh login on the next start. The map
provisioning file is the operator's and lives wherever the operator keeps it.
No map data is written here.

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
failures and cancellation at shutdown, and the map route's bundle contract,
its entity tag and age headers, the decoder gate, the provisioning file rules,
idle and stream demands, the last good bundle after failed and refused
demands, the retry spacing, unconfirmed cleanup and cancellation at shutdown.
Route tests use the `openLocalSession` and `mapAcquisition` seams with
synthetic sessions and snapshots, while the library's own refusals
(`authentication_required`, `mower_protocol_unavailable`,
`mower_map_invalid_provisioning`) and its map decoder run through the real
package. `test/maps.test.ts` also proves that the bridge builds byte for byte
the bundle in `tests/fixtures/bridge_map_bundle.json`, which the integration's
own validator decodes in `tests/test_bridge_map.py`. No test sends anything
to a mower or a relay. Every lifecycle test asserts that no socket, listener or
referenced timer remains afterwards.

The library is pinned to the exact release tarball and its `sha512` integrity
in `package-lock.json`. A test fails when the pin becomes a branch, tag or
commit reference. Bump the pin by changing the URL in `package.json`, the
constants in `src/version.ts` and running `npm install --ignore-scripts`.
