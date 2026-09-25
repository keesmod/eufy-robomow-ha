# Release candidates

A release candidate is the integration and the mower bridge at one commit,
built reproducibly and checked against the hardware evidence of
[issue #8](https://github.com/keesmod/eufy-robomow-ha/issues/8). Nothing here
is published: there is no GitHub release, tag, HACS entry, image registry or
app repository. The inherited upstream history declares no license, so
publication waits until the licensing boundary is resolved and the owner
explicitly authorises it.

The versions and their changes are in the [integration changelog](../CHANGELOG.md)
and the [bridge changelog](../bridge/CHANGELOG.md). Migration between the
backends and rollback are described in
[Migration and rollback](migration-and-rollback.md).

## Current candidate

| Component | Version | Pinned inputs |
| --- | --- | --- |
| Integration `eufy_robomow` | 0.15.1 | `requests` 2.34.2, `tinytuya` 1.20.0, dashboard card 0.7.3 |
| Mower bridge, container image | 0.11.0 | `@keesmod/eufy-mega-client` 0.23.0 by release tarball and sha512 integrity, `node:24-bookworm-slim` by digest |
| Mower bridge, Home Assistant app `eufy_mower_bridge` | 0.11.0 | the same inputs, built by the Supervisor on the host |

The checksums of every candidate that was built are recorded in #8. The
candidates of 0.15.0 and 0.15.1 with bridge 0.11.0, built from `64ce862` and
`0ed3691`, matched the folders deployed on the owner's installation file for
file. CI runs the
tests on Home Assistant 2026.7.2, and the rehearsal ran on Home Assistant
2026.9.3 with Supervisor 2026.09.2. The hardware evidence below comes from the
owner's Home Assistant OS installation with the owned E15 (T2880) on firmware
6.9.28, as read on 2026-09-20. The later windows did not read the firmware
again.

## Build and verify

```bash
python3 scripts/build_candidates.py --ref <commit> --out <empty directory outside the repository>
```

The candidate directory then holds:

- `eufy_robomow-<version>.tar`, the integration folder, extracted into
  `/config/custom_components`.
- `eufy-mower-bridge-app-<version>.tar`, the app candidate, extracted into
  `/addons` as `eufy_mower_bridge`.
- `eufy-mower-bridge-<version>.src.tar`, the bridge sources and the build
  context of the container image.
- A `.files.sha256` list for each archive, `candidate.json` with the commit,
  the versions and the pinned inputs, and `SHA256SUMS`.

The archives are built from Git objects with sorted paths, owner 0, normalized
modes and the time of the component's last change, so the same commit gives
byte-identical files on every machine. An unchanged component gives the same
archive from a later commit of a full clone. The build refuses a commit whose
app copy, versions, base images or library pin disagree.
`tests/test_build_candidates.py` proves the reproduction and the content.

To verify a candidate, run `sha256sum -c SHA256SUMS` in its directory. To
compare an installed folder with a candidate, run
`sha256sum -c <path>/eufy_robomow-<version>.tar.files.sha256` in
`/config/custom_components`, or the app's list in `/addons`. Home Assistant
adds `__pycache__` folders, which the lists do not cover.

The container image is built from the source archive:

```bash
tar -xf eufy-mower-bridge-<version>.src.tar
docker build -t eufy-mower-bridge:<version> eufy-mower-bridge
```

The base image is pinned by digest and every dependency by the lockfile, so
the image's application files follow from the archive. Image ids are not
compared, because they carry build timestamps. CI builds both images at every
commit and runs the container smoke test on each.

## Confirmed on hardware

Each item names the version and the date it was confirmed. The receipts are
the dated comments in #8, and the 2026-09-06 test is in
[validation 2026-09-06](validation-2026-09-06.md).

Integration with the local backend:

- Battery, network and the signal percentage the mower declares (0.8.1).
- Start, pause, resume and return through the standard Home Assistant
  services, each confirmed by fresh local telemetry (0.7.0, 2026-09-06).
- `docked` while the mower rests in the dock and while it saves the map at the
  arrival (0.12.1 and 0.13.1, 2026-09-24).
- `returning` during the drive home after a stop or at the end of a task
  (0.13.2, 2026-09-25).
- A start confirmed after a map save and while resting in the dock, and a pause
  refused during the drive home (0.13.3 and 0.13.4, 2026-09-25).
- Hibernation read as idle, with `robot_power_mode` (0.14.1, 2026-09-25).

Integration with the bridge backend:

- Battery, network and signal equal to the local backend (0.11.0 with bridge
  0.7.1, 2026-09-24).
- Start, pause, resume through start while paused, and dock, each confirmed by
  the bridge's fresh report, the owner and the eufy app (0.11.0 to 0.13.0 with
  bridge 0.7.1 to 0.9.0, 2026-09-24).
