# Mower backends

The integration has two mower backends and exactly one of them owns the mower.
Choose it under **Settings → Devices & Services → Eufy Robomow → Configure →
Mower backend**. Entity unique ids are the same for both backends, so switching
back and forth never duplicates or renames entities, and no command is replayed
on a switch. The switch itself and its rollback are described in
[Migration and rollback](migration-and-rollback.md).

| | `local` (default) | `bridge` |
| --- | --- | --- |
| Reads the mower | Home Assistant over the Tuya local protocol, every 10 s | the [mower bridge](../bridge/README.md), whose typed state Home Assistant reads every 10 s |
| Cloud access | Home Assistant's own cloud client, settings every 5 min | none from Home Assistant, the bridge reads the cloud |
| Commands in `control` mode | start, pause, resume and dock | start, pause, resume and dock, also behind the bridge's own `control` opt-in |
| Setting writes in `control` mode | every setting entity | Cut Height, Volume, Smart No-Go Suggestions, Mow Yellow Grass, Travel Speed and Blade Speed, behind the bridge's `settings_mode: write` |
| Session distance, area and progress | from the local telemetry | unknown |

Existing entries keep the `local` backend until you change it. In
`observe_only` mode neither backend exposes a command or a setting write.

## Local backend

The integration polls the mower over the Tuya local protocol and, with account
credentials, polls the cloud settings stored in DP 155.

### Activity

One local status shape is ambiguous: DP 1 true, DP 2 false and DP 118 at 100.
DP 118 is map-save progress and stays at 100 after a map save, so a later task
mows with it. On 2026-09-24 the same shape also held for about fifteen minutes
after each dock arrival and each evening while the mower rested in the dock and
the app showed it idle or charging. A local status reply never carries DP 107,
so in this shape the integration asks the cloud at once and then every minute,
also at night. A DP 107 payload without a mission, such as the default payload
or hibernation, or the map-saving payload while the map is saved at the arrival
before DP 1 turns false, then reports `docked`. A paused mowing mission and the
running recharge mission report `paused` and `returning`, and anything else
keeps `mowing`. Without account credentials or a cloud answer the reading stays
`mowing`. The `robot_status` attribute shows DP 107 as the last cloud poll read
it, and `robot_power_mode` its power mode: `running`, `standby` or `hibernate`.

The local status does not show the drive home either. After a stop or at the
end of a task DP 1 turns false while the mower drives to the dock, 25 to 60
seconds on the owned E15. At the arrival DP 1 is true again for about eighteen
seconds while the map is saved and DP 118 rises from 1 to 100. When DP 1 turns
false after a mowing, paused or returning reading, the integration asks the
cloud at once and at every local poll for two minutes, also at night, and after
that every minute while DP 107 still reads returning. The confirmed `returning`
payload then reports `returning`. The map-saving payload reports `docked` while
the map is saved, which the arrival and every map save ask the cloud for once.
After the app's Stop the mower saves the map where it stands on the lawn and
reads `docked` as well, because Home Assistant has no activity for a mower
standing on the lawn. Only a cloud poll taken since the local status took its
shape decides. Without one each shape keeps its earlier reading: `docked` with
DP 1 false and `returning` while DP 118 is between 5 and 99.

### Commands

A start is confirmed when DP 118 drops to 0 or DP 1 turns true, the latter with
evidence `task_started`. A resume is confirmed when DP 2 turns false, with
evidence `pause_cleared`. In the resting shape above a start leaves the local
status unchanged, so a pending start or resume asks the cloud at every local
poll, and a fresh confirmed `mowing` payload confirms it with evidence
`cloud_mowing_reported`. This is the only command the cloud confirms. During
the drive home the E15 ignores a pause, so the local backend refuses one while
DP 1 is false, before any write.

## Bridge backend

The integration reads state from the dedicated
[mower bridge](../bridge/README.md) and creates no local connection and no
cloud client of its own. Enter the bridge URL (`http` or `https`, for example
`http://127.0.0.1:8090`), its bearer token, and optionally the mower id and a
certificate fingerprint for a private TLS certificate. Saving validates the
token against the bridge and fills in the mower id when the bridge discovers
exactly one mower. Installing the bridge is covered in the
[deployment guide](bridge-deployment.md).

### State

The mower entity, battery, network and signal sensors read the bridge's typed
state every ten seconds. `telemetry_updated_at` is the bridge's observation
time, not the poll time. When the bridge reports stale data or an error, or is
unreachable, the entities become unavailable, exactly as after a failed local
poll. Nothing is carried forward.

### Commands

Start, pause, resume and dock go through the bridge's opt-in command routes
when two opt-ins meet: this integration's operating mode is `control` and the
bridge itself runs in `control` mode, which it reports as `routes.control` in
its state. The integration reads that state on every poll, so the mower entity
exposes start, pause and dock only while both hold. It exposes nothing in
`observe_only`, where every write still raises before any transport.

