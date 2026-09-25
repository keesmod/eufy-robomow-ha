# Changelog

Changes to the Eufy Robomow Home Assistant integration in
`custom_components/eufy_robomow`. The version is the one in `manifest.json`.
The mower bridge and its Home Assistant app have their own
[changelog](bridge/CHANGELOG.md). No version has been published as a release
or through HACS. The inherited upstream history declares no license, so
publication waits until that boundary is resolved.

Every entry states its evidence. "Software-verified" means CI and synthetic
tests without a mower. Hardware evidence comes from the owned E15 through Home
Assistant, recorded in dated comments on
[issue #8](https://github.com/keesmod/eufy-robomow-ha/issues/8) unless another
record is named.

Upgrade and rollback work the same way for every version: replace the installed
`custom_components/eufy_robomow` folder with the version's folder, run the
configuration check and restart Home Assistant. Entity unique ids are built from
the config entry's device id, so no version renames or duplicates an entity.
The session store has kept storage version 1 since 0.7.0 and the options have
kept their keys since 0.11.0. An older version ignores an option it does not
know, but saving the options there drops it.

## 0.14.2 - 2026-09-25

### Bridge 0.10.1 on library 0.22.0

- Pairs with mower bridge 0.10.1, which pins library 0.22.0. The library reads
  DP 107 as the mower's mission status, as the local backend has since 0.14.1.
- In bridge mode a Box, zone or scheduled task now reports `mowing` or `paused`
  instead of an invalid status. A message without a mission reports `idle`,
  which shows as `docked` and ends the session.
- The integration's code is unchanged. Only comments and docs changed.
- Upgrade: update the bridge app with a backup, then the integration. Rollback:
  install 0.14.1.
- Evidence: on 2026-09-25 a Box task's pause and resume through Home Assistant
  in bridge mode were reflected within 0.6 seconds, and its dock showed the
  drive home and the arrival
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5830638474)).
  The mow height went from 40 to 45 mm and back through Home Assistant in
  bridge mode
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5830926513)).

## 0.14.1 - 2026-09-25

### DP 107 read as the mission status

- `robot_status` reads the fields of DP 107 as the official app's mission
  status instead of matching exact payloads. A running or paused mowing mission
  reads as `mowing` or `paused`, the running recharge mission as `returning`,
  a map save without a mission as `map_saving`, and a message without mission,
  sub-mission, state or error flag as `idle`.
- New attribute `robot_power_mode`: `running`, `standby` or `hibernate`.
- A Box task reads as `mowing`, hibernation as `idle`, and a start in Box mode
  while the mower rests in the dock is confirmed from the cloud.
- Mapping without mowing, a paused return and the first frame of a start stay
  unread. Bridge mode is unchanged.
- Upgrade: no option change. Rollback: install 0.14.0.
- Evidence: after the deployment on 2026-09-25 the local backend showed
  `docked` with `robot_power_mode: hibernate` during a rest in the dock
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5829668644)).

## 0.14.0 - 2026-09-25

### Settings through the bridge

- With mower bridge 0.10.0 on library 0.20.0, bridge mode creates Cut Height,
  Volume and the five local switches with the unique ids of the local backend
  and reads them from the bridge's state document.
- Cut Height, Volume, Smart No-Go Suggestions and Mow Yellow Grass are written
  through the bridge's settings route, once each, in the integration's
  `control` mode and only while the bridge reports `routes.settings`.
- Stop on Rain, Child Protection and Real Lawn Map refuse any change in bridge
  mode before a request, as the owner decided in #8.
- Edge distance, pad direction, path distance and the two speeds stay local
  backend only. With an older bridge the setting entities in bridge mode have
  no value. The local backend is unchanged.
- Upgrade: no option change. Rollback: install 0.13.4. In bridge mode the
  setting entities then become unavailable and keep their registry entries.
- Evidence: deployed on 2026-09-25 with bridge 0.10.0 and settings read only.
  The supervised write ran on 0.14.2.

## 0.13.4 - 2026-09-25

### A start while resting in the dock, and the first poll of the drive home

- About five minutes after each arrival the mower rests in the dock with DP 1
  true and DP 118 at 100. In that shape a pending start or resume asks the
  cloud at every local poll, and a fresh `mowing` payload confirms it with the
  evidence `cloud_mowing_reported`. This is the only command the cloud
  confirms.
