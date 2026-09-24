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
`@keesmod/eufy-mega-client` 0.19.0, pinned by the mower bridge to the release
tarball with SHA-256
`982ef2e72167629333a86fab869a8f48439ae33909f30922b1ea8534308f2297` from source commit
`80734ebecf1c83a001b1919bbf85a5ba6689f0c6`, confirms three payloads on the
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

Exact remaining limits. No payload identifies `docked`, `charging`, `idle` or
`error`, so neither the bridge nor bridge mode reports them and dock arrival is
never inferred from inactivity. The mowing-progress source is unidentified and
`progress` stays `unconfirmed`, DP 118 remains map-save progress. The app's
Defogging phase shares the `mowing` payload. The transitional first frame, the
map-saving payload, field 6 = 1 and the default payload are withheld, so a
query that carries one of them reports `status` as `invalid`, and a query
without DP 107 reports `missing`. Nothing is derived from the age of a report
or the absence of a data point. The local backend's reading of DP 1, DP 2 and
DP 118 is unchanged by this evidence.

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