Dock goes through the bridge's `stop` route. On the owned E15 firmware a stop
over DP 1 false ends the task and the mower returns to the dock by itself,
while the library's return over DP 3 is ignored from paused and from the
stopped task. So there is no return route and no stop in place. A confirmed
dock carries the evidence `bridge:map_saving`, the map-saving payload the
library received at dock arrival about 30 seconds after the write, see the
library's
[stop and return receipt](https://github.com/keesmod/eufy-mega-client/blob/19d47a7144e505702e1ef98dfc7d84dfeb956cd6/docs/research/E15_STOP_RETURN_WINDOW_2026-09-20.md).

Each command is one `POST` to the bridge, sent exactly once, and the bridge's
answer is the confirmation. The `command` attribute goes `sending`, `pending`
and then:

- `confirmed` with evidence `bridge:<activity>` when a fresh report reflected
  the expected activity,
- `rejected` with `bridge:<end>` when the mower refused the frame,
- `uncertain` with `bridge:<end>` when the bridge's read-back window passed
  without a report. A timeout or a lost connection during the request also
  ends as `uncertain`. The write may have happened, so an uncertain command
  must not be repeated blindly.
- `failed` with the code as evidence when the bridge or the library refused
  before any write, for example `telemetry_stale`, `command_in_progress`,
  `mower_binding_unavailable` or a `mower_command_*` code. Nothing was written.

A pause still supersedes a pending start and waits for the in-flight answer
instead of being refused, which is not a retry. Nothing is replayed on reload,
on a backend switch or after a bridge reconnect. A new coordinator starts
without a command. The `bridge_control` attribute shows the opt-in the bridge
reports, its classes and time bounds.

### Settings

The local settings exist in bridge mode with the same unique ids, in this
integration's `control` mode. Cut Height and Volume are numbers and the five
switches are read from the `settings` of the bridge's state document. Edge
Distance, Pad Direction, Path Distance, Travel Speed and Blade Speed are read
from its `work_parameters`, which the bridge takes from a cloud reading at
most every five minutes, or from the report that confirmed a write.

Cut Height, Volume, Smart No-Go Suggestions, Mow Yellow Grass, Travel Speed and
Blade Speed are written through the bridge's settings route when two opt-ins
meet: this integration's `control` mode and the bridge's own
`settings_mode: write`, which it reports as `routes.settings`. Each change is
one request. The library behind the bridge reads the setting fresh, refuses
what it cannot prove, writes once and confirms only a fresh report of the new
value. Nothing is retried, and a change back is a second deliberate write. A
confirmed setting shows its new value at once.

Stop on Rain Detection, Child Protection, Real Lawn Map, Edge Distance, Pad
Direction and Path Distance stay read only in bridge mode. Changing one raises
an error before any request. Change them in the Eufy app.

### Activity

The E15 answers a local state query without DP 107, so the bridge's local
status is usually `missing`. The mower entity therefore picks its activity from
three kinds of evidence:

1. A connected charger contact from a fresh cloud reading reports `docked`
   when it is newer than the local evidence. A disconnected, absent, invalid,
   failed or expired contact never supplies `docked`.
2. Otherwise the newest local evidence: the reported status of the last poll,
   or the activity a confirmed command reflected, for at most 30 minutes.
   That is `mowing` after start or resume, `paused` after pause and `docked`
   after a confirmed dock. A newer reported poll replaces a command's activity.
   An uncertain command or a `mower_command_already_set` refusal clears it.
3. Otherwise a fresh cloud activity reading, reported and within both
   90-second age bounds. It is shown but never confirms a command or changes
   which command is sent.

Without any of these the activity is unknown, which on the bridge backend is
the normal state of a resting mower. Mission status `idle` does not mean
`docked`, because the payload also occurs while an inactive mower is away from
the dock. Nothing is inferred from battery level, age, absence or inactivity.

Start while paused sends resume, and a resume refused because the mower was
resumed elsewhere makes the next start a start. `bridge_activity_source`
(`report` or `command`) and `bridge_activity_observed_at` show where the
activity comes from. `bridge_activity` stays the polled value, and
`bridge_status` shows the library's status state: `reported`, `missing`,
`invalid` or `unconfirmed`. The cloud charger status and contact are separate
attributes. See [DP 107 activity](protocol-provenance.md#dp-107-activity).

The charger contact needs bridge 0.14.0 or later, and the cloud activity needs
bridge 0.13.0 or later. An older bridge keeps the local and command evidence
only.

### Session history

Session history observes reported activities, the activity a confirmed command
reflected, and each new cloud reading the entity accepts for display. The
bridge refreshes that reading about every 30 seconds. It never observes a
missing, invalid or unconfirmed status, age or absence. A confirmed dock ends
the session. A differing cloud reading received from 90 seconds before to 120
seconds after local evidence is not observed, because the cloud record can lag
the mower. A session with a cloud observation carries `cloud_observed: true`,
because its times are the bridge's receipt times. Time between observations
more than 45 seconds apart counts as an observation gap. Area, distance and
progress stay unknown on the bridge backend.
