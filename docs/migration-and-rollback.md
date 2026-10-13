# Migration and rollback

How the mower moves between the integration's local backend and the mower
bridge, how the integration and the bridge app are upgraded and rolled back,
and how an external map source is stopped. The procedures keep one control
owner and one map owner at any time. They keep the config entry, the entity
ids, the unique ids and the session history, and they never retry or replay a
command.

These procedures were rehearsed on the maintainer's installation on 2026-09-25,
without mower movement. The rehearsal covered:

- A switch refused while the bridge was unreachable.
- A bridge lost after the switch.
- A restart in bridge mode without a running bridge.
- A rollback and an upgrade of the integration and of the app.

Every entity id, unique id, session and dashboard reference was kept, and no
command was sent. See the [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5832886365) in
[issue #8](https://github.com/keesmod/eufy-robomow-ha/issues/8).

## One control owner, one map owner

**Control.** Exactly one path writes to the mower from Home Assistant.

- With the `local` backend the integration writes over its own LAN connection
  in its `control` mode. Keep the bridge app in `observe_only` with
  `settings_mode` unset or `read_only`. Nothing queries the bridge then, so it
  opens no LAN session to the mower.
- With the `bridge` backend the integration opens no LAN connection and no
  cloud client of its own. The bridge writes, and only while both the
  integration and the bridge run in `control`.
- The eufy app stays an independent control path in both cases.

**Map.** Exactly one source feeds the map entity.

- `external` is a compatible HTTPS map source, which this project does not
  include. It is the default and the manual recovery path.
- `bridge` is the bridge's read-only map route. It needs the `bridge` backend
  and a bridge that reports `routes.maps`.
- Each source keeps its own latest-good file, `latest.mapbundle` and
  `bridge.mapbundle`. The external URL stays stored while `bridge` is
  selected.

The entities keep their unique ids on both backends. Since integration 0.15.0
with bridge 0.11.0 both backends create the same entities, the DP 155 work
parameters included: edge distance, pad direction, path distance, travel speed
and blade speed. With an older bridge those five show as unavailable in bridge
mode, keep their registry entries and return with the local backend.
`tests/test_migration.py` pins this.

## Switching the backend

Before switching, check that the bridge's health check reports `running` and
that no supervised window runs on the mower.

1. Open **Settings → Devices & services → Eufy Robomow → Configure**. Choose
   the backend `bridge` with the bridge URL and token, and keep the map source
   `external` unless the bridge serves maps.
2. Saving asks the bridge's state route with the token and resolves the mower.
   When the bridge cannot be reached, the token is wrong or the mower is
   unknown, the form shows the error and nothing is saved. The local backend
   keeps running. This is a failed cutover before the switch, and nothing
   needs restoring.
3. After saving, the entry reloads on the bridge backend. Within one or two
   polls, about 10 to 20 seconds, the mower entity is available again, with
   `backend: bridge` in its attributes.
4. When the entities stay unavailable, for example because the bridge stopped
   after the switch, open **Configure** again and choose backend `local` and
   map source `external` together. The native source cannot stay selected with
   the local backend. The entry reloads locally and its first poll brings the
   entities back.
   This is the whole recovery.

A backend switch needs no Home Assistant restart. A reload starts a new
coordinator without a pending command, so nothing is retried or replayed
(`tests/test_bridge_backend.py`). The fallback to `local` is a deliberate
step and not automatic, so that the mower never gets two LAN owners from Home
Assistant.

The app candidate starts with `boot: manual`, so it does not start by itself
after a host restart. For lasting use of the bridge backend, enable
**Start on boot** on the app's page. Without it, a restart of the host leaves
the entry retrying its setup until the app is started. **Configure** also
works on a retrying entry, so switching to `local` recovers the entities
without the bridge. In the rehearsal that took 2 seconds.

## Switching the map source

- From `external` to `bridge`: the options flow refuses `bridge` without the
  bridge backend or without `routes.maps`, and nothing changes.
- Back to `external`: one option change. The external URL and its latest-good
  map are still there.
- Before a rollback of the bridge to a version older than 0.7.0, switch the map
  source to `external`.

## Upgrade and rollback of the integration

1. Back up the installed `custom_components/eufy_robomow` folder to a dated
   archive outside `custom_components`. Also back up
   `.storage/core.config_entries`, `core.entity_registry`,
   `core.device_registry` and `eufy_robomow.sessions.<entry id>`. Keep the
   backup private, because the config entry holds the account password and the
   local key.
2. Replace the folder with the version's `eufy_robomow` folder, run the
   configuration check (`ha core check`) and restart Home Assistant
   (`ha core restart`).
3. Check that the entry loaded, that the entity registry is unchanged and that
   the session history still lists its sessions.

A rollback is the same with the previous folder.

- The options have kept their keys since 0.11.0. The session store has used
  storage version 1 since 0.7.0 and keeps session fields it does not know. Home
  Assistant refuses a store with a newer major version, so a later release
  that changes the session layout must use a minor version or a new key
  (`tests/test_migration.py`).
- An older version ignores an option it does not know, but saving the options
  there drops it.
- Entities that only a newer version creates become unavailable after a
  rollback and keep their registry entries. The next upgrade brings them back
  under the same entity ids.
- A rollback across 0.8.1 changes the Signal Strength unit back to dBm and
  raises the statistics repair again.

## Upgrade and rollback of the bridge app

- Upgrade: copy the version's app folder to `/addons/eufy_mower_bridge`, run
  `ha store reload` and then `ha apps update local_eufy_mower_bridge --backup`.
  The Supervisor builds the image on the host and backs up the previous
  version first. Check that `GET /v1/state` reports the new version.
- Rollback: copy the previous version's app folder to
  `/addons/eufy_mower_bridge` and update the same way. The Supervisor installs
  an older version through an update as well, and keeps the data directory and
  the options. **Rebuild** is no rollback, because the Supervisor refuses it
  once the folder carries another version. Restoring the app backup that the
  upgrade made is the alternative. It brings back the previous version with its
  options and the data of that moment. The rehearsal went from 0.10.1 to 0.10.0
  and back this way on Supervisor 2026.09.2. Each update took about 20 seconds
  with the build cache, and the bridge was unreachable for at most about 4
  seconds.
- Keep `mower-session.json` and `bridge-id` in the data directory. The mower id
  the integration stores depends on them. The library's changelog records no
  change of either from library 0.13.0 to the pinned 0.27.0, except that
  library 0.24.0, pinned since bridge 0.12.0, adds private MQTT credentials to
  the session file without changing the mower ids.
- Docker: start the previous image tag with the same volume, see the
  [deployment guide](bridge-deployment.md#rollback).

## Stopping an external map source

Nothing in this repository stops or removes an external map source
automatically. Keep one map owner: stop an external source's acquisition
before you select the `bridge` map source. A source left running next to the
bridge is not a passive standby, because it can start acquisitions on its own
schedule. Keep the external URL, certificate pin and `latest.mapbundle` for
recovery.

The maintainer's former external map source was an Android map helper running
in an emulator. It was retired on 2026-09-25.
See the [retirement receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5839090826).
This project does not include such a helper.

The public external-source API and the `external` option remain supported.
