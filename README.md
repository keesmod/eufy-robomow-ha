# Eufy Robomow — Home Assistant Integration

A Home Assistant custom integration for the **Eufy E15** robotic lawn mower. The design is capability-driven for possible E18 support, but E18 support is not yet hardware-validated or claimed.

Control and monitor your Eufy mower directly from Home Assistant over your local network, with cloud-synced settings pulled straight from your Eufy account — no extra tools or manual key extraction required.

GitHub is the development source and issue tracker for this fork. Version 0.7.0
adds a dedicated dashboard card, observed session history, confirmed commands
and an optional Home Assistant planning package. Version 0.8.0 adds an optional
mower backend that reads state from the dedicated mower bridge. Version 0.8.1
corrects the Signal Strength sensor to the percentage the mower declares.
Version 0.9.0 routes start, pause and resume through the mower bridge in bridge mode.
Version 0.10.0 routes dock through the bridge's stop route on library 0.17.0.
Version 0.11.0 lets the map entity read the mower bridge's read-only map route
on library 0.18.0. Version 0.12.0 takes the mower activity in bridge mode from
the last confirmed command, so resume works through Home Assistant. Version
0.12.1 stops the local backend from reporting mowing while the mower rests in
the dock with its task flag set. Version 0.13.0 shows the drive home after a
dock in bridge mode. Version 0.13.2 shows the drive home in the local backend.
Version 0.13.3 confirms a start right after a map save and refuses a pause
without a running task. Version 0.13.4 confirms a start while the mower rests in
the dock and shows the drive home right after a dock.
Version 0.14.0 reads the local settings through the mower bridge and writes
cut height, volume and two lawn options through its opt-in settings route.
Version 0.14.1 reads DP 107 with the official app's mission status schema. Version 0.14.2 runs
with mower bridge 0.10.1 on library 0.22.0, which does the same in bridge mode.
Version 0.15.0 reads the DP 155 work parameters through mower bridge 0.11.0,
writes Travel Speed and Blade Speed through it and shows a confirmed setting at
once.

---

## Features

| Entity | Type | Description |
|--------|------|-------------|
| Mower | `lawn_mower` | Activity state; start, pause, and dock after control is explicitly enabled |
| Battery | `sensor` | Battery level (%) |
| Mowed Area | `sensor` | Area covered in the current or last session |
| Mowing Progress | `sensor` | Real-time session completion % (from DP113 telemetry blob) |
| Return Progress | `sensor` | DP 118, the map-save progress (%) at the dock arrival and after a Stop |
| Session Distance | `sensor` | Distance traveled in the current session (m) |
| Network | `sensor` | WiFi / Cellular connection type |
| Signal Strength | `sensor` | WiFi signal strength (%), the mower's own declared percentage |
| Cut Height | `number` | Blade height 25–75 mm, step 5 mm (local, instant) |
| Volume | `number` | Speaker volume 0–100 % (local) |
| Edge Distance | `number` | −15 to +15 cm — how far inside/outside the border wire the mower cuts |
| Pad Direction | `number` | Mowing path angle 0–359° |
| Travel Speed | `select` | Mower driving speed: slow / normal / fast |
| Blade Speed | `select` | Blade motor speed: slow / normal / fast |
| Path Distance | `select` | Lane spacing: 8 cm / 10 cm / 12 cm |
| Stop on Rain | `switch` | Pause mowing when rain is detected |
| Child Protection | `switch` | Enable child/pet protection mode |
| Smart No-Go Suggestions | `switch` | AI-assisted no-go zone suggestions |
| Mow Yellow Grass | `switch` | Allow mowing on dry/yellow grass |
| Map | `image` | Optional read-only E15 boundary, areas, pathways, mower and cleaning path |

> **Cloud entities** (edge distance, pad direction, speeds, path distance) require your Eufy account credentials. They are polled every 5 minutes and written back via the Tuya mobile API.
>
> Some entities (generic raw DP sensors, map coverage) are **disabled by default** — enable them in HA if you want to explore unconfirmed data points.

### Map preview

<p align="center">
  <img src="docs/map-preview.svg" alt="Synthetic Eufy E15 map preview showing the boundary, live mowing coverage, mower, charging station, pathway and no-go area" width="480">
</p>

