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
| Integration `eufy_robomow` | 0.15.4 | `requests` 2.34.2, `tinytuya` 1.20.0, dashboard card 0.7.3 |
| Mower bridge, container image | 0.12.1 | `@keesmod/eufy-mega-client` 0.24.0 by release tarball and sha512 integrity, `node:24-bookworm-slim` by digest |
| Mower bridge, Home Assistant app `eufy_mower_bridge` | 0.12.1 | the same inputs, built by the Supervisor on the host |

The checksums of every candidate that was built are recorded in #8. The
candidates of 0.15.0 and 0.15.1 with bridge 0.11.0, built from `64ce862` and
`0ed3691`, matched the folders deployed on the owner's installation file for
file. Integration 0.15.2, built from `5b25e3f` (#56), also matched its 25
installed files after the 2026-09-25 deployment. The configuration check
passed before the Core restart. The entry loaded with all 23 enabled entities
available, the mower docked and the external map source retained. Its archive
SHA-256 is `94a59748161f72c8d2f6d8b949d9ab5ed8217df78bfcb799d539d45d67cbff9d`.
The candidate from `97f0062` (#59) contains integration 0.15.3 and bridge/app
0.12.0. The app was installed with a Supervisor backup on 2026-09-25 at
15:58 UTC, after the configuration check passed. Its 17 source files matched
the candidate. Runtime reported bridge 0.12.0 on library 0.24.0, with the same
options and bridge identity. The integration's 25 installed files matched the
0.15.3 candidate, its entry was loaded and all 23 enabled entities remained
available. The mower was docked, the backend local and the external map source
healthy. The bridge remained `observe_only`, with maps and settings writes
disabled. No Core restart or physical command was needed for this app update.
The app archive SHA-256 is
`60c4781398cee506e4ce491bc3cf7cce518112f885171176cb52cd1d5a042401`.
The integration archive SHA-256 is
`a9a6a4273690eb9b1a90c1a772b059b017feeafcb3be5a9df68bef6f8d3282e5`.

Library 0.24.0 was published separately with the owner's approval. Its
[release](https://github.com/keesmod/eufy-mega-client/releases/tag/v0.24.0)
comes from `3c21ac4`, and the downloaded package matched its manifest and
checksums. The mower candidates remain unpublished.

App 0.12.1 from `422a8f4` (#61) corrects the bootstrap's missing forwarding of
the cloud-map option. It replaced 0.12.0 with a Supervisor backup and passing
configuration check. All 17 app files matched the candidate and runtime
reported 0.12.1 on library 0.24.0. The app archive SHA-256 is
`424ef6f21121918a26109cf1c2ee4784bfce119cb56f5a1ee43aa23f84fb792e`.
Integration 0.15.3 is unchanged. The
[native trial receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5835828129)
records the two successful docked downloads, restart and restored configuration.

Integration 0.15.4 from `4754732` (#63) fixes a partial HTTP read found in the
native-source outage rehearsal. The integration now reads through EOF before
validating a bundle, retaining the 16 MiB limit. All 338 Python tests, Ruff,
types, frontend tests and eight CI jobs passed. The installed 25 files matched
the candidate after a private backup, passing configuration checks and a Core
restart. Its archive SHA-256 is
`58f6aea8389f102f7455e8789baeee9b01e3854733abfee538195edb393489dd`.
The repeated native-source switch, outage recovery and rollback passed on
2026-09-25. The owner then selected lasting native maps in `observe_only`.

CI runs the
tests on Home Assistant 2026.7.2, and the rehearsal ran on Home Assistant
2026.9.3 with Supervisor 2026.09.2. The hardware evidence below comes from the
owner's Home Assistant OS installation with the owned E15 (T2880) on firmware
6.9.28. A read-only check of the owner's running Anker eufy app on
2026-09-25 confirmed that version again. The earlier control and settings
windows did not re-read the firmware during each window.

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
  backup from 0.7.1 to 0.12.0 (2026-09-24 and 2026-09-25).
- The integration was upgraded from 0.7.0 to 0.15.3 by replacing its folder.
  The config entry and its entities were kept (2026-09-24 and 2026-09-25).
- The migration and rollback rehearsal, see the [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5832886365). It
  covered a switch refused while the bridge was unreachable, a bridge lost
  after the switch, and a restart in bridge mode without a running bridge. It
  also covered the integration from 0.14.2 to 0.14.1 and back, and the app from
  0.10.1 to 0.10.0 and back. Every entity id, unique id, session and dashboard
  reference was kept, and no command was sent (2026-09-25).

## Experimental

These capabilities still lack full hardware acceptance. Partial observations
below retain their stated scope.

- The broader native map acceptance and the `bridge` map source. Bridge 0.12.1
  obtains fresh provisioning from library 0.24.0 in explicit cloud mode, or
  reads an operator-supplied file in file mode. Two fresh docked acquisitions
  passed on 2026-09-25 with Android stopped and a bridge restart between them.
  Both matched the external source's static geometry and confirmed cancellation
  and cleanup. The timestamps were 7.639 seconds apart. The later rehearsal on
  integration 0.15.4 confirmed the HA source switch, a visible cached map during
  a bridge outage, automatic fresh acquisition after restart and rollback to a
  healthy external source. Existing identities, session objects, dashboard
  references and both map caches were preserved. Continuous updates while
  mowing and longer observation remain open.
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
- Observed native updates during mowing and longer daily-use observation.
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