- The drive home after a dock from the running command's progress (0.13.0 with
  bridge 0.9.0, 2026-09-24).
- A Box task's pause, resume and dock with the mission status reading (0.14.2
  with bridge 0.10.1 on library 0.22.0, 2026-09-25).
- The mow height changed from 40 to 45 mm and back, each value read back
  (0.14.2 with bridge 0.10.1 and `settings_mode: write`, 2026-09-25).
- Volume, Smart No-Go Suggestions and Mow Yellow Grass changed and restored,
  and the mow height set to both bounds, each value read back (0.14.2 with
  bridge 0.10.1, 2026-09-25).
- Travel Speed and Blade Speed changed and restored, each read back from a
  fresh report, with the five DP 155 entities equal to the local backend's
  (0.15.0 with bridge 0.11.0 on library 0.23.0, 2026-09-25).
- The bridge renewed its lapsed cloud session by itself (bridge 0.8.0,
  2026-09-24).
- No command was repeated after an uncertain answer, a bridge restart or a
  backend switch (2026-09-24).

Deployment:

- The app candidate built and ran on a Supervisor (amd64), with its own
  configuration folder mounted read only, and was updated with a Supervisor
  backup from 0.7.1 to 0.11.0 (2026-09-24 and 2026-09-25).
- The integration was upgraded from 0.7.0 to 0.15.0 by replacing its folder.
  The config entry and its entities were kept (2026-09-24 and 2026-09-25).
- The migration and rollback rehearsal, see the [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5832886365). It
  covered a switch refused while the bridge was unreachable, a bridge lost
  after the switch, and a restart in bridge mode without a running bridge. It
  also covered the integration from 0.14.2 to 0.14.1 and back, and the app from
  0.10.1 to 0.10.0 and back. Every entity id, unique id, session and dashboard
  reference was kept, and no command was sent (2026-09-25).

## Experimental

These parts are software-verified or inherited. They have not been confirmed
on hardware in this programme.

- The bridge's read-only map route and the `bridge` map source. No live map has
  been acquired through the bridge, and the route needs map provisioning that
  the operator supplies.
- The `external` map source. It has been in daily use on the owner's
  installation since 0.5.0, but native map acceptance has not passed.
- The no-go zones on the map since 0.15.1, from map-record field 12. The
  owner's one zone was compared with the app before the change, and the
  drawing is software-verified with synthetic geometry.
- The cloud settings of the local backend: edge distance, pad direction, path
  distance, travel speed and blade speed, inherited from upstream.
- Edge distance, pad direction and path distance in bridge mode, read only
  there since 0.15.0.
- The standalone container image outside CI.
- The opt-in planning package. Automatic mowing stays off.
- Any model other than the E15. E18 support is not claimed.

## Outstanding obligations

- Native map acceptance, item 2 of #8.
- The switch of the map source to `bridge` and back on the owner's
  installation, which needs map provisioning. The backend and version
  migration passed its rehearsal on 2026-09-25.