- The poll that confirms a dock may ask the cloud, so `returning` shows at
  once.
- A command confirmed during a refresh that outlasts the 35-second bound stays
  confirmed.
- Card 0.7.3 labels the new evidence.
- Upgrade: set the dashboard resource to
  `/eufy_robomow/eufy-mower-card.js?v=0.7.3`. Rollback: install 0.13.3 and set
  `?v=0.7.2`.
- Evidence: on 2026-09-25 a start during the rest was confirmed from the cloud
  after 11.6 seconds, and a dock showed `returning` on its confirming poll
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5829418938)).

## 0.13.3 - 2026-09-25

### A start after a map save, and a pause during the drive home

- A start is also confirmed when DP 1 turned true from the status the command
  was chosen from, with the evidence `task_started`. A resume is also confirmed
  when DP 2 turned false, with the evidence `pause_cleared`. DP 118 stays at
  100 after a map save, so a start right after an arrival timed out before.
- The local backend refuses a pause while DP 1 is false, before any write,
  because the E15 ignores a pause during the drive home. An unknown task flag
  never blocks a pause.
- Card 0.7.2 offers pause only while mowing.
- Upgrade: set the dashboard resource to `?v=0.7.2`. Rollback: install 0.13.2
  and set `?v=0.7.1`.
- Evidence: in the supervised window of 2026-09-25 through the local backend a
  pause during the drive home was refused without a write
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5828944196)).

## 0.13.2 - 2026-09-25

### The drive home in the local backend

- When DP 1 turns false after a mowing, paused or returning reading, the
  coordinator asks the cloud at once and at every local poll for two minutes,
  also at night. After that it asks every minute while DP 107 still reads
  `returning`.
- The `returning` payload reads as `returning` and the map-saving payload as
  `docked` in every task shape. Only a cloud poll taken since the local status
  took its shape decides.
- A drive home costs about five to ten extra cloud requests. Bridge mode is
  unchanged.
- Upgrade: no option change. Rollback: install 0.13.1.
- Evidence: in the window of 2026-09-25 Home Assistant showed `returning` from
  the first poll after a dock's confirming poll until the map save
  ([deployment](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5828300788),
  [window](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5828944196)).

## 0.13.1 - 2026-09-24

### The map save at the arrival reads as docked

- A local poll that caught the map save at the dock arrival, with DP 1 still
  true, reported `mowing`. The map-saving DP 107 payload from the cloud now
  reports `docked`, and `robot_status` can read `map_saving`.
- Upgrade: no option change. Rollback: install 0.13.0.
- Evidence: deployed on 2026-09-24 during a rest in the dock, which stayed
  `docked`
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5815137640)).
  The evening rest of that day stayed `docked` as well
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5828300788)).

## 0.13.0 - 2026-09-24

### The drive home after a dock in bridge mode

- With mower bridge 0.9.0 on library 0.19.0, a bridge poll during a running
  command carries the command's latest confirmed activity. The entity shows
  `returning` during the drive home after a dock and `docked` at the confirmed
  arrival.
- An uncertain answer keeps a report the command itself produced and clears
  only older evidence. An unknown `command` block is ignored, and an older
  bridge sends none.
- Upgrade: no option change. Rollback: install 0.12.1.
- Evidence: in the third control window of 2026-09-24 Home Assistant showed
  `returning` 11 seconds after a dock through the bridge
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5815137640)).

## 0.12.1 - 2026-09-24

### Resting in the dock reads as docked

- The local status DP 1 true, DP 2 false and DP 118 at 100 is ambiguous. On
  2026-09-24 it held for about fifteen minutes after each dock arrival and each
  evening while the mower rested in the dock.
- In that shape the coordinator asks the cloud at once and then every minute,
  also at night. The default DP 107 payload reports `docked`, the `paused` and
  `returning` payloads report themselves, and anything else keeps `mowing`.
- New attribute `robot_status`.
- Upgrade: automations that wait for `docked` see it at the arrival instead of
  about a quarter of an hour later. Rollback: install 0.12.0.
- Evidence: confirmed together with 0.13.1, see there.

