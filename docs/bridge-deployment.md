# Mower bridge deployment

How to run the dedicated mower bridge from `bridge/` as a container or as a
local Home Assistant app, and how to upgrade, restart, back up and roll it
back. Everything here is local: no image is published and no app repository is
listed. Version 0.5.0 serves state routes and, only behind the explicit
`operating_mode: control` opt-in with a stop route, the start, pause and resume
routes. It pins library 0.16.0, which confirms the E15 activities mowing,
paused and returning and the start, pause and resume commands.

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
docker build -t eufy-mower-bridge:0.5.0 ./bridge
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
  eufy-mower-bridge:0.5.0
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
number of discovered mowers. Nothing is retried, restart the container after a
configuration change.

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

## Upgrade

1. Read the release notes and the bridge version in `bridge/package.json`.
2. Back up first (below).
3. Docker: build the new tag from the new checkout, stop and remove the old
   container, start the new one with the same variables and volume. App: replace
   `/addons/eufy_mower_bridge` with the new `ha_app/`, then **Rebuild** in the
   app page and start.
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
discovery run. There is no automatic retry, so a bridge that failed to sign in
stays disconnected until the next restart.

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

Nothing else needs a backup. Discovery results and telemetry are not stored.

## Rollback

1. Stop the current bridge.
2. Docker: start the previous image tag with the same variables and volume.
   App: restore the previous `ha_app/`, rebuild, start.
3. When the previous version cannot read the newer session file, delete
   `mower-session.json` from the data directory and restart. The bridge signs
   in once. Keep `bridge-id`, the integration's mower id and unique IDs depend
   on it and on the library's identity salt inside the session, so prefer
   restoring the backup of the data directory over deleting the session.
4. Verify with the health check. The integration's entities recover on the
   next poll, no command is replayed.

## Where the limits are

Bridge mode in the integration is state only. Activity reports mowing, paused
and returning from the E15 payloads confirmed in library 0.15.0, see
[DP 107 activity](protocol-provenance.md#dp-107-activity). Docked, charging,
idle and error have no confirmed payload and are never inferred, and mowing
progress stays unconfirmed. Bridge 0.5.0 adds opt-in start, pause and resume
routes behind `operating_mode: control` and a required `control_stop_route`,
which the integration does not use yet. Leave the mode at `observe_only`
unless a supervised test with the app at hand is planned. `return`, settings
and map routes follow in later steps. Physical control keeps its explicit
opt-in and supervised validation.
