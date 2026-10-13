# Rain-aware planning

[`examples/eufy_mower_planning.yaml`](../examples/eufy_mower_planning.yaml) is
an opt-in Home Assistant package that starts the mower in a dry mowing window.
It is [experimental](release-candidates.md#experimental). Its entity names and
notifications are in Dutch.

Automatic mowing (`input_boolean.eufy_automatisch_maaien`) starts switched off.
Configure the package and supervise its validation before you switch it on.
Schedules in the Eufy app run independently of the package.

## Setup

1. Add the file as a
   [Home Assistant package](https://www.home-assistant.io/docs/configuration/packages/).
2. Replace every `example_*` entity with your own:
   - `sensor.example_buienalarm_rain_data`, a Buienalarm precipitation sensor
     whose `data` attribute holds `success`, `start`, `delta` and `precip`.
   - `switch.example_irrigation_valve` and
     `input_boolean.example_irrigation_active`, which show running irrigation.
   - `input_boolean.example_irrigation_planned` and
     `input_datetime.example_irrigation_start`, which show planned irrigation.
3. Adjust the mower entity ids if yours differ. The package uses
   `lawn_mower.eufy_robomow_e15_mower`, `sensor.eufy_robomow_e15_battery`,
   `switch.eufy_robomow_e15_stop_on_rain_detection` and
   `switch.eufy_robomow_e15_child_protection`.
4. Restart Home Assistant and check the settings below.

Missing, unknown or stale sources block automatic starts. The `planning` view of
the [dashboard card](../README.md#dashboard-card) shows the reason from
`sensor.eufy_maaiplanning` and the settings, as in
[`examples/dashboard.yaml`](../examples/dashboard.yaml).

## Settings

A one-time automation at the first Home Assistant start sets the initial
values. Mowing days starts at its first option.

| Setting | Values | Initial value |
| --- | --- | --- |
| Mowing days | `Ma / wo / vr` (Mon, Wed, Fri), `Di / do / za` (Tue, Thu, Sat), `Dagelijks` (daily) or `Weekend` (Sat, Sun) | `Ma / wo / vr` |
| Earliest and latest start | a time of day | 10:00 and 15:00 |
| Dry period after rain, irrigation or a restart | 30 to 240 min | 60 min |
| Maximum session duration | 30 to 120 min | 90 min |
| Minimum start battery | 40 to 100 % | 80 % |

## Start conditions

Every five minutes the package checks these conditions and starts the mower
only when all of them hold:

- Automatic mowing is on, today is a mowing day and the time is inside the
  start window.
- No start attempt was made today.
- The sun is up and sets no sooner than the maximum session duration plus 30
  minutes from now.
- The mower telemetry is younger than 45 seconds, the integration is in
  `control` and the mower is `docked`.
- The battery is at or above the minimum start level.
- Stop on Rain Detection and Child Protection are on.
- The radar forecast is fresh, covers the whole session and reports no rain.
- No irrigation is running, and none is planned to start before the end of the
  session plus 30 minutes.
- The dry period has passed since the last rain, irrigation or Home Assistant
  restart.

## Start and watchdog

The package records the start attempt before it sends the start, so it makes
at most one start attempt per day, also after a timeout. It then marks the
session as its own.

A watchdog checks its own sessions every minute and when irrigation starts:

- When the maximum session duration has passed, it sends one dock.
- When the mower is mowing and the conditions sensor
  `sensor.eufy_maaiomstandigheden` no longer reports dry weather, it sends one
  pause. This happens when irrigation runs or is planned within the maximum
  session duration plus 30 minutes, when the irrigation state or plan is
  unknown, and when the radar forecast is missing, stale, too short, invalid
  or reports rain.

Each watchdog command is sent once, without retries and without an automatic
resume. A notification asks you to check the mower. The session stops counting
as the package's own after the watchdog command, or after the mower has been
docked for two minutes.
