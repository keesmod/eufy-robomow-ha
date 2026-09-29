# Map recovery after an unconfirmed cancellation

This is the evidence and recovery policy for [#71](https://github.com/keesmod/eufy-robomow-ha/issues/71).
Bridge/app 0.13.3 introduced one automatic read-only probe. #71 was closed on
2026-09-28 at the owner's request with hardware observation waived, see
[closure](#closure-of-71). The unchanged policy subsequently recovered from
one natural unconfirmed cancellation on the owned E15 on 2026-09-29, see
[observed recovery](#observed-recovery-on-2026-09-29).

On 2026-09-28 the owner explicitly accepted the unknown remote transfer lifetime
for this recovery rule, including future use of the same rule. The code guarantees
local exclusivity only. The fifteen-minute wait does not prove remote termination.
This acceptance permits merge and installation after the required checks and
backup. It does not establish hardware recovery or allow overlapping local
acquisitions, a shorter wait, repeated failed probes or unconfirmed local cleanup.
Do not ask again for this same bounded uncertainty.

## What survived the 2026-09-28 restart

Read-only recorder inspection gives these UTC transitions for the map entity:

| Time | Recorded result |
| --- | --- |
| 11:41:12.827 | Stale, `mower_map_connection_failed` |
| 11:42:38.870 | Stale, `mower_map_cancel_unconfirmed` |
| 11:52:17.088 | Stale, `map_unavailable`, after the installation restart |
| 11:54:17.141 | Healthy, no acquisition error |

The last map before the restart carries capture time 11:42:37. The first
healthy post-restart map carries capture time 11:53:17. These are receipt
observations, not the exact times a peer stopped transferring. In particular,
the unconfirmed cancellation predates the reported 11:50 task end.

The recorder kept the stable error but not the bridge's `last_demand` or its
`cancellation_failure`. The current bridge container has only five startup
lines for 11:35 to 12:00 and no map outcome. The Home Assistant container has
no retained log lines in that interval. The on-disk core logs predate this
incident. No retained per-demand output was found in the inspected temporary,
share or backup locations. No ping trace covers the failure. The precise
cancellation failure is therefore unknown. The preceding connection failure
makes a carrier interruption plausible, but does not establish its cause.

## Library behavior

The pinned 0.25.2 archive's `mowers/maps/acquisition.js` and `session.js` show:

- A carrier failure before stopping returns `connection_failed`.
- Once a demand stops during download, the library sends one correlated cancel
  and waits up to five seconds. A hard abort then yields `cancel_unconfirmed`
  with `cancellationFailure: timeout`. A socket failure during cancellation
  yields that same end reason with `connection_failed`. A mismatched response
  yields `response_mismatch`.
- The 10000 ms `local_timeout_ms` setting applies to the separate LAN state
  and command path. It does not extend this map cancellation deadline.
- Cleanup closes the locally owned sockets and clears timers and private
  buffers. It reports whether that local work completed. It does not prove
  that the remote peer processed the cancel.
- The affected acquisition instance refuses reuse. A new instance negotiates
  fresh random signaling and carrier identities and obtains provisioning
  through the configured source.

An interruption of five to seven seconds during the cancel wait could cause
this result. That is a source-based explanation, not a diagnosis of this
particular event.

## Recovery policy

1. Keep the demand active until `shutdown()` returns. If cleanup is unconfirmed
   or shutdown rejects, block until restart. A delay cannot repair an unknown
   local resource lifetime.
2. For `cancel_unconfirmed` with confirmed cleanup, wait fifteen minutes from
   returned shutdown, measured with a monotonic clock. The displayed wall-clock
   deadline is informational and clock corrections cannot shorten or prolong
   the wait. Requests keep receiving the last good map with its stale
   flag and error. A stream lease cannot bypass the wait.
3. The first request at or after the deadline starts one fresh acquisition.
   No independent timer or background demand is created. Requests during the
   probe, including during its shutdown, cannot start another acquisition.
4. Resume the ordinary idle and stream schedule only after the probe publishes
   a valid new map and ends with confirmed cancellation and cleanup. A failed
   probe, failed provisioning, missing map or unconfirmed cancellation blocks
   until restart. There is no sequence of automatic retries.

The fifteen-minute delay exceeds the approximately eleven minutes between
the observed failure and the first post-restart map. It leaves margin over the
observed network interruptions and local teardown, but no permitted evidence
establishes a remote transfer lifetime. Neither the 30-second demand nor the
65-second provisioning validity requirement is a peer timeout. The delay is
therefore a conservative operator policy, not proof that the peer has stopped.
The code guarantees no overlapping local acquisition instances. The original
remote transfer's lifetime remains unknown. One successful recovery without a
process restart was observed on the E15 on 2026-09-29. This does not establish
a remote expiry bound or prove recovery for other failure modes.

`maps.recovery` exposes the state and planned probe time. `maps.last_recovery`
retains the latest probe's timestamps, end reason, publication count and
cancellation and cleanup confirmations after ordinary demands resume. It
contains no device identifiers, credentials or geometry. A provisioning failure
never reaches the library and leaves that last probe summary unchanged.

## Validation and hardware boundary

Synthetic HTTP-route regressions cover concurrent requests, both request modes,
no acquisition before the deadline, one probe, resumed idle and stream schedules,
failed probes, failed provisioning, incomplete maps, unconfirmed cleanup,
delayed shutdown and bridge shutdown during the wait and the probe.

For hardware acceptance, observe a natural unconfirmed cancellation in normal
use. Record the original failure time, returned shutdown, probe time and outcome,
both confirmations and a fresh healthy map. Confirm the bridge process did not
restart. Do not create an outage or send a physical command to provoke the case.
The last recovery summary survives later demands but not a bridge restart.

## Installation on the owned E15 environment

Bridge/app 0.13.3 from `5071678` replaced 0.13.2 on 2026-09-28 after the owner
accepted the rule. Supervisor backup `ca0d4988`, preserved prior app sources
and a passing configuration check preceded the update. The new process started
at 13:03:42 UTC. Readback at 13:05:36 UTC confirmed matching sources and package
versions, preserved identities and settings, and a healthy ordinary acquisition
with cancellation and cleanup confirmed. Integration 0.16.1 was unchanged and
Home Assistant Core did not restart. The
[installation receipt](https://github.com/keesmod/eufy-robomow-ha/issues/71#issuecomment-5870459019)
records the archive checksum and entity checks.

`last_recovery` was null. This installation check is not evidence of recovery
from an unconfirmed cancellation without a restart.

## Closure of #71

Two read-only observations on 2026-09-28 found no unconfirmed cancellation.
From 14:07 to 14:18 UTC in idle operation, 18 acquisitions published valid
maps, see the [idle observation](https://github.com/keesmod/eufy-robomow-ha/issues/71#issuecomment-5871855831).
From 15:38 UTC during a short mowing round the owner confirmed and supervised,
5 acquisitions published 102 valid maps, see the
[mowing observation](https://github.com/keesmod/eufy-robomow-ha/issues/71#issuecomment-5873479410).
Every acquisition confirmed cancellation and cleanup, the bridge process did
not restart and both recovery fields stayed null.

At 15:52 UTC the owner closed #71 and waived the remaining hardware
criterion, because recurrence seems unlikely, see the
[closing comment](https://github.com/keesmod/eufy-robomow-ha/issues/71#issuecomment-5873618452).
At closure, automatic recovery after a real `mower_map_cancel_unconfirmed`
remained unobserved. The later recurrence and successful recovery below add
hardware evidence without changing the accepted policy.


## Observed recovery on 2026-09-29

During the app-controlled charger-contact check in #86, on bridge/app 0.14.0,
library 0.27.0 and integration 0.17.0, a map demand naturally ended with
unconfirmed cancellation. No fault was injected. The operator made no further
physical command after returning the mower to its station.

| UTC time | Observation |
| --- | --- |
| 13:21:05.789 | Demand ended `cancel_unconfirmed`, cancellation failure `connection_failed`, local cleanup confirmed, one map published |
| 13:36:05.789 | Displayed earliest probe time after the accepted fifteen-minute wait |
| 13:37:04.364 | The next normal map request started one fresh probe |
| 13:37:05.548 | Probe ended with one valid publication, no rejected maps, cancellation and cleanup confirmed |
| 13:38:14.912 | HA map healthy, mower `docked`, fresh connected charger contact, no unavailable integration entities |

The probe's end was `aborted`, the normal bounded-demand stop after obtaining
its map, with both confirmations true. `maps.recovery` and `maps.error`
cleared, and `maps.last_recovery` retained the probe result. The bridge
container start was `2026-09-29T13:17:16.744879339Z` both before and after the
observation. It was not restarted. The bounded observer stopped after HA
reported the healthy map. See the [receipt](https://github.com/keesmod/eufy-robomow-ha/issues/86#issuecomment-5891459242).

This is one observed recovery on the owned E15. The original remote transfer's
expiry and the cause of its connection failure remain unknown. No broader
model, network-failure or remote-cleanup claim follows from this result.
