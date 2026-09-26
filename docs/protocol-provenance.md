# Protocol provenance policy

Protocol knowledge enters this repository only through independently reproducible evidence.

## Accepted evidence

- Official Eufy documentation and user-visible app behavior.
- Redacted observations from an owned E15: before/action/after state, timestamps, screenshots, packet metadata, and resulting mower or app state.
- Synthetic fixtures derived from a confirmed field definition.
- A third-party implementation only when its license and provenance explicitly permit reuse.

## Confirmation levels

- `hypothesis`: a plausible interpretation with insufficient evidence.
- `observed`: seen once or twice and not yet safe to encode as a stable contract.
- `confirmed`: independently reproduced at least three times on the recorded app and firmware versions.
- `hardware-validated`: confirmed through supervised physical behavior, not only transport acknowledgement.

Only confirmed facts belong in production constants and parsers. Physical commands additionally require hardware validation and rollback instructions.

## Required capture context

Record the Eufy app version, mower model, mower firmware, timezone, operating mode, and the exact user action for every series. Keep speculative observations in tracker issues until confirmed.

## Redaction

Never commit raw captures. Remove passwords, tokens, local keys, full device and account identifiers, serials, ICCIDs, MAC addresses, IP addresses, exact coordinates, and private lawn geometry. Replace stable values with documented synthetic placeholders so tests preserve structure without preserving identity.

## Other repositories

Unlicensed projects and forks may be used to enumerate features and design experiments. Do not copy or mechanically translate their implementation, constants, schemas, fixtures, or tests. Record independently reproduced facts in this repository's own evidence trail.

## DP 109 signal unit

The mower's own data-point declaration, retrieved through discovery on the
owned E15 (product code T2880, firmware 6.9.28), names DP 109
`wifi_signal_strength`: a read-only integer from 0 to 100 with unit `%`. Nine
raw local queries and three typed queries on 2026-09-16 returned the same
percentage, recorded in the library's
[telemetry observation receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_TELEMETRY_OBSERVATION_2026-09-16.md).
No dBm value and no conversion formula were observed. The percentage is
`confirmed`. The earlier negative-dBm reading inherited with the upstream fork
had no evidence in this repository and was a `hypothesis`; it was removed in
version 0.8.1.

## DP 107 activity

