"""DataUpdateCoordinator for Eufy Robomow."""

from __future__ import annotations

import asyncio
import logging
import time
from threading import Lock
from typing import Any
from homeassistant.util import dt as dt_util

from .bridge_client import (
    BridgeClient,
    BridgeClientError,
    BridgeCommandOutcome,
    parse_state_document,
    refused_before_write,
)
from .commands import MowerCommand
from .sessions import SessionStore
from .telemetry import read_local_activity, robot_status, status_shape, task_active
from .const import CMD_START, CMD_RESUME, CMD_PAUSE, CMD_DOCK
from datetime import datetime, timedelta

import tinytuya

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    TUYA_VERSION,
    POLL_INTERVAL,
    CLOUD_POLL_INTERVAL,
    _CLOUD_MAX_BACKOFF,
    CLOUD_EDGE_MM,
    CLOUD_PATH_MM,
    CLOUD_TRAVEL_SPEED,
    CLOUD_BLADE_SPEED,
    CLOUD_PAD_DIRECTION,
    OPERATING_MODE_CONTROL,
    BACKEND_BRIDGE,
    BACKEND_LOCAL,
)

_LOGGER = logging.getLogger(__name__)

# After this many consecutive local-poll errors we recreate the tinytuya
# Device object to flush any stale socket / connection state.
_MAX_CONSECUTIVE_ERRORS = 5


# The bridge command class behind each mower entity action. Dock is the bridge's
# ``stop`` class: on the owned E15 a stop over DP 1 false ends the task and the
# mower returns to the dock by itself, and the library's return over DP 3 is
# ignored by the firmware.
BRIDGE_COMMAND_CLASSES = {"start": "start", "resume": "resume", "pause": "pause", "dock": "stop"}

# The bridge's reported activities that mean a task runs. They play the role of the
# local backend's DP 1 for the live map. Nothing else counts as a task.
BRIDGE_TASK_ACTIVITIES = frozenset({"mowing", "paused", "returning"})

# Cloud refresh spacing while the local status is ambiguous (DP 1 true, DP 2
# false, DP 118 at 100). Mowing and resting in the dock look alike there, and the
# cloud DPS carry DP 107, which the local status reply never does.
AMBIGUOUS_CLOUD_INTERVAL = 60

# The local readings of a running task. When DP 1 turns false after one of them,
# the task has ended or a stop was sent and the mower drives home with DP 1
# false, 25 to 60 seconds on the owned E15 in September 2026. Only DP 107 shows
# that drive, so the cloud is asked at every local poll for RETURN_WATCH_FAST
# seconds, then every AMBIGUOUS_CLOUD_INTERVAL while DP 107 still reads
# returning. The map-saving or default payload ends the drive.
RUNNING_READINGS = frozenset({"mowing", "paused", "returning"})
RETURN_WATCH_FAST = 120
AT_REST_STATUSES = frozenset({"map_saving", "idle"})
# Spacing of those requests, just under the local poll interval, so every poll
# asks at most once and a refresh requested in between does not ask again.
RETURN_CLOUD_INTERVAL = POLL_INTERVAL - 1

# How long the activity a confirmed bridge command reflected stands in for the
# polled one. The E15 answers state queries without DP 107, so after this bound
# the mower may have changed by itself (its app schedule, the app, a low battery)
# and the activity is unknown again. Nothing is inferred from the age.
BRIDGE_COMMAND_ACTIVITY_MAX_AGE = timedelta(minutes=30)


