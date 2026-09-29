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
| Integration `eufy_robomow` | 0.17.0 | `requests` 2.34.2, `tinytuya` 1.20.0, dashboard card 0.7.4 |
| Mower bridge, container image | 0.14.0 | `@keesmod/eufy-mega-client` 0.27.0 by release tarball and sha512 integrity, `node:24-bookworm-slim` by digest |
| Mower bridge, Home Assistant app `eufy_mower_bridge` | 0.14.0 | the same inputs, built by the Supervisor on the host |

Integration 0.17.0 and bridge/app 0.14.0 consume the charger contact from
[library 0.27.0](https://github.com/keesmod/eufy-mega-client/releases/tag/v0.27.0).
A fresh connected contact shows `docked` unless equal or newer local activity takes precedence.
Card 0.7.4 already offers Start from that state with the existing control opt-ins.
The contact remains display evidence. Candidate `4fed5ea` replaced integration
0.16.3 and bridge/app 0.13.4 on 2026-09-29 after private source and storage
backups, Supervisor app backup `d1b492f9` and passing configuration checks.
Readback at 13:18:55 UTC verified exact installed sources, runtime library
0.27.0, unchanged identities, options and stored sessions, no unavailable
integration entities and a healthy map. Core and the app were restarted.

The owner's app-controlled E15 window then read connected at the station,
disconnected while mowing and after a stop on the lawn, and connected again
on return. HA showed `docked`, `mowing`, `unknown`, `returning` and `docked`.
At arrival the contact supplied `docked` while mission status still said
`returning`. Card 0.7.4's control function, evaluated against the captured HA
states, enabled Start at the station and disabled it after the lawn stop.
The physical commands used BatteryCam, so Start through HA was not retested
in that window. A supervised window later that day confirmed Start from
`docked` and dock through HA, see the
[Start receipt](https://github.com/keesmod/eufy-robomow-ha/issues/86#issuecomment-5892585855).
A map cancellation failed during the window with confirmed local cleanup.
The existing recovery policy then recovered without a restart: one probe
started at 13:37:04 UTC, confirmed cancellation and cleanup, and HA showed a
healthy map at 13:38:14 UTC. See the
[installation receipt](https://github.com/keesmod/eufy-robomow-ha/issues/86#issuecomment-5891236606)
and [recovery receipt](https://github.com/keesmod/eufy-robomow-ha/issues/86#issuecomment-5891459242).

The integration archive SHA-256 is
`7365660a0c2ba25a8a59a96e411dd852550a6aca4939a8ed1223573885c74a85`.
The app archive SHA-256 is
`57fa270e010572473f86c1568023702bca7d258d39fa2e335882e2f78b3a79f6`.

Integration 0.16.3 from `db343c7` (#84) records bridge-mode sessions from the
bridge's bounded cloud readings, see
[#83](https://github.com/keesmod/eufy-robomow-ha/issues/83). Local evidence
keeps precedence from 90 seconds before to 120 seconds after it, and a session
with a cloud reading carries `cloud_observed`. It replaced 0.16.2 on 2026-09-28
after a private backup and a passing configuration check. All 25 installed
files matched the candidate, and Home Assistant Core restarted from 19:27:27 to
19:28:00 UTC. The entry loaded with the same 23 entity ids and none
unavailable, the 50 stored sessions were kept and a resting mower opened no
session. A task recorded from cloud readings is not yet observed. The
integration archive SHA-256 is
`919c104381e666d45e7c307ef56b0a6383116f62d47574adbd2b6c2cb8f25579`. See the
[installation receipt](https://github.com/keesmod/eufy-robomow-ha/issues/83#issuecomment-5876965056).

Integration 0.16.2 from `b379117` (#80) carries card 0.7.4, see
[#79](https://github.com/keesmod/eufy-robomow-ha/issues/79). On the bridge
backend a resting mower reports no activity, so its entity stays `unknown`.
The card now shows such a mower with fresh telemetry as connected, shows the
settings without a known activity and hides progress, distance and area, which
the bridge cannot supply. Command gating is unchanged. It replaced 0.16.1 on
2026-09-28 after a private backup and a passing configuration check. All 25
installed files matched the candidate, and Home Assistant Core restarted from
18:05:30 to 18:06:04 UTC. The dashboard resource was then set to `?v=0.7.4`.
The entry loaded with the same 23 entity ids and none unavailable, the served
card reports 0.7.4 and the map entity was healthy. The integration archive SHA-256
is `7d0abfee20ec0b610b0ac6daa905b34f564b7fa984d82ed38cf14b9816250b42`. See the
[installation receipt](https://github.com/keesmod/eufy-robomow-ha/issues/79#issuecomment-5875742543).

Bridge/app 0.13.4 from `022810d` (#77) refuses a `control` configuration whose
two LAN steps and read-back reach the integration's 75-second command timeout,
see [#75](https://github.com/keesmod/eufy-robomow-ha/issues/75). The owner's
`control_read_back_ms` of 60000 exceeded that budget once `local_timeout_ms`
became 10000, explicitly on 0.13.1 from 07:56 UTC on 2026-09-28 and by default
from 0.13.2.
Before the update it was lowered to 54000,
with a private backup of the app options. App 0.13.4 replaced 0.13.3 on
2026-09-28 from 17:41:10 to 17:41:28 UTC with Supervisor backup `a9a11d61`.
All 17 installed app files matched the candidate list. The startup log shows
0.13.4 accepting `local_timeout_ms` 10000 with `control_read_back_ms` 54000.
The state route reported `control` with that read-back, auth connected, one
discovered mower and a fresh map whose acquisition confirmed cancellation and
cleanup. The integration's stored mower id matched the bridge's. All 23 enabled
entities remained present with none unavailable, and the map entity was
healthy again at 17:42:59 UTC. Integration 0.16.1 was unchanged and Home
Assistant Core did not restart. The app archive SHA-256 is
`8344727619b9518977f09cf3d07ec7d654f5a6dbce8e5b4dbce271d51f6b9c53`. See the
[installation receipt](https://github.com/keesmod/eufy-robomow-ha/issues/75#issuecomment-5875389046).

Bridge/app 0.13.3 from `5071678` (#73) adds one read-only recovery probe after
an unconfirmed map cancellation and a fifteen-minute wait. The owner accepted
the unknown remote transfer lifetime for this bounded rule, including future
use. Local acquisitions remain exclusive. See [recovery policy](map-recovery.md).

It replaced app 0.13.2 on 2026-09-28 with Supervisor backup `ca0d4988` and a
passing configuration check. The new process started at 13:03:42 UTC. Readback
at 13:05:36 UTC confirmed exact app sources, bridge 0.13.3 on library 0.25.2,
unchanged identities and settings, and a healthy map whose acquisition had
confirmed cancellation and cleanup. All 23 enabled entities remained present,
with none unavailable or newly unknown. Integration 0.16.1 was unchanged and
Home Assistant Core did not restart. The app archive SHA-256 is
`d66d1f6bc3662d1c1ef184d2d47f9e87c70c0d419d7090a3aaa7fd8b6e20448c`.
This verifies installation and ordinary acquisition, not recovery after a
natural unconfirmed cancellation. #71 was closed at the owner's request with
that observation waived, see the
[installation receipt](https://github.com/keesmod/eufy-robomow-ha/issues/71#issuecomment-5870459019)
and the [closure](map-recovery.md#closure-of-71).

Bridge 0.13.2 only raises the default of `local_timeout_ms` from 5000 to 10000,
so a LAN step outlasts the mower's Wi-Fi outages of 5 to 7 seconds, see the
[bridge changelog](../bridge/CHANGELOG.md). From 07:56 UTC on 2026-09-28 the
owner's installation first ran bridge 0.13.1 with that option set to 10000.
The measured windows then showed 0 LAN `request_timeout` errors, against 2.3
an hour before, see the
[measurement](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5869155614).
App 0.13.2 from `ab9d169` (#70) replaced 0.13.1 through a Supervisor update
with its own backup of 0.13.1 on 2026-09-28, from 11:51:45 to 11:52:04 UTC.
All installed app files matched the candidate list. The explicit
`local_timeout_ms` option was removed, and the startup log shows 0.13.2 with
`local_timeout_ms` 10000. The state route reported `control` and auth
connected. The mower entity kept its controls and was never unavailable, and
the map was healthy again at 11:54:17 UTC. The app archive SHA-256 is
`8cf3ab9d65344104113632df74cb3c9eec50ac63d91ad811f47158be0dd928fa`.
Integration 0.16.1 was not touched. Until the 0.16.2 update its installed folder
differed from the 0.16.1 in the candidate only by one code comment in
`bridge_client.py`, which #70 changed without a version bump. See the
[deployment receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5869490828).

Library [0.25.2](https://github.com/keesmod/eufy-mega-client/releases/tag/v0.25.2)
was published separately with the owner's approval from `c41af2c`
([library PR #205](https://github.com/keesmod/eufy-mega-client/pull/205)). It
renews a map-enabled cloud session before map provisioning would be refused.
Its downloaded archive matched the release manifest and checksums, with SHA-256
`296876ef8dc52a9d931ef4f0efe3cf258563f11a06e853c7a4bebce32f71fd9f`, and is
byte-identical to the release rehearsal of the same commit. On the owned E15
the bridge renewed 79 seconds before the local expiry between idle demands,
and once during a running stream demand, which ended normally, see the
[first](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5857287766) and [second](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5857986097) receipts.

Library [0.25.1](https://github.com/keesmod/eufy-mega-client/releases/tag/v0.25.1)
was published separately with the owner's approval from `de5b45b`. Its downloaded
archive matched the release manifest and checksums, with SHA-256
`99e00c72c41d56723a39bd3c8ab212c7c8f71532a554f49329dab90875aa5dcc`.
The decompressed archive is byte-identical to the candidate that passed a
30-second docked demand and a ten-second explicit abort, both with confirmed
cancellation and cleanup. [Library PR #203](https://github.com/keesmod/eufy-mega-client/pull/203)
records the docked observation. The later short moving-map run and the
integration 0.16.1 replay fix are recorded in the [issue #8 receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5845487941).

Candidate checksums are recorded in #8 and its follow-up #71. The
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
windows did not re-read the firmware during each window, and the 2026-09-27
receipts do not record it.

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
- Start from idle at the station and from charging, pause, resume through
  start while paused, and dock, each confirmed by the bridge's fresh report,
  with the starts and the dock also checked in the eufy app (0.16.1 with
  bridge 0.13.1 on library 0.25.2, 2026-09-27,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5857986097)).
- The cloud session renewed before map provisioning would be refused, between
  idle demands and during a running stream demand, which ended normally
  (bridge 0.13.1 on library 0.25.2, 2026-09-27).
- Start from `docked` through the service the card's Start button sends,
  confirmed by the bridge's fresh report 1.2 seconds after the call, and dock,
  confirmed at the map save about 10 seconds before the charger contact
  returned, both checked in the eufy app. The charger contact cleared after
  the start and returned at the station, and the session was recorded
  without an observation gap (0.17.0 with bridge
  0.14.0 on library 0.27.0, 2026-09-29,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/86#issuecomment-5892585855)).
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
- The app was updated from 0.13.0 to 0.13.1 with a Supervisor backup, and the
  persisted session was kept (2026-09-27).
- After a host reboot the Supervisor started the app through `boot: auto`.
  The bridge restored its session without a login, the mower entity offered
  control again and the map was healthy about 4.7 minutes after the reboot
  request. The mower entity read `unknown` instead of `docked` until a
  confirmed command or a local report (2026-09-27,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5857986097)).
- The app was updated from 0.13.1 to 0.13.2 with a Supervisor backup, and all
  installed app files matched the candidate list. The explicit
  `local_timeout_ms` option was removed for the new default of 10000. The
  bridge came back in `control`, the mower entity was never unavailable and
  the map was healthy again at 11:54:17 UTC (2026-09-28,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5869490828)).
- The app was updated from 0.13.2 to 0.13.3 with Supervisor backup
  `ca0d4988`. The installed app sources matched the candidate, identities and
  settings were unchanged and an ordinary map acquisition confirmed
  cancellation and cleanup (2026-09-28,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/71#issuecomment-5870459019)).
- The app was updated from 0.13.3 to 0.13.4 with Supervisor backup
  `a9a11d61` after `control_read_back_ms` was lowered from 60000 to 54000.
  All 17 installed app files matched the candidate list, the bridge came back
  in `control` with read-back 54000 and the map was healthy again at 17:42:59
  UTC (2026-09-28,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/75#issuecomment-5875389046)).
- The integration was updated from 0.16.1 to 0.16.2 by replacing its folder
  after a private backup and a passing configuration check. All 25 files
  matched the candidate, the config entry and its 23 entity ids were kept and
  the dashboard resource moved to card 0.7.4 (2026-09-28,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/79#issuecomment-5875742543)).
- The integration was updated from 0.16.2 to 0.16.3 by replacing its folder
  after a private backup and a passing configuration check. All 25 files
  matched the candidate, and the config entry, its 23 entity ids and the 50
  stored sessions were kept (2026-09-28,
  [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/83#issuecomment-5876965056)).

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
  references and both map caches were preserved. On 2026-09-26, bridge 0.13.0
  and library 0.25.1 passed a short supervised E15 moving-map run. During the
  active window, bridge publications had a median interval of 2.002 seconds
  and a maximum of 6.048 seconds, with cancellation and cleanup confirmed.
  Integration 0.16.1 prevents old cached paths and tracking from seeding the
  current task. It passed offline replay and separate installation readback.
  The mowing task of 2026-09-27 ran on 0.16.1, but its receipt does not assess
  path coverage. On 2026-09-27 bridge 0.13.1 on library 0.25.2 streamed 87
  demands during a mowing task, with 2,447 publications and none rejected.
  Three ended with `connection_failed` while the mower reported its weakest
  Wi-Fi readings, and the bridge resumed after its retry interval each time.
  A session renewal during a running stream demand and a host reboot passed,
  see the [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5857986097). The installation now runs in
  `control` with settings writes disabled. Bridge 0.13.1 then ran 13 hours
  overnight in daily use with the mower at its station. The map stayed healthy,
  with no map error, and 14 session renewals passed without error, see the
  [daily-use receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5864644965).
  Bridge 0.13.3 adds one recovery probe after a fifteen-minute cool-down.
  One natural unconfirmed cancellation recovered without a bridge restart
  on 2026-09-29, using bridge 0.14.0 and library 0.27.0. The probe confirmed
  cancellation and cleanup and HA returned to a healthy map. This does not
  establish a remote expiry bound, see [recovery evidence and limits](map-recovery.md).
- The compatible `external` HTTPS map source remains available as a manual
  recovery route.
- The no-go zones on the map since 0.15.1, from map-record field 12. The
  owner's one zone was compared with the app before the change, and the
  drawing is software-verified with synthetic geometry.
- The cloud settings of the local backend: edge distance, pad direction, path
  distance, travel speed and blade speed, inherited from upstream.
- Edge distance, pad direction and path distance in bridge mode, read only
  there since 0.15.0.
- The standalone container image outside CI.
- The app on aarch64. The app declares it and an early bridge image passed an
  offline health check on arm64 in #23, but CI builds on amd64 only and map
  acquisition and control have run only on the owner's amd64 host.
- The opt-in planning package. Automatic mowing stays off.
- Any model other than the E15. E18 support is not claimed.

## Outstanding obligations

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
| 0.17.0 | 0.14.0 (0.27.0) | the current candidate, exact installed sources and preserved identities/options/session storage. On 2026-09-29 the E15 charger contact passed the station, lawn-stop and return checks through library 0.27.0, bridge and HA. Physical commands used BatteryCam. The existing map recovery policy also recovered once without a restart, see the #86 receipt. Later that day a Start from `docked` and a dock through HA were each confirmed by the bridge and the eufy app |
| 0.16.3 | 0.13.4 (0.25.2) | the previous candidate, whose integration matched file for file after the 2026-09-28 update, with the entry, its entity ids and the stored sessions kept. A task recorded from cloud readings is not yet observed |
| 0.16.2 | 0.13.4 (0.25.2) | the previous candidate, whose integration matched file for file after the 2026-09-28 update with card 0.7.4, the entry and its entity ids unchanged |
| 0.16.1 | 0.13.4 (0.25.2) | an earlier candidate, whose app matched file for file after the 2026-09-28 update, with `control_read_back_ms` lowered to 54000 within the enforced budget, identities and integration unchanged and native-map acquisition healthy. Recovery after an unconfirmed cancellation was unobserved at that installation, see the #71 waiver |
| 0.16.1 | 0.13.3 (0.25.2) | an earlier candidate, whose app matched file for file after the 2026-09-28 update, with identities, settings and integration unchanged and ordinary native-map acquisition healthy. Recovery after an unconfirmed cancellation was unobserved at that installation |
| 0.16.1 | 0.13.2 (0.25.2) | an earlier candidate, whose app matched file for file after its deployment on 2026-09-28, while integration 0.16.1 differed from the candidate only by one code comment until the 0.16.2 update |
| 0.16.1 | 0.13.1 (0.25.2) | an earlier candidate, matched file for file after its deployment, control window, session renewals and host reboot of 2026-09-27, run with `local_timeout_ms: 10000` on 2026-09-28 until the 0.13.2 update |
| 0.15.1 | 0.11.0 (0.23.0) | matched file for file after its deployment on 2026-09-25 |
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
   example `/eufy_robomow/eufy-mower-card.js?v=0.7.4`.
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
