# Mower bridge deployment

How to run the dedicated mower bridge from `bridge/` as a container or as a
local Home Assistant app, and how to upgrade, restart, back up and roll it
back. Everything here is local: no image is published and no app repository is
listed. Version 0.10.1 serves state routes with the typed settings and, only
behind the explicit `operating_mode: control` opt-in with a stop route, the
start, pause, resume and stop routes. The separate `settings_mode: write`
opt-in enables the settings route for mow height, volume, smart no-go zones
and sparse lawn optimization. It pins library 0.22.0, which reads DP 107 as
the mower's mission status, confirms the start, pause, resume and stop commands,
and reads and writes the settings behind its own opt-in.
On the owned E15 a stop ends the task and returns the mower to the dock. With
an operator-supplied map provisioning file it also serves the read-only map
route, see [Map provisioning](#map-provisioning-optional).

## What is separate from a camera installation

| Item | Mower bridge | Camera bridge (ha-eufy-cam) |
| --- | --- | --- |
| Port | `8090` | `8080` (Docker) or `8063` (app) |
| Variables | `EUFY_MOWER_*` | `EUFY_BRIDGE_*`, `EUFY_*` |
| Data | `/data/eufy-mower` | `/data` or `/data/eufy` |
| Token | its own, at least 32 characters | its own |
| Account | the Eufy Home account of the mower | the Eufy Security account |
| Library | `@keesmod/eufy-mega-client`, mower module only | the same package, security module only |

Never share a token, a data directory or a volume between the two. Either
installation runs while the other is absent or stopped.

## Reproducible image

`bridge/Dockerfile` builds in two stages from the official Node 24 slim image,
pinned by its multi-architecture digest. Dependencies come from the committed
`package-lock.json` with `npm ci --ignore-scripts`, which pins the library to
its exact release tarball and `sha512` integrity. The runtime stage keeps
production dependencies only, runs as the unprivileged `node` user, declares
the `/data` volume and port `8090`, and carries a `HEALTHCHECK` that calls the
authenticated state route on loopback every 30 seconds.

CI proves the build on a clean Ubuntu runner: both images build, refuse to
start without configuration (exit 78), start with synthetic credentials and no
network, become healthy, answer the state route with the token and refuse
without it, stop with exit 0 within the shutdown deadline, and never log the
token or the password. `bridge/scripts/container-smoke.sh` runs the same proof
locally.

Refreshing the base digest or the library pin is a deliberate change of the
Dockerfiles, `bridge/package.json`, `bridge/src/version.ts` and the staged copy
in `ha_app/` (run `python3 scripts/prepare_ha_app.py`).

## Install with Docker

```bash
docker build -t eufy-mower-bridge:0.10.1 ./bridge
```

```bash
docker run -d --name eufy-mower-bridge --restart unless-stopped \
  -p 127.0.0.1:8090:8090 \
  -v eufy-mower-data:/data \
  -e EUFY_MOWER_BRIDGE_TOKEN=<random secret of at least 32 characters> \
  -e EUFY_MOWER_EMAIL=<eufy account email> \
  -e EUFY_MOWER_PASSWORD=<eufy account password> \
  -e EUFY_MOWER_COUNTRY=NL \
  -e EUFY_MOWER_HOST=<mower LAN address> \
  eufy-mower-bridge:0.10.1
```

Bind the published port to an address that only Home Assistant can reach, or
put both on the same private network. The container listens on `0.0.0.0`
inside its own network namespace and every request needs the bearer token.
`EUFY_MOWER_HOSTS` with `id=host` pairs replaces `EUFY_MOWER_HOST` when more
than one mower is discovered. The complete variable list is in
[`bridge/README.md`](../bridge/README.md).

Compose users map the same variables into `environment:` and the volume into
`volumes:`. Prefer an env file with mode `0600` over inline secrets.

Verify:

```bash
docker inspect --format '{{.State.Health.Status}}' eufy-mower-bridge
```

```bash
docker exec eufy-mower-bridge node dist/healthcheck.js
```

The health check prints one JSON line with the HTTP status, lifecycle, bridge
version, authentication state and last error code, and exits 0 only while the
bridge reports `running`. The log shows one authentication attempt and the
number of discovered mowers, and later one line for each renewal of the cloud
session. Restart the container after a configuration change.

Then select the `bridge` backend in the integration with the URL
`http://<host>:8090` and the same token.

## Install as a local Home Assistant app

`ha_app/` is an app candidate for a local installation. It stages the bridge
sources, uses the same pinned base image, maps the Supervisor options to the
bridge's variables in `bootstrap.mjs`, creates `/data/eufy-mower` with owner-only
permissions and drops root before the bridge starts.

1. Run `python3 scripts/prepare_ha_app.py` in a checkout, then copy `ha_app/` to
   `/addons/eufy_mower_bridge` on the Home Assistant OS machine.
2. **Settings → Apps → Install app**, refresh, install **Eufy Mower Bridge**
   from the local section. The first build takes several minutes.
3. Configure `token`, `email`, `password`, `country` and `host` (or `hosts`),
   save, start, read **Logs**.
4. Use `http://<home-assistant-ip>:8090` as the bridge URL in the integration.
   The app publishes port 8090 on the host; the Supervisor network also
   reaches it as `local-eufy-mower-bridge`.

The app data lives in the app's `/data`, which Home Assistant backups include.

## Map provisioning (optional)

For fresh account-based provisioning, set app option
`map_provisioning_mode: cloud`, or environment variable
`EUFY_MOWER_MAP_PROVISIONING_MODE=cloud`. Leave `map_provisioning_file` unset.
Bridge 0.12.0 on library 0.24.0 then requests private provisioning for each
acquisition and uses its existing session renewal when needed. No file needs
manual renewal. Back up the private data directory and options before enabling
this experimental route. Native map acceptance and controlled source migration
remain required before retiring the existing external map source.

The default file route remains available. The operator supplies a private file
and keeps it fresh, see
[Map provisioning](../bridge/README.md#map-provisioning) in the bridge README
for its content, expiry and rules. The bridge reads it for every acquisition
demand and never stores or logs its contents. Without cloud mode or a file the route
answers `404` and `routes.maps` stays `false`.

Docker: keep the file in a private directory on the host, owned by uid 1000
(the image's `node` user) with mode `0600`, and mount it read-only:

```bash
docker run -d --name eufy-mower-bridge --restart unless-stopped \
  -p 127.0.0.1:8090:8090 \
  -v eufy-mower-data:/data \
  -v /private/eufy-mower-map:/run/eufy-mower-map:ro \
  -e EUFY_MOWER_MAP_PROVISIONING_FILE=/run/eufy-mower-map/map-provisioning.json \
  -e EUFY_MOWER_BRIDGE_TOKEN=<random secret of at least 32 characters> \
  -e EUFY_MOWER_EMAIL=<eufy account email> \
  -e EUFY_MOWER_PASSWORD=<eufy account password> \
  -e EUFY_MOWER_COUNTRY=NL \
  -e EUFY_MOWER_HOST=<mower LAN address> \
  eufy-mower-bridge:0.10.1
```

Mount the directory rather than the file, so a file replaced by renaming
reaches the container. Add `EUFY_MOWER_MAP_MOWER_ID` when the account has more
than one mower.

App: the candidate maps its own configuration folder read-only at `/config`.
Put the file in `/addon_configs/local_eufy_mower_bridge/` on the Home
Assistant OS machine, give it to uid 1000 with mode `0600` (for example
`chown 1000:1000` and `chmod 600` from the SSH app), and set
`map_provisioning_file` to `/config/map-provisioning.json`. This mapping
follows the documented Supervisor syntax and has not been exercised on a
Supervisor yet.

After native acceptance, choose the map source `bridge` in the integration's options. The
integration refuses it while the bridge does not report `routes.maps`. The
map source URL stays stored, switching the map source back to `external` is
the manual recovery path.

## Upgrade

1. Read the release notes and the bridge version in `bridge/package.json`.
2. Back up first (below).
3. Docker: build the new tag from the new checkout, stop and remove the old
   container, start the new one with the same variables and volume. App: replace
   `/addons/eufy_mower_bridge` with the new `ha_app/`, run `ha store reload` and
   update the app with a backup, `ha apps update local_eufy_mower_bridge
   --backup` or **Update** on the app page. **Rebuild** only rebuilds the
   installed version, and the Supervisor refuses it once the folder carries
   another version.
4. Verify with the health check and the integration's state. The `version`
   field in `GET /v1/state` must show the new version.

The session file is owned by the library. A new library release may not read
an older session, in which case the bridge signs in again once at start. That
is expected and needs no action.

## Restart

Docker: `docker restart eufy-mower-bridge`. App: **Restart** on the app page.
A restart stops the bridge with `SIGTERM`. It cancels an in-flight
authentication or query, closes the private API, closes the library client and
flushes files, all within 15 seconds, and exits with 1 twenty seconds after the
signal at the latest. After the start one authentication attempt and one
discovery run. The library reuses a cloud session for at most one hour, so a
route that needs the cloud renews a lapsed session through one bounded attempt,
at most once a minute after a failure. A refused sign-in, for example a wrong
password, a captcha or a lock, is never repeated: resolve it and restart the
bridge. No command is ever retried or replayed.

## Backup

The data directory holds two private files: `mower-session.json`, the opaque
library session, and `bridge-id`, the stable identity reported to the
integration. Both are `0600`.

- Docker: `docker run --rm -v eufy-mower-data:/data -v "$PWD:/backup" \`
  `node:24-bookworm-slim tar -C /data -czf /backup/eufy-mower-data.tgz .`
  Keep the archive private, it contains the session.
- App: a Home Assistant backup of the app includes `/data`.
- Keep the token and the configuration with the backup. The integration's
  options hold the same token and the mower id.

Nothing else needs a backup. Discovery results, telemetry and map bundles
are not stored. The map provisioning file is the operator's material and
expires quickly, so it is not part of the bridge backup.

## Rollback

1. Docker: stop the current container and start the previous image tag with
   the same variables and volume.
2. App: put the previous `ha_app/` in `/addons/eufy_mower_bridge`, run
   `ha store reload` and update the app with a backup. The Supervisor installs
   an older version through an update as well, keeps the data directory and
   the options, and starts the app again when it was running. Restoring the
   app backup that the upgrade made is the alternative. It brings back the
   previous version with its options and the data of that moment. The
   rehearsal of 2026-09-25 confirmed the update path on Supervisor 2026.09.2.
3. When the previous version cannot read the newer session file, delete
   `mower-session.json` from the data directory and restart. The bridge signs
   in once. Keep `bridge-id`, the integration's mower id and unique IDs depend
   on it and on the library's identity salt inside the session, so prefer
   restoring the backup of the data directory over deleting the session.
4. Verify with the health check. The integration's entities recover on the
   next poll, no command is replayed.

Before rolling back to a version without the map route (0.6.0 and older),
switch the integration's map source back to `external`. Otherwise the map
entity keeps its last good map and reports the refused request.

## Where the limits are

Bridge mode in the integration reads state and, since integration 0.9.0,
routes start, pause and resume through the bridge's opt-in command routes.
Since integration 0.10.0 dock goes through the bridge's stop route as well,
and since integration 0.11.0 the map entity can read the bridge's map route.
Activity reports mowing, paused, returning and idle from the E15 mission
status read by library 0.22.0, see [DP 107 activity](protocol-provenance.md#dp-107-activity).
Every mowing mission counts, the Box, zone and scheduled tasks included. Idle
reads as docked and ends a bridge-mode session. Docked, charging and error have
no confirmed payload and are never inferred, and mowing progress stays
unconfirmed. Commands need two opt-ins: the integration's operating mode
`control` and the bridge's `operating_mode: control` with its required
`control_stop_route`. The integration reads `routes.control` from the bridge
state on every poll and exposes start, pause and dock only while it is true.
Each command is sent once, the bridge's confirmed, failed or uncertain answer
is the result, and an uncertain command is never repeated automatically. Dock
is the bridge's `stop` class: on the owned E15 a stop over DP 1 false ends the
task and the mower returns to the dock by itself, and a confirmed dock carries
the map-saving payload the library received at dock arrival. The library's
`return` over DP 3 is ignored by the owned firmware from paused and from the
stopped task, so the bridge has no return route and cannot stop the mower in
place. Leave the bridge at `observe_only` unless a supervised test with the
app at hand is planned, the bridge has not yet run in control mode against
the mower, see keesmod/eufy-robomow-ha#8. Since integration 0.14.0 the cut
height, volume and the five local switches read the bridge's state document in
bridge mode. Writes need the integration's `control` mode and the bridge's
separate `settings_mode: write`, and only cut height, volume, smart no-go
suggestions and mow yellow grass are written. Rain stop, child protection and
the real lawn map stay read only there, see
[Settings through the bridge](protocol-provenance.md#settings-through-the-bridge).
Since integration 0.15.0 and bridge 0.11.0 the DP 155 work parameters (edge
distance, pad direction, path distance, travel and blade speed) read the
bridge's `work_parameters` in bridge mode, and the travel and blade speeds are
written through the same settings route and opt-ins, see
[Work parameters through the bridge](protocol-provenance.md#work-parameters-through-the-bridge).
The map
route is read-only, needs the operator's provisioning and has not acquired a
live map through the bridge yet, that map acceptance is part of
keesmod/eufy-robomow-ha#8 as well. Physical control keeps its explicit opt-in
and supervised validation.