## 0.12.0 - 2026-09-24

### Activity from confirmed commands in bridge mode

- A confirmed command stands in for the polled activity for at most 30
  minutes: `mowing` after start or resume, `paused` after pause and `docked`
  after a confirmed dock. A newer reported poll replaces it. An uncertain
  command or a `mower_command_already_set` refusal clears it.
- Start while paused therefore sends resume, and a confirmed dock ends the
  session. The live map follows the same activity.
- New attributes `bridge_activity_source` and `bridge_activity_observed_at`.
  `mower_binding_unavailable` counts as refused before the write.
- Pairs with mower bridge 0.8.0, which renews its hourly cloud session.
- Upgrade: no option change. Rollback: install 0.11.0.
- Evidence: in the second control window of 2026-09-24, start, pause, start
  while paused as resume and dock through Home Assistant were confirmed, and
  the bridge renewed its session
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5811629836)).

## 0.11.0 - 2026-09-23

### The map through the bridge

- New option **Map source**: `external`, the default, or `bridge`. `external`
  is the Android map helper or another compatible HTTPS source and stays the
  manual recovery path. `bridge` is the mower bridge's read-only map route.
- The bridge source keeps its own latest-good file `bridge.mapbundle` next to
  `latest.mapbundle`, so switching never mixes or discards the other source's
  map. The options flow refuses `bridge` without the bridge backend or without
  `routes.maps`.
- In bridge mode the live map follows the bridge's reported mowing, paused or
  returning activity.
- Upgrade: existing entries keep `external`. Rollback: install 0.10.0, which
  has no map source option and reads the external URL.
- Evidence: deployed on 2026-09-24 on the local backend with every entity
  unchanged
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5809904636)),
  and the first control window ran on it
  ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5810889297)).
  No live map has been acquired through the bridge yet.

## 0.10.0 - 2026-09-20

### Dock through the bridge

- With mower bridge 0.6.0 on library 0.17.0, bridge control mode exposes dock
  and routes it through the bridge's `stop` class. On the owned E15 a stop ends
  the task and the mower returns to the dock by itself. The firmware ignores
  the library's return over DP 3, so there is no stop in place.
- A confirmed dock carries the evidence `bridge:map_saving`, the dock arrival
  the library observed. An uncertain dock is never repeated.
- Upgrade: no option change. Rollback: install 0.9.0, which offers no dock in
  bridge mode.
- Evidence: software-verified at release. Docks through Home Assistant in
  bridge mode were confirmed in the later control windows of #8.

## 0.9.0 - 2026-09-19

### Commands through the bridge

- With mower bridge 0.5.0 on library 0.16.0, start, pause and resume go through
  the bridge's command routes, once each, when both the integration and the
  bridge run in `control` mode. The integration reads `routes.control` from the
  bridge on every poll.
- The `command` attribute gains `uncertain` for a written but unconfirmed
  command, and `bridge:` evidence values. The mower entity gains
  `bridge_control`.
- A pause supersedes a pending start and waits for its answer. Nothing is
  replayed on a reload, a backend switch or a bridge reconnect.
- Session history grows in bridge mode from reported activities.
- Upgrade: no option change. Rollback: install 0.8.2.
- Evidence: software-verified at release.

## 0.8.2 - 2026-09-19

### Bridge activity

- With mower bridge 0.4.0 on library 0.15.0, bridge mode reports mowing, paused
  and returning from the confirmed E15 payloads.
- New attribute `bridge_status`: `reported`, `missing`, `invalid` or
  `unconfirmed`.
- Upgrade: no option change. Rollback: install 0.8.1.
- Evidence: software-verified at release.

## 0.8.1 - 2026-09-19

### Signal Strength as the mower's declared percentage

- The mower declares DP 109 as a percentage from 0 to 100. Earlier versions
  negated it and labelled it dBm. The sensor keeps its entity id and unique id
  and reports the percentage without a device class.
- Upgrade: Home Assistant detects the changed unit of the long-term statistics
  and offers a repair. The old values were the negated percentage, so they can
  be kept with the sign restored under **Developer tools → Statistics**.
  Automations with negative dBm thresholds need the percentage. Rollback:
  install 0.8.0, which changes the unit back and raises the repair again.
