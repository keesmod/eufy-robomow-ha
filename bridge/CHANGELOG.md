# Changelog

Changes to the dedicated mower bridge in `bridge/` and to its local Home
Assistant app candidate in `ha_app/`, which always carries the bridge's version.
The integration has its own [changelog](../CHANGELOG.md). Every version pins
`@keesmod/eufy-mega-client` to one release tarball, with its sha512 integrity
in the lockfile. No image is published, no app repository is listed and no
version has been released.

Every entry states its evidence. "Software-verified" means CI and synthetic
tests without a mower. Hardware evidence comes from the owned E15, recorded in
dated comments on
[issue #8](https://github.com/keesmod/eufy-robomow-ha/issues/8) unless another
record is named.

Upgrade and rollback follow the [deployment guide](../docs/bridge-deployment.md).
The data directory holds `mower-session.json` and `bridge-id`. The library's
changelog records no change of the persisted session or of the mower ids from
0.13.0 to 0.22.0, so every version below reads the data of another. Keep both
files, because the mower id the integration stores depends on them. A rollback
of the app restores the app backup that the update created, which brings back
the previous version with its options and data.

## 0.10.1 - 2026-09-25

### Library 0.22.0, the mission status

- Pins library 0.22.0, which reads DP 107 as the mower's mission status, as
  the official app does. Every mowing mission, the Box, zone and scheduled
  tasks included, reports `mowing` or `paused`. The recharge mission reports
  `returning`, and a message without a mission reports `idle`.
- Command reflection follows the same reading, so a Box pause or resume is
  confirmed.
- The bridge code is unchanged. The route tests follow the pinned registry.
- Upgrade: update the app with a backup. No option changes. Rollback: return
  to 0.10.0.
- Evidence: deployed on 2026-09-25. In bridge mode a Box task's pause and
  resume were confirmed in 0.56 and 0.58 seconds, and its dock reported
  `returning` and then the map save at the arrival
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5830638474)).
  The settings window ran on it with `settings_mode: write`
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5830926513)).

## 0.10.0 - 2026-09-25

### Settings

- Pins library 0.20.0.
- The state route serves `settings` from the same query: `mow_height`,
  `volume`, `smart_no_go_zones`, `sparse_lawn_optimization`,
  `rain_auto_return`, `child_lock` and `bird_view_capture`. Each is `reported`
  with its value and `writable` flag, or `missing`, or `invalid`.
- New option `settings_mode`, `read_only` by default and independent of
  `operating_mode`. With `write`, `POST /v1/mowers/{id}/settings/{key}?value=…`
  writes the mow height, the volume, smart no-go zones or sparse lawn
  optimization. Rain and child protection and the bird-view capture answer
  `409 mower_setting_read_only`.
- One write runs per mower, shared with commands. The outcome is `confirmed`,
  `failed` or `uncertain` with the previous value, and it is never retried.
- `routes.settings` in the bridge state reports the opt-in. The app passes
  `settings_mode`.
- Upgrade: update the app with a backup. Without `settings_mode` the settings
  stay read only. Rollback: return to 0.9.0.
- Evidence: deployed on 2026-09-25 with the settings read only. The supervised
  write ran on 0.10.1.

## 0.9.0 - 2026-09-24

### Command progress

- Pins library 0.19.0 and passes its `onProgress` callback to every command.
- `GET /v1/mowers/{id}/state` gains `command`. While a command runs it holds
  the class, the acknowledgement time and the latest confirmed activity.
  Otherwise it is `null`.
- Progress never ends, repeats or changes a command, and the typed `status`
  stays what the query reported.
- Upgrade: update the app with a backup. Rollback: return to 0.8.0.
- Evidence: in the third control window of 2026-09-24 the library received
  `returning` 2.6 seconds after a dock, and Home Assistant showed it from the
  next poll
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5815137640)).

## 0.8.0 - 2026-09-24

### Cloud session renewal

- The library reuses a cloud session for at most one hour. A route that needs
  the cloud now renews a lapsed session through one bounded attempt that
  concurrent routes join, at most once a minute after a failure.
- A refused sign-in is never repeated. A renewal happens only before a
  command's write, and commands are never retried or replayed.
- A new session runs discovery again before its LAN session. `auth.state`
  follows the library, and each renewal logs one line.
- Upgrade: update the app with a backup. Rollback: return to 0.7.1, which
  answers `authentication_required` about an hour after its sign-in until a
  restart.
- Evidence: on 2026-09-24 the next poll renewed a lapsed session, and the
  mower entity stayed available
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5811629836)).

## 0.7.1 - 2026-09-23

### The startup log names the stop route

- The `control` startup log names every routed command class, `stop`
  included.
- Upgrade: none. Rollback: return to 0.7.0.
- Evidence: the first app installed on the owner's Home Assistant on
  2026-09-24, in `observe_only`
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5809904636)).
  The first control window ran on it
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5810889297)).

## 0.7.0 - 2026-09-23

### Read-only map route

- Pins library 0.18.0.
- With an operator-supplied map provisioning file, `GET /v1/mowers/{id}/map`
  serves the map bundle the integration validates. A bundle is published only
  after the library's decoder accepted the snapshot. It comes with an `ETag`
  and an explicit age, and the last good bundle is served after a failed
  acquisition. `routes.maps` reports the route.