- The retirement plan of the Android map helper, which starts only after both.
- A fresh read of the firmware for the candidate's record.
- The licensing boundary and an explicit authorisation before any publication.

## Compatibility

The bridge's state document has carried `protocol: 1` since 0.1.0, and its
mower documents `contract: 1` since 0.2.0. Every later field is additive. An
older bridge gives the integration fewer features but never fails a poll for a
missing optional field: the `command` block is optional since integration
0.13.0 and the settings since 0.14.0.

| Integration feature | Needs bridge |
| --- | --- |
| State: battery, network, signal | 0.2.0 |
| Activity from reports | 0.4.0 |
| Start, pause and resume | 0.5.0 in `control` |
| Dock | 0.6.0 in `control` |
| Map source `bridge` | 0.7.0 with map provisioning |
| Unattended use over an hour | 0.8.0, cloud session renewal |
| The drive home after a dock | 0.9.0 |
| Setting entities | 0.10.0, writes with `settings_mode: write` |
| Box, zone and scheduled tasks | 0.10.1 on library 0.22.0 |
| Work parameters: travel and blade speed written, the other three read | 0.11.0 on library 0.23.0 |

Combinations run on the owner's installation:

| Integration | Bridge (library) | Record |
| --- | --- | --- |
| 0.15.1 | 0.11.0 (0.23.0) | the current candidate, matched file for file after its deployment on 2026-09-25 |
| 0.15.0 | 0.11.0 (0.23.0) | matched file for file after its deployment, speed window of 2026-09-25 |
| 0.14.2 | 0.10.1 (0.22.0) | bridge-mode check, settings windows and migration rehearsal of 2026-09-25 |
| 0.14.0 | 0.10.0 (0.20.0) | deployed with settings read only, 2026-09-25 |
| 0.13.0 | 0.9.0 (0.19.0) | third control window, 2026-09-24 |
| 0.12.0 | 0.8.0 (0.18.0) | second control window, 2026-09-24 |
| 0.11.0 | 0.7.1 (0.18.0) | first control window, 2026-09-24 |

The local backend does not use the bridge. The camera bridge of ha-eufy-cam
shares no port, token, account, data directory or library instance with the
mower bridge, and either runs while the other is absent or stopped.

## Upgrade

Update the bridge app first, then the integration.

1. Bridge app: copy the candidate's `eufy_mower_bridge` folder to
   `/addons/eufy_mower_bridge`, reload the store and update the app with a
   backup, for example `ha store reload` and
   `ha apps update local_eufy_mower_bridge --backup`. The Supervisor builds the
   image on the host. Check that `GET /v1/state` reports the new version.
2. Integration: back up the installed folder, `.storage/core.config_entries`,
   `core.entity_registry`, `core.device_registry` and
   `eufy_robomow.sessions.<entry id>`. Keep the backup private, because the
   config entry holds the account password and the local key. Replace the
   folder with the candidate's `eufy_robomow`, run the configuration check and
   restart Home Assistant.
3. Set the dashboard resource to the card version of the candidate, for
   example `/eufy_robomow/eufy-mower-card.js?v=0.7.3`.
4. From 0.8.0 or older, fix the Signal Strength statistics as the changelog of
   0.8.1 describes.

## Rollback

- Integration: put the previous folder back, run the configuration check and
  restart. The options and the session store have kept their format since
  0.11.0. Saving the options in an older version drops options it does not
  know.
- Bridge app: copy the previous version's app folder to
  `/addons/eufy_mower_bridge`, reload the store and update the app with a
  backup. The Supervisor installs the older version and keeps the data and the
  options. Restoring the backup of the upgrade is the alternative. Keep
  `bridge-id` and the session, because the integration's mower id depends on
  them.
- Docker: start the previous image tag with the same volume.
- Before a bridge older than 0.7.0, switch the integration's map source to
  `external`.

These steps were rehearsed on the owner's installation on 2026-09-25, see
the [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5832886365).