The mower declares DP 107 `robot_status` as a raw data point. Library
`@keesmod/eufy-mega-client` 0.23.0, pinned by the mower bridge to the release
tarball with SHA-256
`3ddf499b5d1899c3bf1979b7597deb88926d4560b07e5737ca165f5640432c75` from source commit
`6d9427e3e2a1a9a9a1a800173f5347ade993bca6`, confirms three payloads on the
owned E15 (product code T2880, firmware 6.9.28, Anker eufy app 6.1.00): fields
1 = 2 and 3 = 1 `mowing`, fields 1 = 2 and 3 = 2 `paused`, and fields 1 = 1 and
3 = 1 `returning`. Each reached at least four app-correlated transitions across
two owner-operated windows, recorded in the library's
[contract receipt](https://github.com/keesmod/eufy-mega-client/blob/ee1ac36bead945445aee63fe049b295e81b9eafc/docs/research/E15_ROBOT_STATUS_CONTRACT_2026-09-16.md)
and
[reproduction receipt](https://github.com/keesmod/eufy-mega-client/blob/ee1ac36bead945445aee63fe049b295e81b9eafc/docs/research/E15_ROBOT_STATUS_REPRODUCTION_2026-09-19.md).
The three activities are `confirmed`. Mower bridge 0.7.0 serves them in the
`status` field of the state route and the integration's bridge mode (0.8.2)
maps them onto the mower entity, both with the observation time of the query
that carried the payload.

Since 0.22.0 the library also reads DP 107 as the mower's mission status, with
the source and evidence of the
[mission status receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_MISSION_STATUS_SCHEMA_2026-09-25.md).
Every mowing mission reports `mowing` or `paused`, the Box, zone and scheduled
tasks included. A message without mission, sub-mission, state or error flag
reports `idle`, the default payload, hibernation and field 6 = 1 included.
Mower bridge 0.10.1 serves these readings. Integration 0.16.0 removes the
previous bridge mapping from `idle` to docked, because mission inactivity
alone does not establish a physical dock arrival.

Exact remaining limits. No payload identifies `docked`, `charging` or `error`,
and dock arrival is never inferred from inactivity. `idle` means no mission and
also follows the app's Stop on the lawn. The mowing-progress source is
unidentified and `progress` stays `unconfirmed`, DP 118 remains map-save
progress. The app's Defogging phase shares the `mowing` payload. The
transitional first frame, the map-saving payload, a paused return and missions
that do not mow are withheld, so a query that carries one of them reports
`status` as `invalid`, and a query without DP 107 reports `missing`. Nothing is
derived from the age of a report or the absence of a data point.

Bridge 0.13.0 reads cloud activity separately through library 0.25.1
`queryCloudState`. One authenticated device-get response supplies DP 107 and
DP 155, with explicit cloud receipt time. The owned E15 returned typed `idle`
on this route while the owner reported it docked on 2026-09-26. Integration
0.16.0 can use a healthy cloud receipt no older than 90 seconds for display and
map streaming, after local and confirmed-command evidence. This is not a
device timestamp and never confirms a bridge command. Live moving-map
acceptance remains in #8.

The local backend's status reply never carries DP 107, but the cloud DPS do.
Since integration 0.12.1 the local backend reads DP 107 from them with the
same three definitions, the exact map-saving payload and the default payload,
and only a cloud poll taken since the local status took its current shape
decides. It asks where DP 1, DP 2 and DP 118 cannot show the activity. The
first case is the shape DP 1 true, DP 2 false and DP 118 at 100, where a task
that mows looks like the mower resting in the dock. Since 0.13.2 it also asks
during the drive home after a task, which runs with DP 1 false, and during the
map save, in which DP 118 rises from 1 to 100 with DP 1 true at each dock
arrival and after the app's Stop. The recorder showed that sequence at every
natural end of a task from 2026-09-14 to 2026-09-24: DP 1 false for 50 to 60
seconds, then the map save. The library's stop window of 2026-09-20 recorded
the matching LAN reports. The default payload means `docked` only in the
first shape, where the app showed the mower idle or charging in the dock.

A supervised window on 2026-09-25 measured this path on the owned E15 through
the local backend. After each of two docks from Home Assistant the cloud DPS
showed DP 1 false and the `returning` payload no later than 1.6 and 2.3
seconds after the command, sampled every two seconds. The dock arrival followed after 34 and
45 seconds with DP 1 true, DP 118 from 1 and the map-saving payload. Home
Assistant showed `returning` from the first poll after the dock's confirming
poll and `docked` at the map save. A pause, DP 2 true, written ten seconds into
the second drive left DP 2 false and DP 107 `returning` until the arrival, so
the E15 ignores a pause without a running task. A start from the dock right
after an arrival set DP 1 true while DP 118 stayed at 100 from the map save.

A start from Home Assistant during the rest in the dock, with DP 1 already true
and DP 118 at 100, started the mower as well. The cloud showed DP 107 turn from
the default payload through field 1 = 2 into the `mowing` payload within 4
seconds, while the local status stayed unchanged. Since 0.13.4 the local backend
confirms such a start from the cloud's `mowing` payload read after the write.
This is the only command confirmation that uses the cloud. A Box task started
in the app reported DP 107 field 1 = 17 with field 3 = 1, at times with field 2
= 3, while the app showed Mowing…. Integration 0.13.4 left that payload unread,
and the local backend kept reading `mowing` from its local status. Since
0.14.1 it reads as `mowing`, see the mission status below.

The same Box task then ended by itself after about ten minutes. DP 1 turned
false, and the cloud showed the `returning` payload at once, the same payload
as after a stop. The mower drove home for about 39 seconds before the map save
at the arrival. The local backend showed `returning` from its first poll after
DP 1 turned false until the map save and `docked` from then on. So the natural
end of a task follows the same sequence as a stop. On integration 0.13.4 a
start during the rest in the dock was confirmed from the cloud's `mowing`
payload 12 seconds after the command. The poll that confirmed the following
dock already showed `returning`.

### The mission status schema

The official app's product script for the T2880, `T2880.js` of Anker eufy
6.1.00 with SHA-256
`0be33785e7c70d2c2e890d0f3b9d4ee512dca527b447ca95651ba683d5ef048d`, is the
same script whose map numbering keesmod/eufy-mega-client#51 used. It was read
on the owner's Mac on 2026-09-25 and decodes DP 107 as the mower's mission
status. The script is not copied here. Only the facts the integration uses
follow, all varints with an absent field counting as zero:

- **Field 1, mission.** 0 is none and 1 is the recharge mission. The mowing
  missions are the whole lawn (2), mapping while mowing (4), a temporary task
  (5), remote-controlled mowing (7), the scheduled whole lawn (8), scheduled
  mapping while mowing (9), a selected zone (10), a scheduled zone (16), a
  drawn box (17), edge trimming (18) and scheduled edge trimming (22). Other
  values, such as mapping without mowing (3), are not read.
- **Field 2, sub-mission.** Relocation (1), leaving the station (3), saving the
  map (5), setting the blade height (6) and defogging (9), among others.
- **Field 3, state.** Idle (0), running (1), paused (2), aborted (3) and
  complete (4).
- **Field 4, power mode.** Running (0), standby (1) and hibernate (2).
- **Field 5** flags an error and **field 6** flags data being saved.

The library's three confirmed payloads are the running whole-lawn mission, the
paused one and the running recharge mission. The map-saving payload is
saving the map without a mission. On the owned E15 the windows of 2026-09-16
to 2026-09-25 observed the following values. Where the app's display was
recorded at the time, it agreed:

- missions 1, 2 and 17, the last as Mowing… during a Box task;
- sub-missions 1 (Positioning…), 3 and 6 while leaving the dock, 5 (Saving the
  map) and 9 (Defogging…);
- states 1 and 2;
- power mode 2;
- the saving-data flag right after each map save.

Since 0.14.1 the local backend reads a running or paused mowing mission as
`mowing` or `paused` and the running recharge mission as `returning`. Saving
the map while running without a mission reads as `map_saving`. A payload
without a mission, sub-mission, state or error flag reads as `idle`,
whatever its power mode. The `robot_power_mode` attribute shows field 4.

### Hibernation, field 4 = 2

The cloud reported field 4 = 2 as the only record of DP 107 while the mower
stood docked with DP 1 false:

- on 2026-09-06 after an app selection in the Zone mode, see
  [zone-control-research.md](zone-control-research.md);
- on 2026-09-24 at 10:15 and 17:21 UTC. The integration's polls first read an
  unread payload between 10:02 and 10:07 UTC and between 13:51 and 13:56 UTC,
  five to fifteen minutes after a rest in the dock ended;
- on 2026-09-25 at 06:47 UTC, with an unread payload since the first daytime
  poll at 05:30 UTC.

When the rest in the dock began at 20:30 UTC on 2026-09-24 with DP 1 true, DP
107 was the default payload again. DP 152 carried a top-level field 5 = 1
alongside field 4 on 2026-09-06 and at 17:21 UTC on 2026-09-24, but not at
06:47 UTC on 2026-09-25. At 07:30 UTC on 2026-09-25, with field 4 = 2 in the
cloud and DP 1 false, the app showed its idle controls in the dock, as with the
default payload. The mission status schema names field 4 = 2 hibernate.

## Map identity

Map-record field 1 is the map id. Field 16 is the total area. The original
app's parser establishes both fields, as recorded in the library's
[map geometry](https://github.com/keesmod/eufy-mega-client/blob/ed6d3e2/docs/MAP_GEOMETRY.md).
Before integration 0.15.2 the renderer and live-coverage merge used field 16
as the id. Since 0.15.2 they use field 1. Equal areas must not make two maps
share their coverage, and a changed area must not reset coverage of the same
map. Tests use synthetic maps with deliberately different ids and areas.

## Current map-position interpretation

The E15 map record's field 8 and the latest clean-path position have been
repeatedly observed against the iOS app, but their dual live-session semantics
are not documented by Eufy or Tuya. The renderer therefore treats:

- map-record field 8 as the idle mower pose;
- the latest clean-path position as the live mower pose while mowing; and
- map-record field 8 as the charging-station marker during that live view.

This is an `observed` presentation rule, not a confirmed protocol contract. The
fixed SVG marker offset is visual alignment for the renderer's fixed-size icons
and is clamped to the canvas. None of these positions may be used for commands,
safety decisions, virtual-fence writes, or other physical control.

The library's decoder in 0.18.0 reads map-record field 8 as the station pose
and `navPath.bin.stream` as a pose, both with x in field 1, y in field 2 and
the heading in field 3, from the original parser's numbering. This
integration's parser reads fields 2 and 3 of both as x and y for the renderer.
The two readings differ, and which
one matches the app on the owned E15 is part of the native map acceptance in
issue #8. Until then both stay display only.

## Map exclusion geometry

Map-record field 11 holds the obstacles and field 12 the forbidden zones, the
no-go zones that the eufy app draws in red. The numbering is the library's
[map geometry](https://github.com/keesmod/eufy-mega-client/blob/main/docs/MAP_GEOMETRY.md),
read from the app's own parser and capture-validated for both fields. A
forbidden zone carries 1 id, 2 boundary, 3 isPolygon, 4 shape and 5 ellipse.
The boundary is a polygon whose points carry x in field 1 and y in field 2 as
sint32, like the lawn boundary. The shapes are rectangle 0, polygon 1 and
ellipse 2.

Integration 0.15.1 draws a rectangle or polygon zone from its boundary. It
skips an ellipse, an unknown shape and a boundary with fewer than three
distinct corners. The ellipse message holds a float rotation whose unit no
source states, and the decoder never reads it. Earlier versions drew the
obstacles under the name `no_go_areas`, which the snapshot keeps.

On 2026-09-25 the app showed one red no-go zone that Home Assistant did not
draw (#8). A read-only look at the owner's cached map bundle found one
forbidden zone with only an id and a boundary of four distinct corners, so its
shape is the default rectangle, and four obstacles of four points each. No
coordinate was recorded.

The app's product script can only delete physical forbidden zones (field 14)
and has no edit operation for obstacles, so the mower creates both. The
owner's map carries no physical forbidden zone. Its obstacles are drawn as the
dark polygons.

## Map pathways

Map-record field 18 holds the pathways, with 1 points and 2 id. A pathway is
the route the mower drives between separate lawns, as Eufy's
[multi-lawn article](https://service.eufy.com/article-description/How-many-maps-can-the-E15-and-E18-Robot-Lawn-Mowers-support-Are-they-capable-of-working-on-multiple-lawns)
describes. The app's product script can only delete them, so the mower records
them when a pathway is set up.

The owner's map of 2026-09-25 has one lawn region and two pathways. One runs
from the dock across the lawn edge, and the app draws it. The other never
leaves the lawn, and the app did not show it in two read-only comparisons that
day (#8). The app's Live button covers part of its map in those comparisons.
At the owner's request integration 0.15.3 draws a pathway only when at least
one of its points lies outside the lawn boundary, and then draws all of it. The snapshot keeps every pathway. The first renderer had the same rule
until #3 removed it without an app comparison. This is an `observed`
presentation rule, not a protocol contract.

## Map bundle through the bridge

Mower bridge 0.7.0 serves the map bundle this integration already validates
from the library's `PortableMapAcquisition` (keesmod/eufy-mega-client#50) and
its decoder `decodeMowerMapSnapshot` (keesmod/eufy-mega-client#51) in library
0.18.0. No new protocol fact enters this repository: the bundle carries the
three transport files as the library retained them, and the manifest,
snapshot digest and validation are this repository's existing contract. The
library's evidence levels apply unchanged. The three-file transfer has prior
standalone Linux research hardware proof (#49 in the library), the
acquisition adapter and the decoder are experimental software coverage and
the decoder's numbering is capture-validated. A fresh acquisition decoded end
to end on the owned E15, through the library or through the bridge, has not
run. The bridge tests use synthetic snapshots only.

## Settings through the bridge

Mower bridge 0.10.0 serves the typed settings of library 0.20.0
([settings contract](https://github.com/keesmod/eufy-mega-client/blob/main/docs/MOWER_SETTINGS.md)).
The library names every data point, type and bound from two permitted
sources: the owned E15's own declaration, read in a bounded read-only readout
on 2026-09-25, and the official app's product script, recorded in its
[settings schema receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_SETTINGS_SCHEMA_2026-09-25.md).
They are the data points this integration's local backend already reads:

| Entity | DP | Declared code | Bridge key | Written in bridge mode |
| --- | --- | --- | --- | --- |
| Cut Height | 110 | `mow_height`, 25 to 75 mm | `mow_height` | yes |
| Volume | 26 | `volume_set`, 0 to 100 % | `volume` | yes |
| Smart No-Go Suggestions | 132 | `enable_smart_forbid_zone` | `smart_no_go_zones` | yes |
| Mow Yellow Grass | 141 | `sparse_lawn_optimization` | `sparse_lawn_optimization` | yes |
| Stop on Rain Detection | 101 | `rain_auto_return` | `rain_auto_return` | never |
| Child Protection | 47 | `child_lock` | `child_lock` | never |
| Real Lawn Map | 133 | `enable_bird_view_capture` | `bird_view_capture` | never |

Bridge mode maps each key onto its data point, so the entities keep their
unique ids on both backends. Rain stop and child protection stay read only in
bridge mode in either direction, as the owner decided on 2026-09-25 in issue
#8, and so does the real lawn map, which the library leaves to the map work.
The local backend's switches are unchanged. Every bridge write is one request
that the library turns into one fresh query, one write and a read-back of the
written value, and it is never retried. The mapping of the app's labels to DP
141 and DP 133 is not established from the app's strings alone. On 2026-09-25
a supervised window in issue #8 changed the mow height from 40 to 45 mm and
back to 40 through integration 0.14.2 and bridge 0.10.1 on library 0.22.0,
with the mower in the dock. Both writes were confirmed, a later fresh query
reported each value and the app's Grass Height followed them, recorded in the
library's
[settings window receipt](https://github.com/keesmod/eufy-mega-client/blob/main/docs/research/E15_SETTINGS_WINDOW_2026-09-25.md).
A second window the same day changed and restored the volume, both switches
and the mow height at its bounds of 25 and 75 mm through the same versions.
Every write was confirmed within 80 to 103 milliseconds and a fresh query
reported each value
([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5832883255)).

## Work parameters through the bridge

Mower bridge 0.11.0 serves the DP 155 work parameters of library 0.23.0
([work parameter contract](https://github.com/keesmod/eufy-mega-client/blob/main/docs/MOWER_WORK_PARAMETERS.md)).
DP 155 is the official app's work parameter message. The library numbers its
fields from the app's product script, and reads it from the cloud record,
because the E15's LAN replies do not carry it. This integration's local
backend reads the same point through its own cloud client:

| Entity | DP 155 field | Bridge key | Written in bridge mode |
| --- | --- | --- | --- |
| Travel Speed | 2, mow speed | `mow_speed` | yes: slow, normal, fast as `low`, `medium`, `adaptive_high` |
| Blade Speed | 6, blade disk speed | `blade_speed` | yes: slow, normal, fast as `low`, `medium`, `high` |
| Edge Distance | 3, edge cutting distance | `edge_distance` | never |
| Path Distance | 5, mow spacing | `mow_spacing` | never |
| Pad Direction | 4, direction, single-mode angle | `direction.single_angle` | never |

Bridge mode maps the values onto the local backend's data keys, so the
entities keep their unique ids and show the same values on both backends.
A write is one request that the library turns into one cloud reading for the
value before it, one fresh LAN query, one partial message that carries only
the speed, and a read-back from fresh LAN reports. It is never retried. The
library has no permitted source for the bounds of the edge distance or the
mow spacing, and a direction write would replace a nested configuration, so
those three stay read only in bridge mode. The local backend still writes all
five through its cloud client, as before. On 2026-09-25 the owned E15 merged
a partial DP 155 message with only the blade speed, written over the LAN
outside the library, and reported the complete message within about 0.2
seconds ([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5832883255)). In a third window
the same day, integration 0.15.0 and bridge 0.11.0 on library 0.23.0 changed
Travel Speed from normal to fast and back and Blade Speed from normal to fast
and back through Home Assistant in bridge mode. Every write was confirmed from
a fresh report that kept the other fields, and later cloud readings matched
([receipt](https://github.com/keesmod/eufy-robomow-ha/issues/8#issuecomment-5833748272)).
