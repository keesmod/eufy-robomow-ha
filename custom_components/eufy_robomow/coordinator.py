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
        command_pending = self.command and self.command.state in ("sending", "pending")
        self.local_dps = dict(dps)
        self.local_generation += 1
        self.last_local_update = dt_util.utcnow()
        if self.command and sample_started > self.command.sent_monotonic:
            self.command.observe(self.local_generation, self.local_dps)
        if self.session_store:
            self.session_store.observe(self.local_dps, self.last_local_update)

        _LOGGER.debug("Local DPS update received (%d keys)", len(dps))

        # ── 2. Cloud DPS + settings (every CLOUD_POLL_INTERVAL seconds) ───────
        # Two guards before attempting a cloud poll:
        #   a) Sun above horizon — mower cannot operate at night, no point refreshing.
        #      Uses HA's sun.sun entity; if unavailable defaults to allowing the poll.
        #   b) Exponential backoff on consecutive failures (5 min → 10 → 20 → 40 → 60).
        #      _cloud_last_fetch is updated on BOTH success AND failure so a failed
        #      login is not retried every 10 s (which would hammer the Eufy API and
        #      interfere with other integrations sharing the same account).
        now = time.monotonic()
        if command_pending:
            self._carry_forward_cloud_data(dps, local_dp_keys)
        elif self.cloud_client is not None:
            _sun = self.hass.states.get("sun.sun")
            _sun_below_horizon = _sun is not None and _sun.state == "below_horizon"

            if _sun_below_horizon:
                _LOGGER.debug("Sun below horizon — skipping cloud poll")
                self._carry_forward_cloud_data(dps, local_dp_keys)
            else:
                backoff = min(
                    CLOUD_POLL_INTERVAL * (2**self._cloud_consecutive_failures),
                    _CLOUD_MAX_BACKOFF,
                )
                if now - self._cloud_last_fetch >= backoff:
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
        self._supersede_pending(action)
        operation = MowerCommand(action, self.local_generation)
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
                raise HomeAssistantError(
                    f"The mower bridge refused the command before sending it: {exc.code}"
                ) from exc
            operation.finish("uncertain", exc.code)
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
            return
        if outcome.result == "failed":
            operation.finish("rejected", f"bridge:{outcome.end}")
            raise HomeAssistantError("The mower rejected the command")
        operation.finish("uncertain", f"bridge:{outcome.end}")
        raise HomeAssistantError(
            "The command was written, but the mower did not confirm it within the "
            "bridge's read-back window. Check the mower before repeating it."
        )

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
