# Map sources

E15 maps use a separate Tuya P2P media transport that is not available through
the mower's normal local DPS connection. The integration does not bundle that
transport or its Android-only vendor libraries. Its read-only map entity reads
a validated map bundle from one of two sources instead.

**Settings → Devices & Services → Eufy Robomow → Configure → Map source**
selects exactly one source for the map entity:

- **external** (default): a compatible HTTPS map source such as the existing
  Android map helper, configured by URL. Existing entries keep it until you
  change it, and it stays the manual recovery path.
- **bridge**: the read-only map route of the dedicated
  [mower bridge](../bridge/README.md). It needs the **bridge** mower backend,
  see [Mower backends](backends.md), and a bridge that serves maps.

The image entity, its unique id, the session history and the dashboard stay
the same for both sources.

## External source

The external source is configured with:

- **Map source HTTPS URL**, the base URL of the map source.
- **Certificate SHA-256 fingerprint**, an optional pin for private or
  self-signed TLS. Normal certificate validation is used when this is empty.

Authentication is derived from the mower's existing local key, and no
additional token is stored. The source must expose `GET /v1/map` with content
type `application/vnd.eufy-robomow-map+zip`, support `ETag` responses, and use
the `X-Eufy-Map-Mode` request header (`idle` or `stream`) to control its
read-only P2P session. The stored ZIP contains a manifest and exactly these
three files:

- `map.bin.stream`
- `cleanPath.bin.stream`
- `navPath.bin.stream`

Idle maps refresh every five minutes. During an active mowing task Home
Assistant requests changed stream snapshots every two seconds. Clear the source
URL to remove the map entity. No mower setting or geometry is changed.

## Bridge source

The entity reads `GET /v1/mowers/{id}/map` of the configured bridge with the
bridge's URL, token and optional certificate pin, and derives no token from the
local key. The bundle and its validation are the same, but it is bound to the
bridge's mower id instead of the device id.

The bridge answers from memory and paces its own acquisitions, so Home
Assistant checks an idle map every minute with `ETag` and a live map every two
seconds while the bridge reports mowing, paused or returning. When the bridge
serves its last good map after a failed acquisition, `acquisition_status`
becomes `stale` and `acquisition_last_error` names the bridge's code. A refused
request names its code too, for example `map_provisioning_unreadable`.

Choosing the bridge source is refused unless the bridge reports `routes.maps`,
which needs cloud or operator-supplied file provisioning, see the
[deployment guide](bridge-deployment.md#map-provisioning-optional). The
external URL stays stored, so switching back to **external** recovers the
previous source.

## Validation, caching and live coverage

Home Assistant validates the archive, device binding, sizes, hashes, protobuf
and boundary before displaying a map. One latest-good bundle per source is
stored privately under `/config/eufy_robomow_maps/`: `latest.mapbundle` for the
external source and `bridge.mapbundle` for the bridge, so switching the source
never mixes or discards the other one's map. If acquisition fails, the previous
valid map remains available.

While a task runs, Home Assistant accumulates and deduplicates coverage deltas
and renders the newest mower pose as a script-free SVG. Only healthy captures
from the current observation window seed that coverage and pose. An earlier
cached map stays visible without entering the new task's accumulation, and
reloading during mowing starts a new window.

The map draws the boundary, charging area, pathways that leave the lawn,
obstacles, the app's no-go zones as red dashed zones (rectangles and polygons,
not yet ellipses), completed mowing lanes and the latest mower position. The
live marker interpretation matches repeated E15 observations but is not a
vendor-documented contract. It is display-only and never drives mower control.

## Privacy

Map bundles contain private lawn geometry. Never commit them, attach them to
an issue or include them in diagnostics.

Map recovery after a failed or unconfirmed acquisition is described in
[Map recovery](map-recovery.md).