The map image follows the live mowing session with two-second, change-aware
updates. It shows the mapped boundary, charging area, external pathways, no-go
areas, completed mowing lanes and the latest mower position. The preview above
uses synthetic geometry; no private lawn map or device data is stored in this
repository.

---

## Prerequisites

- **Local network access** — the mower and Home Assistant must be on the same LAN (or the mower reachable via IP).
- **Eufy account** — required for cloud-managed settings. The same email/password you use in the Eufy Home app.
- Home Assistant **2026.7** or newer during private alpha development.

---

## Installation

### Private development installation

1. Copy the `custom_components/eufy_robomow/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

HACS packaging is intentionally deferred until the private integration has passed protocol, safety, and reliability validation.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration → Eufy Robomow**.

**Step 1 — Sign in:**
Enter your Eufy account email and password. The integration will automatically discover all your devices and fetch their local keys.

**Step 2 — Select mower:**
Pick your mower from the dropdown and enter its local IP address (find it in your router's DHCP table or the Eufy app's device info screen).

That's it — no external tools, no manual key extraction.

New entries start in **observe-only** mode. Entries upgraded from the original
integration also start in observe-only when they do not yet have an explicit
operating mode. In this mode the mower entity reports state, but physical
commands and settings-write entities are disabled. After supervised read-only
validation, use **Settings → Devices & Services → Eufy Robomow → Configure** to
opt in to control.

> **Alpha credential notice:** the current cloud client still stores the Eufy account password in the Home Assistant config entry so it can renew sessions. Restrict access to Home Assistant backups and `.storage`; replacing this with renewable session material is tracked as a separate hardening change.

### Mower backend

**Settings → Devices & Services → Eufy Robomow → Configure** offers a
**Mower backend** choice. Exactly one backend owns the mower.

- **local** (default, unchanged): this integration polls the mower over the
  Tuya local protocol and, with account credentials, polls cloud settings.
  Existing entries keep this backend until you change it.

  One local status shape is ambiguous: DP 1 true, DP 2 false and DP 118 at
  100. DP 118 is map-save progress and stays at 100 after a map save, so a
  later task mows with it. On 2026-09-24 the same shape also held for about
  fifteen minutes after each dock arrival and each evening while the mower
  rested in the dock and the app showed it idle or charging. A local status
  reply never carries DP 107, so in this shape the integration asks the cloud
  at once and then every minute, also at night. A DP 107 payload without a
  mission, such as the default payload or hibernation, or the map-saving
  payload while the map is saved at the arrival before DP 1 turns false, then
  reports `docked`. A paused mowing mission and the running recharge mission
  report `paused` and `returning`, and anything else keeps `mowing`. Without
  account credentials or a cloud answer the reading stays `mowing`. The
  `robot_status` attribute shows DP 107 as the last cloud poll read it, and
  `robot_power_mode` its power mode: `running`, `standby` or `hibernate`.

  The local status does not show the drive home either. After a stop or at
  the end of a task DP 1 turns false while the mower drives to the dock, 25 to
  60 seconds on the owned E15, and at the arrival DP 1 is true again for about
  eighteen seconds while the map is saved and DP 118 rises from 1 to 100. When
  DP 1 turns false after a mowing, paused or returning reading, the
  integration asks the cloud at once and at every local poll for two minutes,
  also at night, and after that every minute while DP 107 still reads
  returning. The confirmed `returning` payload then reports `returning`. The
  map-saving payload reports `docked` while the map is saved, which the
  arrival and every map save ask the cloud for once. After the app's Stop the
  mower saves the map where it stands on the lawn and reads `docked` as well,
  as it did right afterwards before, because Home Assistant has no activity
  for a mower standing on the lawn. Only a cloud poll taken since the local
  status took its shape decides, and without one each shape keeps its earlier
  reading: `docked` with DP 1 false and `returning` while DP 118 is between 5
  and 99.
- **bridge**: the integration reads state from the dedicated mower bridge
  described below and creates no local connection and no cloud client of its
  own. Enter the bridge URL (`http` or `https`, for example
  `http://127.0.0.1:8090`), its bearer token, and optionally the mower id and
  a certificate fingerprint for a private TLS certificate. Saving validates the
  token against the bridge and fills in the mower id when the bridge discovers
  exactly one mower.

In bridge mode the mower entity, battery, network and signal sensors read the
bridge's typed state every ten seconds. `telemetry_updated_at` is the bridge's
observation time, not the poll time. When the bridge reports stale data, an
error or is unreachable, the entities become unavailable, exactly as after a
failed local poll. Nothing is carried forward.