class EufyMowerCoordinator(DataUpdateCoordinator[dict]):
    """Polls the Eufy E15 via Tuya local protocol every POLL_INTERVAL seconds.

    If an EufyCloudClient is provided, cloud DPS are also fetched every
    CLOUD_POLL_INTERVAL seconds (default 5 min).  ALL raw cloud DPS are merged
    into coordinator.data so every DP is visible as a HA sensor — even cloud-only
    ones like DP3, DP4, DP36, DP102-DP185 that the local Tuya protocol never
    returns.  For any DP present in both transports the local (real-time) value
    takes precedence.

    With ``backend=BACKEND_BRIDGE`` the coordinator instead reads one typed state
    document per poll from the dedicated mower bridge and owns no local socket.
    Start, pause, resume and dock then go through the bridge's opt-in command
    routes, each sent exactly once, while settings writes have no bridge route.
    """

    # Defaults so a coordinator created without __init__ (as in unit tests) is local.
    backend: str = BACKEND_LOCAL
    bridge: BridgeClient | None = None
    bridge_mower_id: str | None = None
    # The library's status field state from the last successful bridge poll
    # (reported, missing, invalid or unconfirmed) and the reported activity.
    bridge_status: str | None = None
    bridge_activity: str | None = None
    bridge_error: str | None = None
    # The activity the last confirmed bridge command reflected and when the
    # library received that report. A confirmed dock records ``docked``: its
    # map-saving payload is the dock arrival the library observed.
    bridge_command_activity: str | None = None
    bridge_command_activity_at: datetime | None = None
    # The observation time of the last successful poll, the bridge's
    # ``observed_at`` in bridge mode.
    last_local_update: datetime | None = None
    # The local backend's DP 107 from the last successful cloud poll, read with
    # the confirmed definitions, and when that poll ran. The shape of the last
    # local status reply (see telemetry.status_shape) and since when it holds:
    # only a cloud poll taken since then decides the reading.
    cloud_robot_status: str | None = None
    cloud_polled_at: datetime | None = None
    local_shape: str | None = None
    shape_since: datetime | None = None
    # The reading after the previous local poll, the monotonic time DP 1 turned
    # false after a running task, and whether the current task shape began while
    # the drive home was watched, which is the dock arrival.
    _previous_reading: str | None = None
    _return_watch_started: float | None = None
    _arrival: bool = False
    # Whether the bridge's last state answer reported ``routes.control`` and the
    # control opt-in it carried (classes, max_state_age_ms, read_back_ms).
    bridge_routes_control: bool = False
    bridge_control: dict[str, Any] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        device_id: str,
        local_key: str,
        cloud_client=None,  # EufyCloudClient | None  (avoid circular import)
        operating_mode: str = "observe_only",
        backend: str = BACKEND_LOCAL,
        bridge: BridgeClient | None = None,
        bridge_mower_id: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL),
        )
        self.host = host
        self.device_id = device_id
        self.local_key = local_key
        self.cloud_client = cloud_client
        self.operating_mode = operating_mode
        # Exactly one backend owns the mower. In bridge mode no local socket is
        # ever opened, no cloud client exists and no write reaches the device.
        self.backend = backend
        self.bridge = bridge
        self.bridge_mower_id = bridge_mower_id
        self.bridge_status = None
        self.bridge_activity = None
        self.bridge_error = None
        self.bridge_command_activity = None
        self.bridge_command_activity_at = None
        self.bridge_routes_control = False
        self.bridge_control = None
        if backend == BACKEND_BRIDGE and (bridge is None or not bridge_mower_id):
            raise ValueError("The bridge backend needs a bridge client and a mower id")

        self._device_lock = Lock()
        # The bridge serves one command per mower at a time. A later command waits
        # here for the answer of the one in flight instead of being refused with
        # ``command_in_progress``. Waiting is not a retry: nothing is sent twice.
        self._bridge_command_lock = asyncio.Lock()
        self.local_dps: dict = {}
        self.local_generation = 0
        self.last_local_update: datetime | None = None
        self.command: MowerCommand | None = None
        self.session_store: SessionStore | None = None
        self._device: tinytuya.Device | None = (
            None if backend == BACKEND_BRIDGE else self._make_device()
        )
        # Use float('-inf') so the first poll always fetches cloud DPS
        self._cloud_last_fetch: float = float("-inf")
        self.cloud_robot_status = None
        self.cloud_polled_at = None
        self.local_shape = None
        self.shape_since = None
        self._previous_reading = None
        self._return_watch_started = None
        self._arrival = False
        # Consecutive cloud failures — used for exponential backoff
        self._cloud_consecutive_failures: int = 0
        # Track consecutive local-poll failures to know when to recreate the device
        self._consecutive_errors: int = 0
        # Set of all DP keys seen so far (local + cloud), used to detect new DPs
        self._known_dps: set[str] = set()
        # Callbacks invoked when previously-unseen DPS keys appear in a poll.
        # Registered by sensor.py so it can add generic sensors on-the-fly.
        # Callbacks receive the current dps dict as their sole argument.
        self._new_dp_callbacks: list = []

    @property
    def control_enabled(self) -> bool:
        """Return whether physical and settings writes are explicitly enabled."""
        return self.operating_mode == OPERATING_MODE_CONTROL

    @property
    def writes_available(self) -> bool:
        """Settings writes need explicit control and a backend that routes them.

        Only the local backend writes settings. The bridge has no setting route,
        so no number, select or switch entity is created in bridge mode and every
        settings write is refused there before touching any transport.
        """
        return self.control_enabled and self.backend != BACKEND_BRIDGE

    @property
    def commands_available(self) -> bool:
        """Mower commands need explicit control and a backend that routes them.

        The local backend writes them itself. The bridge backend routes start, dock,
        pause and resume only while its last state answer reported
        ``routes.control``, which is the bridge's own control opt-in.
        """
        if not self.control_enabled:
            return False
        if self.backend == BACKEND_BRIDGE:
            return self.bridge_routes_control
        return True

    @property
    def fresh_cloud_status(self) -> str | None:
        """DP 107 from a cloud poll that ran since the local status took its shape.

        None while no such poll succeeded, so an older cloud value never decides
        the current shape.
        """
        if (
            self.shape_since is None
            or self.cloud_polled_at is None
            or self.cloud_polled_at < self.shape_since
        ):
            return None
        return self.cloud_robot_status

    @property
    def local_activity(self) -> str | None:
        """The local backend's activity: the last status reply with fresh DP 107."""
        return read_local_activity(self.local_dps, self.fresh_cloud_status)

    def _return_watch_open(self, now: float) -> bool:
        """DP 1 turned false after a running task and the drive home may still run.

        The watch ends with the map-saving or default payload, a new local shape,
        or after RETURN_WATCH_FAST seconds unless DP 107 still reads returning.
        """
        started = self._return_watch_started
        if started is None or self.local_shape != "no_task":
            return False
        status = self.fresh_cloud_status
        if status in AT_REST_STATUSES:
            return False
        return now - started <= RETURN_WATCH_FAST or status == "returning"

    def _activity_refresh_due(self, now: float) -> bool:
        """Whether the local status needs DP 107 from the cloud now, also at night.

        The ambiguous shape asks at once and then every AMBIGUOUS_CLOUD_INTERVAL.
        A map save, and the dock arrival after a watched drive home, ask once.
        The drive home asks at once and at every local poll while it is watched.
        A failed cloud poll keeps its backoff, so none of these repeats it.
        """
        if self._cloud_consecutive_failures or self.shape_since is None:
            return False
        fresh = self.cloud_polled_at is not None and self.cloud_polled_at >= self.shape_since
        since_fetch = now - self._cloud_last_fetch
        if self.local_shape == "ambiguous":
            return not fresh or since_fetch >= AMBIGUOUS_CLOUD_INTERVAL
        if self.local_shape == "map_save" or (self.local_shape == "task" and self._arrival):
            return not fresh
        if self._return_watch_open(now):
            if not fresh:
                return True
            assert self._return_watch_started is not None
            if now - self._return_watch_started <= RETURN_WATCH_FAST:
                return since_fetch >= RETURN_CLOUD_INTERVAL
            return since_fetch >= AMBIGUOUS_CLOUD_INTERVAL
        return False

    def _command_cloud_due(self, now: float) -> bool:
        """A pending start or resume that only DP 107 from the cloud can confirm.

        While the mower rests in the dock DP 1 is already true and DP 118 at 100,
        so a start leaves the local status unchanged, although on 2026-09-25 the
        mower started and the cloud showed it within 4 seconds. Then the cloud is
        asked at every local poll that began after the write while the command
        is pending, and a failed cloud poll keeps its backoff.
        """
        command = self.command
        if (
            command is None
            or command.state != "pending"
            or command.action not in ("start", "resume")
            or now <= command.sent_monotonic
            or self._cloud_consecutive_failures
            or self.local_shape != "ambiguous"
            or status_shape(command.before or {}) != "ambiguous"
        ):
            return False
        return (
            self._cloud_last_fetch < command.sent_monotonic
            or now - self._cloud_last_fetch >= RETURN_CLOUD_INTERVAL
        )

    def _track_local_shape(self, now: float) -> None:
        """Follow the local status shape after a successful poll.

        A new shape needs a new cloud answer. DP 1 turning false after a running
        task opens the drive-home watch, and a task shape that follows an open
        watch is the dock arrival, whose map save the cloud confirms.
        """
        shape = status_shape(self.local_dps)
        if shape == self.local_shape:
            return
        self._arrival = shape == "task" and self._return_watch_open(now)
        self._return_watch_started = (
            now if shape == "no_task" and self._previous_reading in RUNNING_READINGS else None
        )
        self.local_shape = shape
        self.shape_since = self.last_local_update

    @property
    def bridge_activity_evidence(self) -> tuple[str, str, datetime | None] | None:
        """The newest evidenced bridge activity as (activity, source, observed_at).

        ``report`` is the reported status of the last successful poll. ``command``
        is the activity a confirmed command reflected, while it is younger than
        BRIDGE_COMMAND_ACTIVITY_MAX_AGE. The E15 answers queries without DP 107,
        so after a command the polled status is usually missing. A missing,
        invalid or unconfirmed status says nothing, and nothing is derived from
        age or absence.
        """
        candidates: list[tuple[datetime | None, str, str]] = []
        if self.bridge_status == "reported" and self.bridge_activity:
            candidates.append((self.last_local_update, "report", self.bridge_activity))
        observed_at = self.bridge_command_activity_at
        if (
            self.bridge_command_activity
            and observed_at is not None
            and dt_util.utcnow() - observed_at <= BRIDGE_COMMAND_ACTIVITY_MAX_AGE
        ):
            candidates.append((observed_at, "command", self.bridge_command_activity))
        if not candidates:
            return None
        oldest = datetime.min.replace(tzinfo=dt_util.UTC)
        newest = max(candidates, key=lambda candidate: candidate[0] or oldest)
        return newest[2], newest[1], newest[0]

    @property
    def bridge_task_active(self) -> bool:
        """A task runs according to the newest evidenced bridge activity.

        Only a mowing, paused or returning activity counts, reported by the last
        successful poll or reflected by a recent confirmed command. A missing,
        invalid or unconfirmed status, a failed poll and the age of an
        observation never do.
        """
        evidence = self.bridge_activity_evidence
        return (
            self.last_update_success
            and evidence is not None
            and evidence[0] in BRIDGE_TASK_ACTIVITIES
        )

    def _require_control_enabled(self) -> None:
        """Reject writes while the integration is in its safe default mode."""
        if not self.control_enabled:
            raise HomeAssistantError(
                "Eufy Robomow is in observe-only mode. Enable control in the "
                "integration options before sending commands."
            )

    def _require_writes_available(self) -> None:
        """Reject settings writes that no backend can route, before any transport."""
        if self.backend == BACKEND_BRIDGE:
            raise HomeAssistantError(
                "Eufy Robomow uses the mower bridge backend, which has no settings "
                "routes. Select the local backend for settings writes."
            )
        self._require_control_enabled()

    def _require_commands_available(self) -> None:
        """Reject mower commands that no backend can route, before any transport."""
        self._require_control_enabled()
        if self.backend == BACKEND_BRIDGE and not self.bridge_routes_control:
            raise HomeAssistantError(
                "The mower bridge is not in control mode, so it routes no command. "
                "Enable control on the bridge itself before sending commands through it."
            )

    def async_add_new_dp_listener(self, callback) -> None:
        """Register *callback(dps)* to be called whenever new DPS keys are discovered.

        The callback receives the current complete DPS dict as its argument so it
        can inspect the latest values without waiting for coordinator.data to update.
        """
        self._new_dp_callbacks.append(callback)

    def _make_device(self) -> tinytuya.Device:
        d = tinytuya.Device(
            self.device_id,
            self.host,
            self.local_key,
            version=TUYA_VERSION,
        )
        d.set_socketTimeout(5)
        # Non-persistent: close the TCP socket after every request.
        # Persistent mode (the default in some tinytuya versions) keeps the
        # socket open between polls.  When that socket silently dies it is never
        # freed, causing file-descriptor leaks and eventually OOM / CPU spikes.
        d.set_socketPersistent(False)
        return d

    def _device_call(self, method: str, *args):
        """Serialize worker calls even if an awaiting task is cancelled."""
        with self._device_lock:
            if self._device is None:
                raise HomeAssistantError("No local mower connection exists in bridge mode")
            return getattr(self._device, method)(*args)

    # ── polling ───────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        """Fetch DPS from device (local) and optionally from cloud.

        Strategy:
          1. Local poll  — always, every POLL_INTERVAL seconds (~10 s).
          2. Cloud poll  — every CLOUD_POLL_INTERVAL seconds (~5 min).
             • Fetches ALL raw cloud DPS via tuya.m.device.dp.get.
             • Merges them into the dict: local values win for any DP in both.
             • Decoded DP155 settings are stored under cloud_* keys.
          3. Carry-forward — when cloud poll is not due (or fails), all keys from
             the previous coordinator.data that are not in the fresh local dps are
             carried forward so cloud-only entities never go unavailable.
          4. New-DP detection — after the full merge, new DPS keys fire the
             _new_dp_callbacks with the complete dps dict so sensor.py can
             register generic sensors immediately.
        """
        if self.backend == BACKEND_BRIDGE:
            return await self._async_update_from_bridge()

        # ── 1. Local DPS (every POLL_INTERVAL seconds) ────────────────────────
        sample_started = time.monotonic()
        try:
            result = await self.hass.async_add_executor_job(self._device_call, "status")
        except Exception as exc:  # noqa: BLE001
            self._consecutive_errors += 1
            if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                _LOGGER.debug(
                    "Recreating tinytuya device after %d consecutive errors",
                    self._consecutive_errors,
                )
                self._device = self._make_device()
                self._consecutive_errors = 0
            raise UpdateFailed(f"Tuya connection error: {exc}") from exc

        if not isinstance(result, dict) or not isinstance(result.get("dps"), dict):
            raise UpdateFailed("No valid local mower telemetry received")

        if "Error" in result:
            err = result["Error"]
            _LOGGER.debug("Tuya poll error: %s (%s)", err, result.get("Err"))
            self._consecutive_errors += 1
            if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                _LOGGER.debug(
                    "Recreating tinytuya device after %d consecutive errors",
                    self._consecutive_errors,
                )
                self._device = self._make_device()
                self._consecutive_errors = 0
            raise UpdateFailed(f"Tuya error: {err}")

        self._consecutive_errors = 0
        dps: dict = result.get("dps", {})
        local_dp_keys: set[str] = set(dps.keys())
        self.local_dps = dict(dps)
        self.local_generation += 1
        self.last_local_update = dt_util.utcnow()
        if self.command and sample_started > self.command.sent_monotonic:
            self.command.observe(self.local_generation, self.local_dps)
        if self.session_store:
            self.session_store.observe(self.local_dps, self.last_local_update)
        now = time.monotonic()
        self._track_local_shape(now)
        # A command still pending after this poll keeps the cloud out, so its
        # confirmation never waits for a cloud request. The poll that confirmed a
        # command may ask, which shows the drive home right after a dock, and a
        # start whose effect only DP 107 can show asks while it is pending.
        command_pending = self.command is not None and self.command.state in ("sending", "pending")
        command_cloud = self._command_cloud_due(now)

        _LOGGER.debug("Local DPS update received (%d keys)", len(dps))

        # ── 2. Cloud DPS + settings (every CLOUD_POLL_INTERVAL seconds) ───────
        # Two guards before attempting a cloud poll:
        #   a) Sun above horizon — mower cannot operate at night, no point refreshing.
        #      Uses HA's sun.sun entity; if unavailable defaults to allowing the poll.
        #   b) Exponential backoff on consecutive failures (5 min → 10 → 20 → 40 → 60).
        #      _cloud_last_fetch is updated on BOTH success AND failure so a failed
        #      login is not retried every 10 s (which would hammer the Eufy API and
        #      interfere with other integrations sharing the same account).
        if command_pending and not command_cloud:
            self._carry_forward_cloud_data(dps, local_dp_keys)
        elif self.cloud_client is not None:
            _sun = self.hass.states.get("sun.sun")
            _sun_below_horizon = _sun is not None and _sun.state == "below_horizon"
            # Where the local status alone cannot show the activity, DP 107 is
            # needed from the cloud now, also at night: the ambiguous shape, a map
            # save and the drive home after a task. Failures keep their backoff.
            activity_refresh = command_cloud or self._activity_refresh_due(now)

            if _sun_below_horizon and not activity_refresh:
                _LOGGER.debug("Sun below horizon — skipping cloud poll")
                self._carry_forward_cloud_data(dps, local_dp_keys)
            else:
                backoff = min(
                    CLOUD_POLL_INTERVAL * (2**self._cloud_consecutive_failures),
                    _CLOUD_MAX_BACKOFF,
                )
                if activity_refresh or now - self._cloud_last_fetch >= backoff:
                    try:
                        raw_cloud_dps, cloud_settings = await self.hass.async_add_executor_job(
                            self.cloud_client.get_all_dps
                        )
                        # Merge: add every cloud DP that is NOT already in local dps.
                        # Local values take precedence (more real-time).
                        for dp_key, dp_val in raw_cloud_dps.items():
                            if dp_key not in local_dp_keys:
                                dps[dp_key] = dp_val

                        # Decoded DP155 settings under cloud_* keys
                        dps[CLOUD_EDGE_MM] = cloud_settings["edge_mm"]
                        dps[CLOUD_PATH_MM] = cloud_settings["path_mm"]
                        dps[CLOUD_TRAVEL_SPEED] = cloud_settings["travel_speed"]
                        dps[CLOUD_BLADE_SPEED] = cloud_settings["blade_speed"]
                        dps[CLOUD_PAD_DIRECTION] = cloud_settings["pad_direction"]

                        self._cloud_last_fetch = now
                        self._cloud_consecutive_failures = 0
                        self.cloud_robot_status = robot_status(raw_cloud_dps.get("107"))
                        self.cloud_polled_at = dt_util.utcnow()
                        if command_cloud and self.command is not None:
                            self.command.observe_cloud(self.cloud_robot_status)
                        _LOGGER.debug(
                            "Cloud poll: %d raw DPS merged, settings=%s",
                            len(raw_cloud_dps),
                            cloud_settings,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._cloud_consecutive_failures += 1
                        next_retry = min(
                            CLOUD_POLL_INTERVAL * (2**self._cloud_consecutive_failures),
                            _CLOUD_MAX_BACKOFF,
                        )
                        _LOGGER.warning(
                            "Cloud DPS fetch failed (attempt %d, next retry in %ds): %s",
                            self._cloud_consecutive_failures,
                            next_retry,
                            exc,
                        )
                        # Update timestamp so the failed attempt is not retried on the
                        # next local poll (10 s); retry honours the backoff window.
                        self._cloud_last_fetch = now
                        self._carry_forward_cloud_data(dps, local_dp_keys)
                else:
                    # Not yet due — carry forward previous cloud data
                    self._carry_forward_cloud_data(dps, local_dp_keys)

        # ── 3. New-DP detection (after full merge) ────────────────────────────
        current_all_keys = set(dps.keys())
        new_keys = current_all_keys - self._known_dps

        def _dp_sort_key(k: str):
            # Numeric DP keys sort before string keys (e.g. "cloud_edge_mm").
            # Returns a (int, str) tuple so comparisons are always type-compatible.
            return (0, int(k)) if k.isdigit() else (1, k)

        if new_keys:
            _LOGGER.info(
                "New DPS discovered: %s",
                sorted(new_keys, key=_dp_sort_key),
            )
            # Pass the current full dps dict so callbacks don't read stale data
            for cb in list(self._new_dp_callbacks):
                cb(dps)
        self._known_dps = current_all_keys
        self._previous_reading = self.local_activity

        return dps

    async def _async_update_from_bridge(self) -> dict:
        """One typed state query through the mower bridge, then its own state.

        Freshness comes from the bridge's ``observed_at``, never from the poll
        time. A stale answer, an error code, an unreachable bridge or an invalid
        document fails the update so entities become unavailable, exactly like a
        failed local poll. Nothing is carried forward and nothing is written.

        The bridge's ``GET /v1/state`` is read afterwards for ``routes.control``,
        which decides whether commands are available. A failure of that query
        alone leaves the telemetry update intact and makes control unavailable.
        """
        assert self.bridge is not None and self.bridge_mower_id is not None
        try:
            document = await self.bridge.async_mower_state(self.bridge_mower_id)
            telemetry = parse_state_document(document, self.bridge_mower_id)
        except BridgeClientError as exc:
            self.bridge_error = exc.code
            raise UpdateFailed(f"Mower bridge request failed: {exc.code}") from exc
        if telemetry.stale or telemetry.error is not None:
            self.bridge_error = telemetry.error or "stale"
            raise UpdateFailed(f"Mower bridge lost mower data: {self.bridge_error}")
        self.bridge_error = None
        self.bridge_status = telemetry.status
        self.bridge_activity = telemetry.activity
        if telemetry.command_activity is not None:
            # A report of the command still running on the bridge, for example
            # returning within a second of a dock that confirms only at arrival.
            self._record_activity(*telemetry.command_activity)
        self.local_generation += 1
        self.last_local_update = telemetry.observed_at
        if self.session_store and telemetry.status == "reported" and telemetry.activity:
            # Only a reported activity is an observation. Missing, invalid and
            # unconfirmed say nothing, and nothing is derived from age or absence.
            self.session_store.observe_activity(telemetry.activity, telemetry.observed_at)
        dps = dict(telemetry.dps)
        _LOGGER.debug("Bridge state received (%d mapped keys)", len(dps))
        self._known_dps = set(dps.keys())
        await self._async_read_bridge_control()
        return dps

    async def _async_read_bridge_control(self) -> None:
        """Read whether the bridge routes commands from its own state document."""
        assert self.bridge is not None
        try:
            state = await self.bridge.async_state()
        except BridgeClientError as exc:
            _LOGGER.debug("Bridge state query failed: %s", exc.code)
            self.bridge_routes_control = False
            self.bridge_control = None
            return
        routes = state.get("routes")
        control = state.get("control")
        self.bridge_routes_control = isinstance(routes, dict) and routes.get("control") is True
        self.bridge_control = (
            control if self.bridge_routes_control and isinstance(control, dict) else None
        )

    def _carry_forward_cloud_data(self, dps: dict, local_dp_keys: set[str]) -> None:
        """Copy all non-local keys from the previous coordinator.data into dps.

        This keeps cloud-only DP sensors from going unavailable between cloud polls.
        Local DP values already in dps are never overwritten.
        """
        if not self.data:
            return
        for key, val in self.data.items():
            if key not in local_dp_keys:
                dps[key] = val

    # ── commands ──────────────────────────────────────────────────────────────

    async def async_send_mower_command(self, action: str, timeout: float = 35) -> None:
        """Send once, then await fresh device telemetry with a bounded timeout.

        In bridge mode the command goes through the bridge's opt-in route once and
        its answer is the confirmation, see :meth:`_async_send_bridge_command`.
        """
        if self.backend == BACKEND_BRIDGE:
            await self._async_send_bridge_command(action)
            return
        self._require_writes_available()
        if action not in ("start", "resume", "pause", "dock"):
            raise HomeAssistantError("Unsupported mower command")
        if action == "pause" and task_active(self.local_dps) is False:
            # Without a running task the E15 ignores a pause. On 2026-09-25 a pause
            # written during the drive home after a dock left DP 2 false and DP 107
            # returning until the dock arrival, and the command timed out.
            raise HomeAssistantError(
                "The mower runs no task, so Home Assistant cannot pause it. During "
                "the drive home the E15 ignores a pause. Use Pause or Stop in the "
                "eufy app instead."
            )
        self._supersede_pending(action)
        operation = MowerCommand(action, self.local_generation, before=dict(self.local_dps))
        self.command = operation
        self.async_update_listeners()
        dp, value = {
            "start": CMD_START,
            "resume": CMD_RESUME,
            "pause": CMD_PAUSE,
            "dock": CMD_DOCK,
        }[action]
        try:
            result = await self.hass.async_add_executor_job(
                self._device_call, "set_value", int(dp), value
            )
            if operation.state == "superseded":
                raise HomeAssistantError("Command superseded by a later safety command")
            if isinstance(result, dict) and "Error" in result:
                operation.finish("rejected")
                raise HomeAssistantError("The mower rejected the command")
            # Ignore every poll that began before the write finished.
            operation.after_generation = self.local_generation
            operation.sent_monotonic = time.monotonic()
            operation.state = "pending"
            self.async_update_listeners()
            async with asyncio.timeout(timeout):
                await self.async_request_refresh()
                await operation.event.wait()
            if operation.state == "superseded":
                raise HomeAssistantError("Command superseded by a later safety command")
        except TimeoutError as exc:
            if operation.state == "confirmed":
                # Confirmed during the refresh, whose cloud request then outlasted
                # the bound. The confirmation stands.
                return
            operation.finish("timeout")
            raise HomeAssistantError(
                "Command sent, but the mower did not confirm its state within 35 seconds. "
                "It may still execute; check the mower before retrying."
            ) from exc
        except asyncio.CancelledError:
            operation.finish("interrupted")
            raise
        except HomeAssistantError:
            raise
        except Exception as exc:
            operation.finish("failed")
            raise HomeAssistantError("Could not send the mower command") from exc
        finally:
            self.async_update_listeners()

    def _supersede_pending(self, action: str) -> None:
        """Apply the pending rules: start and resume wait, pause and dock supersede."""
        if self.command and self.command.state in ("sending", "pending"):
            if action in ("start", "resume"):
                raise HomeAssistantError("A mower command is already pending")
            self.command.finish("superseded")

    async def _async_send_bridge_command(self, action: str) -> None:
        """Route start, pause, resume or dock through the bridge exactly once.

        Dock goes to the bridge's ``stop`` class. On the owned E15 firmware a stop
        over DP 1 false ends the task and the mower returns to the dock by
        itself, while the library's return over DP 3 is ignored, so the bridge
        has no return route. The bridge checks mode, class, mower, ownership and
        telemetry age before any write and answers the library's confirmed,
        failed or uncertain outcome. A refusal known to precede the write ends as
        ``failed`` with its code, every other failure ends as ``uncertain``
        because the frame may have been written. Nothing is retried and a
        superseded command that has not been sent yet is never sent.
        """
        self._require_commands_available()
        if action not in ("start", "resume", "pause", "dock"):
            raise HomeAssistantError("Unsupported mower command")
        kind = BRIDGE_COMMAND_CLASSES[action]
        self._supersede_pending(action)
        assert self.bridge is not None and self.bridge_mower_id is not None
        operation = MowerCommand(action, self.local_generation)
        self.command = operation
        self.async_update_listeners()
        interrupted = False
        try:
            async with self._bridge_command_lock:
                if operation.state == "superseded":
                    raise HomeAssistantError("Command superseded by a later safety command")
                operation.sent_monotonic = time.monotonic()
                operation.state = "pending"
                self.async_update_listeners()
                outcome = await self.bridge.async_send_command(self.bridge_mower_id, kind)
            if operation.state == "superseded":
                raise HomeAssistantError("Command superseded by a later safety command")
            self._finish_bridge_command(operation, outcome)
        except BridgeClientError as exc:
            if operation.state == "superseded":
                raise HomeAssistantError(
                    "Command superseded by a later safety command"
                ) from exc
            if refused_before_write(exc.code):
                operation.finish("failed", exc.code)
                if exc.code == "mower_command_already_set":
                    # The library's fresh query contradicted the activity this
                    # command was chosen from, so it is no longer evidence.
                    self._clear_command_activity()
                raise HomeAssistantError(
                    f"The mower bridge refused the command before sending it: {exc.code}"
                ) from exc
            operation.finish("uncertain", exc.code)
            self._clear_command_activity(before=dt_util.parse_datetime(operation.requested_at))
            raise HomeAssistantError(
                f"The mower bridge did not answer the command ({exc.code}). The command "
                "may have been written. Check the mower before repeating it."
            ) from exc
        except asyncio.CancelledError:
            interrupted = True
            operation.finish("interrupted")
            raise
        finally:
            if not interrupted:
                await self.async_request_refresh()
            self.async_update_listeners()

    def _finish_bridge_command(self, operation: MowerCommand, outcome: BridgeCommandOutcome) -> None:
        """Map the bridge's outcome onto the command states the entity exposes.

        A confirmed dock carries the map-saving payload instead of an activity:
        the evidence ``bridge:map_saving`` is the dock arrival the library
        observed, never an inference.
        """
        if outcome.result == "confirmed":
            operation.finish("confirmed", f"bridge:{outcome.activity or outcome.payload}")
            self._record_command_activity(outcome)
            return
        if outcome.result == "failed":
            operation.finish("rejected", f"bridge:{outcome.end}")
            raise HomeAssistantError("The mower rejected the command")
        operation.finish("uncertain", f"bridge:{outcome.end}")
        self._clear_command_activity(before=dt_util.parse_datetime(operation.requested_at))
        raise HomeAssistantError(
            "The command was written, but the mower did not confirm it within the "
            "bridge's read-back window. Check the mower before repeating it."
        )

    def _record_command_activity(self, outcome: BridgeCommandOutcome) -> None:
        """Keep the activity a confirmed command reflected, with its report time.

        A reflected activity is a fresh DP 107 report. The map-saving payload of
        a confirmed stop is the dock arrival the library observed, so it records
        ``docked``. A newer reported poll wins. The session history observes the
        activity once, as it observes a reported status.
        """
        activity = outcome.activity or ("docked" if outcome.payload == "map_saving" else None)
        if activity is None or outcome.observed_at is None:
            return
        self._record_activity(activity, outcome.observed_at)

    def _record_activity(self, activity: str, observed_at: datetime) -> None:
        """Keep one reported activity from a command as the bridge-mode evidence.

        An older report than the evidence already held changes nothing, and the
        session history observes each accepted activity once.
        """
        held = self.bridge_command_activity_at
        if held is not None and held >= observed_at:
            return
        if (
            self.bridge_status == "reported"
            and self.bridge_activity
            and self.last_local_update is not None
            and self.last_local_update >= observed_at
        ):
            return
        self.bridge_command_activity = activity
        self.bridge_command_activity_at = observed_at
        if self.session_store:
            self.session_store.observe_activity(activity, observed_at)

    def _clear_command_activity(self, before: datetime | None = None) -> None:
        """Forget the command activity once the mower's state is no longer known.

        With ``before``, a report received at or after that time stays: the
        running command's own progress, for example ``returning`` during a dock
        whose arrival fell outside the read-back, was observed after the write.
        """
        held = self.bridge_command_activity_at
        if before is not None and held is not None and held >= before:
            return
        self.bridge_command_activity = None
        self.bridge_command_activity_at = None

    async def async_send_command(self, dp: str, value) -> None:
        """Write a single DPS value or raise a visible Home Assistant error."""
        self._require_writes_available()
        _LOGGER.debug("Sending command DP %s = %s", dp, value)
        try:
            result = await self.hass.async_add_executor_job(
                self._device_call, "set_value", int(dp), value
            )
            if isinstance(result, dict) and "Error" in result:
                raise HomeAssistantError(f"Mower rejected command DP {dp}: {result['Error']}")
            _LOGGER.debug("Command result: %s", result)
            # Immediately refresh state
            await self.async_request_refresh()
        except HomeAssistantError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HomeAssistantError(f"Mower command DP {dp} failed: {exc}") from exc

    async def async_set_cloud_setting(self, **kwargs) -> None:
        """Write one or more cloud settings via the Tuya mobile API.

        Keyword arguments: edge_mm, path_mm, travel_speed, blade_speed, pad_direction.
        Raises a visible Home Assistant error when the write is not confirmed.
        """
        self._require_writes_available()
        if not self.cloud_client:
            raise HomeAssistantError(
                "Cloud settings are unavailable because no cloud client is configured."
            )

        def _do_set() -> None:
            self.cloud_client.set_settings(**kwargs)

        try:
            await self.hass.async_add_executor_job(_do_set)
            # Small delay to let cloud process the write before re-fetching
            await asyncio.sleep(1)
            # Force a cloud re-fetch on the next poll cycle
            self._cloud_last_fetch = float("-inf")
            await self.async_request_refresh()
        except Exception as exc:  # noqa: BLE001
            exc_str = str(exc)
            if "DEVICE_OFFLINE" in exc_str or "offline" in exc_str.lower():
                _LOGGER.warning(
                    "Cannot update cloud setting: device is offline "
                    "(command will not be queued; retry when mower is running)"
                )
            else:
                _LOGGER.error("Cloud setting update failed: %s", exc)
            raise HomeAssistantError(f"Cloud setting update failed: {exc}") from exc