- Acquisition is request driven, one demand at a time. The provisioning file
  is read for every demand and never logged, served or stored.
- The app gains `map_provisioning_file` and `map_mower_id` and maps its own
  configuration folder read-only at `/config`.
- Upgrade: no new required option. Rollback: switch the integration's map
  source to `external` first, then return to 0.6.0.
- Evidence: software-verified. The app's configuration folder mapping ran on a
  Supervisor on 2026-09-24. No live map has been acquired through the bridge.

## 0.6.0 - 2026-09-20

### Stop route

- Pins library 0.17.0.
- `POST /v1/mowers/{id}/commands/stop` sits behind the same `control` opt-in.
  On the owned E15 a stop ends the task and the mower returns to the dock by
  itself. The command document gains `payload`, the map-saving payload that
  reflects a stop at the dock arrival.
- `return` keeps answering `409 command_unsupported`, because the firmware
  ignores DP 3 from paused and from the stopped task.
- Upgrade: none. Rollback: return to 0.5.0.
- Evidence: the library's
  [stop and return receipt](https://github.com/keesmod/eufy-mega-client/blob/19d47a7144e505702e1ef98dfc7d84dfeb956cd6/docs/research/E15_STOP_RETURN_WINDOW_2026-09-20.md)
  of 2026-09-20. Stops through the bridge were confirmed in the control
  windows of 2026-09-24.

## 0.5.0 - 2026-09-19

### Opt-in command routes

- Pins library 0.16.0.
- `operating_mode` accepts `control`, which requires `control_stop_route`, the
  operator's own stop route, never logged or served. `control_max_state_age_ms`
  and `control_read_back_ms` are bounded options.
- `POST /v1/mowers/{id}/commands/{start|pause|resume}`. In `observe_only` every
  command route answers `403 control_disabled`. In `control` the bridge checks
  the class, the id, exclusive ownership, discovery, the host and the state's
  age before it opens one bounded LAN session for one command.
- The outcome is `confirmed`, `failed` or `uncertain`. Nothing is retried,
  replayed or reconnected.
- The state document reports `routes.control` and a `control` block.
- Upgrade: `observe_only` stays the default. Rollback: return to 0.4.0.
- Evidence: software-verified at release. The bridge first ran in `control`
  mode against the mower on 2026-09-24.

## 0.4.0 - 2026-09-19

### Confirmed E15 activity

- Pins library 0.15.0. The state route's `status` reports `mowing`, `paused` or
  `returning` with the query's observation time, `missing` without DP 107 and
  `invalid` for the withheld payloads.
- Upgrade: none. Rollback: return to 0.3.0.
- Evidence: the library's
  [robot status receipt](https://github.com/keesmod/eufy-mega-client/blob/ee1ac36bead945445aee63fe049b295e81b9eafc/docs/research/E15_ROBOT_STATUS_REPRODUCTION_2026-09-19.md).
  A query reply of the owned E15 carries no DP 107, so a poll reports
  `missing`.

## 0.3.0 - 2026-09-19

### Container image and app candidate

- A two-stage `bridge/Dockerfile` from the Node 24 slim image pinned by its
  digest. It runs `npm ci --ignore-scripts` from the committed lockfile, keeps
  only production dependencies, runs as the unprivileged `node` user, and has
  the `/data` volume, port 8090 and a `HEALTHCHECK` on the authenticated state
  route.
- `ha_app/`, the local Home Assistant app candidate on the same base image.
  `bootstrap.mjs` maps the Supervisor options, creates `/data/eufy-mower` with
  owner-only permissions and drops root. The app starts with `boot: manual`.
- `scripts/prepare_ha_app.py` stages the bridge sources into the app and fails
  on a version or base image mismatch. CI builds both images and runs the
  container smoke test on each.
- Upgrade: none. Rollback: earlier versions ran from source only.
- Evidence: software-verified. The app was first installed on a Supervisor on
  2026-09-24, as 0.7.1.

## 0.2.0 - 2026-09-19

### Discovery and state routes

- `GET /v1/mowers`, contract 1, lists the discovered E15 mowers with the
  library's opaque ids, served from a cache.
- `GET /v1/mowers/{id}/state`, contract 1, runs one bounded read-only LAN
  session with one typed query. It serves `status`, `battery`, `progress` and
  `network` with their states, plus `observed_at`, `age_ms`, `stale` and
  `error`. Raw data points, keys and hosts are never served.
- Hosts come from `EUFY_MOWER_HOST` or `EUFY_MOWER_HOSTS`.
- Upgrade: set `EUFY_MOWER_HOST`, or `EUFY_MOWER_HOSTS` for more than one
  mower, before the state route can reach a mower. Rollback: return to 0.1.0.
- Evidence: software-verified.

## 0.1.0 - 2026-09-19

### Lifecycle foundation

- Pins library 0.13.0 as the exact release tarball with its sha512 integrity.
  One library client runs with only the mower module.
- A validated configuration, a private data directory with the session file
  and the bridge identity, and a bearer-protected private HTTP server with
  `GET /v1/state`. It makes one explicit authentication attempt without retry
  and shuts down within a bound on `SIGTERM`.
- `operating_mode` accepts only `observe_only`.
- Upgrade: none, this is the first version. Rollback: none.
- Evidence: software-verified.