Bridge mode routes start, pause, resume and dock through the bridge's opt-in
command routes when two opt-ins meet: this integration's operating mode is `control`
and the bridge itself runs in `control` mode, which it reports as
`routes.control` in its state. The integration reads that state on every poll,
so the mower entity exposes start, pause and dock only while both hold and
exposes nothing in `observe_only`, where every write still raises before any
transport. Dock goes through the bridge's `stop` route: on the owned E15
firmware a stop over DP 1 false ends the task and the mower returns to the
dock by itself, while the library's return over DP 3 is ignored from paused
and from the stopped task, so there is no return route and no stop in place.
A confirmed dock carries the evidence `bridge:map_saving`, the map-saving
payload the library received at dock arrival about 30 seconds after the write,
see the library's
[stop and return receipt](https://github.com/keesmod/eufy-mega-client/blob/19d47a7144e505702e1ef98dfc7d84dfeb956cd6/docs/research/E15_STOP_RETURN_WINDOW_2026-09-20.md).
Since 0.14.0 the local settings exist in bridge mode too, with the same unique
ids, in this integration's `control` mode: Cut Height and Volume as numbers and
the five switches, read from the `settings` of the bridge's state document
(bridge 0.10.0 or later). Cut Height, Volume, Smart No-Go Suggestions and Mow
Yellow Grass are written through the bridge's settings route when two opt-ins
meet: this integration's `control` mode and the bridge's own
`settings_mode: write`, which it reports as `routes.settings`. Each change is
one request, the library behind the bridge reads the setting fresh, refuses
what it cannot prove, writes once and confirms only a fresh report of the new
value. Nothing is retried, and a change back is a second deliberate write.
Stop on Rain Detection, Child Protection and Real Lawn Map stay read only in
bridge mode: turning them on or off raises an error before any request, change
them in the Eufy app. Since 0.15.0 the cloud settings (Edge Distance, Pad
Direction, Path Distance, Travel Speed and Blade Speed) exist in bridge mode
too, with the same unique ids, read from the `work_parameters` of the bridge's
state document (bridge 0.11.0 or later). The bridge takes them from a cloud
reading at most every five minutes, or from the report that confirmed a write.
Travel Speed and Blade Speed are written through the bridge's settings route
under the same two opt-ins, once each. Edge Distance, Pad Direction and Path
Distance are read only in bridge mode. The map entity has its own source choice, the bridge's map
route or the external map source, see [Optional read-only map](#optional-read-only-map).

Each command is one `POST` to the bridge, sent exactly once, and the bridge's
answer is the confirmation. The `command` attribute goes `sending`, `pending`
and then `confirmed` with evidence `bridge:<activity>` when a fresh report
reflected the expected activity, `rejected` with `bridge:<end>` when the mower
refused the frame, or `uncertain` with `bridge:<end>` when the bridge's
read-back window passed without a report. An uncertain command was written and
must not be repeated blindly. A refusal the bridge or the library raises
before any write, for example `telemetry_stale`, `command_in_progress` or a
`mower_command_*` code, ends as `failed` with that code as evidence and wrote
nothing. A timeout or a lost connection during the request ends as `uncertain`
because the write may have happened. A pause still supersedes a pending start
and waits for the in-flight answer instead of being refused, which is not a
retry. Nothing is replayed on reload, on a backend switch or after a bridge
reconnect, a new coordinator starts without a command. The `bridge_control`
attribute shows the opt-in the bridge reports, its classes and time bounds.

Activity comes from the library's typed status. With bridge 0.7.0 on library
0.18.0 the mower entity reports mowing, paused and returning from the confirmed
DP 107 payloads, with `telemetry_updated_at` as the observation time. The
`bridge_status` attribute shows the library's status state: `reported`,
`missing` when the query carried no DP 107, `invalid` for a withheld payload,
or `unconfirmed`. A missing or invalid status leaves the activity unknown. On
the owned E15 a state query carries no DP 107, so in the 2026-09-24 control
window every poll reported `missing` (issue #8).

Since 0.12.0 a confirmed command stands in for it. Its answer carries the fresh
DP 107 report that reflected it and the time the library received that report:
`mowing` after start or resume, `paused` after pause, and `docked` after a
confirmed dock, whose map-saving payload is the dock arrival the library
observed. With bridge 0.9.0 on library 0.19.0 a poll during a running command
also carries the command's latest confirmed activity, so the entity shows
`returning` within about ten seconds of a dock instead of the earlier
activity. An uncertain answer keeps a report the command itself produced and
clears only older evidence. The entity shows that activity for at most 30 minutes, because the
mower can change by itself afterwards, for example through its app schedule. A
newer reported poll replaces it, and an uncertain command or a
`mower_command_already_set` refusal clears it, because the mower's state is
then unknown or contradicts it. So start while paused sends resume, and a
resume refused because the mower was resumed elsewhere makes the next start a
start. `bridge_activity_source` (`report` or `command`) and
`bridge_activity_observed_at` show where the activity comes from.
`bridge_activity` stays the polled value. No E15 payload identifies charging,
idle or error, and nothing is inferred from age, absence or inactivity. Mowing
progress stays unconfirmed. See
[DP 107 activity](docs/protocol-provenance.md#dp-107-activity).

Session history in bridge mode observes a reported activity and the activity a
confirmed command reflected, never a missing, invalid or unconfirmed status and
never age or absence. A confirmed dock ends the session. Between commands the
polls observe nothing, so the time in between counts as an observation gap and
area, distance and progress stay unknown. Entity unique IDs are unchanged, so
switching back and forth never duplicates or renames entities and no command
is replayed on a switch.

### Mower dashboard and session history

1. Install the integration and restart Home Assistant.
2. Add `/eufy_robomow/eufy-mower-card.js?v=0.7.1` as a JavaScript module under
   **Settings → Dashboards → Resources** (advanced mode may be needed).
3. Create a dashboard, enable its sidebar entry, and use
   [`examples/dashboard.yaml`](examples/dashboard.yaml). Replace entity IDs with
   those from your installation. The example uses four native Home Assistant tabs:
   mower, history, planning and settings. Cards follow the active HA theme, like
   the Eufy Viewer dashboard. The card also works in an existing dashboard.

Set `view` to `overview`, `history`, `planning` or `settings` to show one section.
Omitting it keeps the combined view for existing cards. Frontend version 0.7.1
changes presentation only; it does not add zone commands or enable planning.
Frontend version 0.7.2 offers pause only while the mower mows. Frontend version 0.7.3 labels
the cloud confirmation of a start.

The card supports map zoom/pan, battery and session telemetry, settings, and
start/resume, pause and return commands. It shows pending, confirmed, failed and
uncertain results. Start requires confirmation; unavailable or stale telemetry
and observe-only mode disable controls. An inactive-task response to Return is
not proof of physical arrival at the dock.
Pause is offered only while mowing: during the drive home the E15 ignores a
pause, so the local backend refuses one while the task flag is false.

Fifty observed session summaries are stored privately in Home Assistant; the
card shows the latest twenty. Pauses and telemetry gaps remain visible, and a
restart does not invent missing mowing time. Area remains in raw units until the
scale is validated. Existing lifetime counters are not reconstructed as sessions.
One of the map sources described below is still required for map display.

### Optional rain-aware planning

[`examples/eufy_mower_planning.yaml`](examples/eufy_mower_planning.yaml) is an
opt-in Home Assistant package. Replace every `example_*` source and mower entity
with your own. It expects the documented Buienalarm precipitation-array shape
and separate irrigation valve, active-session, planned-session and start-time
entities. Missing or stale sources block automatic starts.

The package offers weekday and time-window selection, minimum battery, dry hold
and maximum session duration. It checks radar coverage for the whole planned
session, irrigation conflicts, daylight and onboard rain/child protection. It
allows at most one start attempt per day. Its watchdog handles only sessions it
started, issuing one pause or return request without automatic retries/resume.
Automatic mowing initially stays off; configure and supervise validation before
enabling it. Eufy-app schedules run independently and must be considered separately.

### Optional read-only map

E15 maps use a separate Tuya P2P media transport that is not available through
the mower's normal local DPS connection. This integration can consume a
compatible map source without bundling that transport or its Android-only
vendor libraries.

**Settings → Devices & Services → Eufy Robomow → Configure** offers a **Map
source** choice. Exactly one source feeds the map entity:

- **external** (default, unchanged): a compatible HTTPS map source such as the
  existing Android map helper, configured by the URL below. Existing entries
  keep it until you change it, and it stays the manual recovery path.
- **bridge**: the read-only map route of the dedicated mower bridge, described
  below. It needs the **bridge** mower backend and a bridge that serves maps.

The external source is configured with:

- **Map source HTTPS URL** — base URL of the map source.
- **Certificate SHA-256 fingerprint** — optional pin for private or self-signed
  TLS. Normal certificate validation is used when this is empty.

Authentication is derived from the mower's existing local key; no additional
token is stored. The source must expose `GET /v1/map` with content type
`application/vnd.eufy-robomow-map+zip`, support `ETag` responses, and use the
`X-Eufy-Map-Mode` request header (`idle` or `stream`) to control its read-only
P2P session. The stored ZIP contains a manifest and exactly these three files:

- `map.bin.stream`
- `cleanPath.bin.stream`
- `navPath.bin.stream`

Home Assistant validates the archive, device binding, sizes, hashes, protobuf
and boundary before displaying it. One latest-good bundle is stored privately
under `/config/eufy_robomow_maps/`. If acquisition fails, the previous valid map
remains available. Idle maps refresh every five minutes. During an active mowing
task, Home Assistant requests changed stream snapshots every two seconds,
accumulates and deduplicates coverage deltas, and renders the newest mower pose.
With the external source, clear the source URL to remove the map entity; no
mower setting or geometry is changed.

With the **bridge** map source the entity reads
`GET /v1/mowers/{id}/map` of the configured bridge with the bridge's URL, token
and optional certificate pin, and derives no token from the local key. The
bundle and its validation are the same, but it is bound to the bridge's mower
id instead of the device id and kept in its own latest-good file,
`bridge.mapbundle`, next to the external source's `latest.mapbundle`, so
switching the source never mixes or discards the other one's map. The bridge
answers from memory and paces its own acquisitions, so Home Assistant checks
an idle map every minute with `ETag` and a live map every two seconds while
the bridge reports mowing, paused or returning. When the bridge serves its
last good map after a failed acquisition, `acquisition_status` becomes
`stale` and `acquisition_last_error` names the bridge's code, and a refused
request names it too, for example `map_provisioning_unreadable`. The image
entity, its unique id, the session history and the dashboard stay the same
for both sources. Choosing the bridge source is refused unless the bridge
reports `routes.maps`, which needs its operator-supplied map provisioning,
see the [deployment guide](docs/bridge-deployment.md#map-provisioning-optional).
The external URL stays stored, switch back to **external** to recover the
previous source.

Map bundles contain private lawn geometry. Never commit them, attach them to an
issue or include them in diagnostics.

---

## Mower bridge (foundation)

[`bridge/`](bridge/README.md) contains the dedicated Node 24 mower bridge that
later steps connect to this integration. It consumes
[`@keesmod/eufy-mega-client`](https://github.com/keesmod/eufy-mega-client)
0.22.0 as a library, pinned to the exact release tarball, and instantiates only
the library's mower module. It has its own token, credentials, session file,
data directory, port and lifecycle, and runs with no camera bridge present.

Version 0.7.0 validates its configuration, keeps one private session, makes one
explicit authentication attempt without retries and stops cleanly on `SIGTERM`.
Its read-only routes are `GET /v1/state`, `GET /v1/mowers` for the discovered
E15 mowers and `GET /v1/mowers/{id}/state` for one typed local query over the
LAN with explicit freshness and stale last-good results. Battery, network,
signal and the E15 activities mowing, paused and returning are confirmed in
the library today, see [DP 107 activity](docs/protocol-provenance.md#dp-107-activity).
No E15 payload identifies docked, charging, idle or error and mowing progress
stays unconfirmed. It starts in `observe_only` by default, where every command
route answers `403`. With `operating_mode: control` and a required stop route
it adds `POST /v1/mowers/{id}/commands/{start|pause|resume|stop}`, each checked on
the bridge for mode, class, mower, host, exclusive ownership and the age of the
last state observation, and served as the library's confirmed, failed or
uncertain outcome without retry or replay. `return` stays unsupported, the
owned firmware ignores its DP 3 write from paused and from the stopped task,
and `stop` is the route that returns the mower to the dock. Its state route
serves the typed settings, and with the separate `settings_mode: write` it adds
`POST /v1/mowers/{id}/settings/{key}` for mow height, volume, smart no-go zones
and sparse lawn optimization, with rain and child protection read only.
With an operator-supplied map provisioning file it adds the read-only
`GET /v1/mowers/{id}/map`, which serves the map bundle described above from
the library's portable map acquisition after the library's decoder accepted
the snapshot, with `ETag`, an explicit age and the last good bundle after a
failed acquisition. No live map has been acquired through the bridge yet,
that map acceptance is issue #8. The Python integration consumes it through the optional **bridge**
mower backend described above, for state and, behind both control opt-ins, for
start, pause, resume and dock, and through the **bridge** map source for the
map. It ships as a reproducible container image and as a
local Home Assistant app candidate with a health check, see the
[deployment guide](docs/bridge-deployment.md). The follow-up order is recorded
in [issue #12](https://github.com/keesmod/eufy-robomow-ha/issues/12). See
[ADR 0003](docs/architecture/0003-dedicated-mower-bridge.md).

---

## How it works

- **Local polling** (every 10 s) via the [Tuya local protocol](https://github.com/jasonacox/tinytuya) for real-time status (battery, activity state, etc.). With the **bridge** backend the mower bridge polls the mower instead and Home Assistant reads its typed state every 10 s over an authenticated private HTTP connection.
- **Cloud polling** (every 5 min) via the Tuya mobile API for settings stored as protobuf blobs in DP155.
- **Writes**, when control is explicitly enabled, go to either the local mower or the cloud API depending on the setting. With the **bridge** backend start, pause, resume and dock go through the bridge's command routes instead, once each, dock through the bridge's stop route, and cut height, volume, smart no-go suggestions and mow yellow grass through its settings route, once each. Rain stop, child protection, the real lawn map, edge distance, pad direction and path distance have no bridge write, and travel and blade speed go through the settings route too.
- **Optional map acquisition** uses five-minute idle snapshots and two-second
  `ETag`-aware live pulls while mowing, then renders the validated geometry
  locally as a script-free SVG. With the **bridge** map source the bridge
  acquires through the library and Home Assistant checks it every minute when
  idle and every two seconds while a task runs.

Runtime dependencies are pinned to the versions validated with Home Assistant
2026.7.1 and the E15's Tuya 3.5 transport. `requests` is declared directly;
TinyTuya remains pinned to 1.20.0 until another version passes the same local
protocol tests.

---

## Upgrade notes

[CHANGELOG.md](CHANGELOG.md) lists every integration version with its
evidence, upgrade and rollback, and [bridge/CHANGELOG.md](bridge/CHANGELOG.md)
does the same for the mower bridge and its app. The notes below cover what an
upgrade changes for an existing installation.
[Release candidates](docs/release-candidates.md) describes how a candidate is
built and verified and what is confirmed on hardware.
[Migration and rollback](docs/migration-and-rollback.md) covers the switch
between the backends, rollback and the later retirement of the Android map
helper.

- **0.15.0, work parameters through the bridge, and bridge 0.11.0.** With
  mower bridge 0.11.0 on library 0.23.0, bridge mode creates Edge Distance,
  Pad Direction, Path Distance, Travel Speed and Blade Speed with the unique
  ids of the local backend. Travel Speed and Blade Speed are written through
  the bridge's settings route, the other three are read only there. A setting
  confirmed through the bridge now shows its new value at once instead of
  after the debounced refresh. Update the app with a backup first, then the
  integration.
- **0.14.2, bridge 0.10.1 on library 0.22.0.** Mower bridge 0.10.1 pins
  library 0.22.0, which reads DP 107 as the mower's mission status like the
  local backend since 0.14.1. In bridge mode a Box, zone or scheduled task now
  reports `mowing` or `paused` instead of an invalid status, and a message
  without a mission reports `idle`, which shows as `docked` and ends the
  session. The integration's code is unchanged. Update the app with a backup.
- **0.14.1, DP 107 read with the app's mission status schema.** The official
  app decodes DP 107 as the mower's mission status. Its fields are the mission,
  sub-mission, state, power mode and an error flag, and a flag for saving data.
  The local backend now reads every mowing mission, such as the Box task
  (mission 17) and scheduled or zone tasks, as `mowing` or `paused`. A payload
  without a mission reads as `idle`, whatever its power mode, and
  hibernation (field 4 = 2) shows in the new `robot_power_mode` attribute.
  A start in Box mode while the mower rests in the dock is now confirmed
  from the cloud as well. Bridge mode is unchanged.
- **0.14.0, settings through the bridge, and bridge 0.10.0.** With mower
  bridge 0.10.0 on library 0.20.0 the bridge backend reads Cut Height, Volume
  and the five local switches from the bridge's state document, with the same
  unique ids as the local backend, and writes Cut Height, Volume, Smart No-Go
  Suggestions and Mow Yellow Grass through the bridge's settings route once
  the bridge runs with `settings_mode: write`. Rain stop, child protection and
  the real lawn map stay read only in bridge mode. With an older bridge the
  setting entities in bridge mode have no value. The local backend is
  unchanged, including its switches for rain stop and child protection.
- **0.13.4, start while resting in the dock and the first poll of the drive
  home.** About five minutes after each arrival the mower rests in the dock
  with DP 1 true and DP 118 at 100. A start then works, but it leaves the
  local status unchanged, so 0.13.3 reported a timeout and showed `docked`
  until it. In that shape a pending start or resume now asks the cloud at
  every local poll, and a fresh confirmed `mowing` payload confirms it with
  evidence `cloud_mowing_reported`. This is the only command the cloud
  confirms. The poll that confirms a dock now also asks for the drive home,
  so `returning` shows at once instead of ten seconds later. A command
  confirmed during a refresh that then outlasts the 35-second bound stays
  confirmed. Update the dashboard resource to
  `/eufy_robomow/eufy-mower-card.js?v=0.7.3`.
- **0.13.3, start after a map save and pause during the drive home.** DP 118
  stays at 100 after a map save, so a start from the dock shortly after an
  arrival never showed DP 118 at 0, and the local backend reported a timeout
  although the mower started. A start is now also confirmed by DP 1 turning
  true, with evidence `task_started`, and a resume by DP 2 turning false,
  with evidence `pause_cleared`. During the drive home the E15 ignores a
  pause. The local backend refuses one while the task flag DP 1 is false, before
  any write, and the card offers pause only while mowing. Update the
  dashboard resource to `/eufy_robomow/eufy-mower-card.js?v=0.7.2`.
- **0.13.2, the drive home in the local backend.** With account credentials
  the local backend reports `returning` while the mower drives to the dock
  after a stop or at the end of a task, where it reported `docked`, and
  `docked` while the map is saved at the arrival, where it reported
  `returning` and sometimes `mowing` for one poll. Automations that wait for
  `docked` after a stop now see it at the arrival instead of at the stop. The
  cloud is asked at every local poll for up to two minutes after DP 1 turns
  false and once per map save, about five to ten requests for each drive
  home. Bridge mode is unchanged.
- **0.13.1, the map save at the arrival is docked.** In the local backend a
  status poll that catches the ten to twenty seconds after a dock arrival,
  while DP 1 is still true and the map is saved, reported `mowing`, as seen
  at 13:23 UTC on 2026-09-24. With the map-saving DP 107 payload from the
  cloud it now reports `docked`. The `robot_status` attribute can read
  `map_saving`.
- **0.13.0, the drive home after a dock, and bridge 0.9.0.** With mower bridge
  0.9.0 on library 0.19.0, a state poll during a running command carries the
  command's latest confirmed activity. In bridge mode the entity therefore shows
  `returning` during the drive home after a dock, where 0.12.1 kept showing
  `mowing` until the arrival. An uncertain answer keeps a report the command
  itself produced. An older bridge simply sends no progress. The local backend
  is unchanged.
- **0.12.1, resting in the dock is docked.** With account credentials the
  local backend reports `docked` instead of `mowing` while the mower rests in
  the dock with its task flag set, which on 2026-09-24 happened for about
  fifteen minutes after every dock arrival and every evening. Automations that
  wait for `docked` now see it at the dock arrival instead of a quarter of an
  hour later, and automations that watch `mowing` no longer act on a mower in
  the dock. The cloud is asked every minute while that status shape lasts.
- **0.12.0, activity from confirmed commands, and bridge 0.8.0.** In bridge
  mode the mower entity shows the activity the last confirmed command
  reflected, for at most 30 minutes, so start while paused sends resume and a
  confirmed dock shows docked and ends the session. The live map follows the
  same activity. `mower_binding_unavailable` counts as refused before the
  write. Mower bridge 0.8.0 renews its hourly cloud session by itself, where
  0.7.1 answered `authentication_required` about an hour after its sign-in
  until a restart. The local backend is unchanged. Upgrading from 0.8.0 or
  earlier changes the Signal Strength unit from dBm to %, and Home Assistant
  then suppresses its long-term statistics until they are fixed under
  **Developer tools → Statistics**. Those values were the negated percentage,
  so they can be kept with the sign restored.
- **0.11.0, the map through the bridge.** With mower bridge 0.7.0 on library
  0.18.0 the options gain **Map source**, `external` by default, so existing
  entries keep their map unchanged. `bridge` points the same map entity at the
  bridge's read-only map route in bridge mode, with its own latest-good file
  and the bridge's token. In bridge mode the live map now follows the
  bridge's reported mowing, paused or returning activity, where it previously
  never switched to live pulls because bridge mode has no DP 1. A failed or
  refused request names the source's error code in `acquisition_last_error`.
  The external source, the local backend and the Android map source are
  unchanged. No live map has been acquired through the bridge, that map
  acceptance is issue #8.
- **0.10.0, dock through the bridge.** With mower bridge 0.6.0 on library
  0.17.0 the bridge backend exposes dock next to start and pause in control
  mode and routes it through the bridge's `stop` class. On the owned E15
  firmware a stop over DP 1 false ends the task and the mower returns to the
  dock by itself, the library's return over DP 3 is ignored, so dock is the
  only way home through the bridge and there is no stop in place. A confirmed
  dock carries the evidence `bridge:map_saving`, the dock arrival the library
  observed, an uncertain dock was written and is never repeated by the
  integration. The local backend is unchanged. No live test has run against
  the mower in bridge control mode, that acceptance is issue #8.
- **0.9.0, bridge commands.** With mower bridge 0.5.0 on library 0.16.0 the
  bridge backend routes start, pause and resume through the bridge's opt-in
  command routes when both this integration and the bridge run in `control`
  mode. The mower entity then exposes start and pause, never dock, because the
  bridge has no return route for the owned firmware. Settings entities stay
  absent in bridge mode. The `command` attribute gains the `uncertain` state
  for a written but unconfirmed command and `bridge:` evidence values, and the
  mower entity gains the `bridge_control` attribute. Session history now grows
  in bridge mode from reported activities and cannot observe a session's end
  yet. The local backend is unchanged. No live test has run against the mower
  in bridge control mode, that acceptance is issue #8.
- **0.8.2, bridge activity.** With mower bridge 0.4.0 on library 0.15.0 the
  bridge backend reports mowing, paused and returning from the confirmed E15
  payloads and adds the `bridge_status` attribute. Docked, charging, idle and
  error have no confirmed payload, so the entity stays unknown between tasks
  in bridge mode. The local backend is unchanged.
- **0.8.1, Signal Strength unit.** DP 109 is declared by the mower as a
  percentage from 0 to 100. Earlier versions negated the value and labelled it
  dBm without evidence. The sensor keeps its entity id and now reports the
  percentage. Home Assistant detects the changed unit of the sensor's
  long-term statistics and offers a repair to update or clear the old
  statistics. Automations that compared the value against negative dBm
  thresholds need the percentage instead.

## Known limitations

- **Zone mowing** — the owned E15 app shows Entire, Zone, Box and Spot, but area identifiers and command transport have not been validated. No zone action is exposed; see [zone research](docs/zone-control-research.md).
- **Map acquisition** — experimental. The external source needs a separate compatible source, because Tuya publishes the required P2P transport only through its Android media stack. The bridge source uses the library's portable acquisition, which needs private provisioning that the operator supplies and keeps fresh. Neither has passed native map acceptance yet, see issue #8.
- **Live marker semantics** — the live mower/station interpretation matches repeated E15 observations but is not a vendor-documented protocol contract. It is display-only and never drives mower control.

---

## Troubleshooting

- **Entities unavailable** — check that the IP address is correct and the mower is on WiFi (not cellular only).
- **Cloud settings not updating** — cloud data refreshes every 5 minutes; changes made in the Eufy app will appear after the next refresh cycle.
- Enable **debug logging** for detailed output:

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.eufy_robomow: debug
```

---

## Credits

Authentication and local-key discovery based on [eufy-clean-local-key-grabber](https://github.com/albaintor/eufy-clean-local-key-grabber).
Local protocol via [tinytuya](https://github.com/jasonacox/tinytuya).