- Evidence: the mower's own declaration, recorded in the library's
  [telemetry receipt](https://github.com/keesmod/eufy-mega-client/blob/7117b9381aa7b3df879e11271e5d5e6cb9a116d2/docs/research/E15_TELEMETRY_OBSERVATION_2026-09-16.md).
  On 2026-09-24 the local backend and the bridge both reported the same
  percentage.

## 0.8.0 - 2026-09-19

### Mower backend option

- New option **Mower backend**: `local`, the default and unchanged, or
  `bridge`. The bridge backend reads state from the dedicated mower bridge with
  its URL, bearer token, optional mower id and optional certificate
  fingerprint. It creates no local connection and no cloud client of its own.
- Saving validates the token against the bridge and resolves the mower id when
  the bridge discovers exactly one mower. A bridge that cannot be reached is
  refused and nothing is saved.
- A backend switch never duplicates or renames entities. Bridge mode has no
  controls and no setting entities in this version.
- Upgrade: existing entries keep `local`. Rollback: switch the backend to
  `local`, then install 0.7.0.
- Evidence: software-verified at release.

## 0.7.0 - 2026-09-06

### Dashboard, confirmed commands and session history

- New dashboard card with map zoom and pan, settings, observed session history
  and visible command outcomes. Card 0.7.1 of the same day follows the Home
  Assistant theme and adds four native tabs.
- Start, resume, pause and return wait for fresh local telemetry. A pending
  start cannot repeat and a safety command can supersede it.
- Fifty observed session summaries are stored privately and survive a restart.
  Gaps stay visible and no mowing time or area is invented.
- An opt-in Home Assistant planning package in `examples/`, off by default.
- The map image entity now polls, so the map refreshes after setup.
- There is no 0.6.0 in this repository. 0.7.0 follows 0.5.1.
- Upgrade: add the dashboard resource
  `/eufy_robomow/eufy-mower-card.js?v=0.7.1`. Rollback: install 0.5.1. The
  session store stays in place for a later upgrade.
- Evidence: a supervised test on the owned E15 on Home Assistant 2026.9.0,
  where start, pause, resume, pause and return were confirmed by fresh
  telemetry, see [validation 2026-09-06](docs/validation-2026-09-06.md).

## 0.5.1 - 2026-07-27

### Review fixes

- The options flow uses Home Assistant's automatic reload instead of a manual
  listener.
- The map source backs off before the first success, keeps separate ETags for
  idle and live pulls, rejects older snapshots and commits an ETag only after
  validation. The map cache uses `0700` directories and `0600` files.
- Every decoded pathway is rendered and markers stay on the canvas.
- `requests` is declared as a direct dependency.
- Upgrade: none. Rollback: install 0.5.0.
- Evidence: software-verified.

## 0.5.0 - 2026-07-27

### Safe control mode and the read-only map

- This fork adopted the upstream integration with a safe control mode. That
  first change kept the manifest at 0.4.0. New and legacy entries without an
  explicit mode start in `observe_only`, where commands and writable settings
  are hidden, and `control` is an explicit option. Write failures reach Home
  Assistant instead of being swallowed, raw DPS values are no longer logged
  and TinyTuya is pinned.
- Optional read-only E15 map as an image entity from an authenticated HTTPS map
  source, with five-minute idle snapshots and two-second ETag-aware pulls while
  mowing. One latest-good bundle is stored privately.
- Upgrade: an entry without an operating mode becomes read only. Select
  `control` in the options to get commands and setting entities back.
  Rollback: install 0.4.0.
- Evidence: the live map was exercised on the private installation before this
  public branch.

## Upstream releases

0.2.0 (2026-02-25), 0.3.0 (2026-02-26) and 0.4.0 (2026-05-08) come from
jnicolaes/eufy-robomow-ha before the fork. Their history is kept as it is.

- 0.4.0: a switch platform for rain stop, child protection and three more
  options, advanced entities disabled by default, zone mowing removed, and the
  docked state fixed during an active session. Later commits without a version
  change fixed the returning range, added brand images and improved the cloud
  sign-in error handling.
- 0.3.0: the pad direction moved to DP 155 field 4.
- 0.2.0: the first release, with local polling, cloud settings and local-key
  discovery.
