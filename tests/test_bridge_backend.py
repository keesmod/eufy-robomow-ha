"""Tests for the bridge backend of the coordinator, the mower entity and setup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.lawn_mower import LawnMowerActivity, LawnMowerEntityFeature
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import frame
from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.eufy_robomow as integration
from custom_components.eufy_robomow.bridge_client import BridgeClient, BridgeClientError
from custom_components.eufy_robomow.const import (
    BACKEND_BRIDGE,
    BACKEND_LOCAL,
    DOMAIN,
    CONF_BACKEND,
    CONF_BRIDGE_MOWER_ID,
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_DEVICE_ID,
    CONF_EUFY_EMAIL,
    CONF_EUFY_PASSWORD,
    CONF_LOCAL_KEY,
    CONF_OPERATING_MODE,
    OPERATING_MODE_CONTROL,
    OPERATING_MODE_OBSERVE_ONLY,
)
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.lawn_mower import EufyRobomowEntity
from custom_components.eufy_robomow.sensor import SENSORS, EufySensor
from custom_components.eufy_robomow.sessions import SessionStore

TOKEN = "synthetic-bridge-token-0123456789abcdef"
MOWER_ID = "c" * 64
OBSERVED_AT = "2026-09-19T10:00:01.250Z"
STATUS_DP = ["107", "107", "107"]
# The control opt-in bridge 0.6.0 reports in control mode. The stop route text is never served.
CONTROL_BLOCK = {"classes": ["start", "pause", "resume", "stop"], "max_state_age_ms": 30000, "read_back_ms": 20000}
START_AND_PAUSE = LawnMowerEntityFeature.START_MOWING | LawnMowerEntityFeature.PAUSE
ALL_CONTROLS = START_AND_PAUSE | LawnMowerEntityFeature.DOCK


def _at(seconds: int) -> str:
    return f"2026-09-19T10:00:{1 + seconds:02d}.250Z"


def _bridge_state(**overrides: Any) -> dict[str, Any]:
    """A bridge state document. The default is the bridge's own observe_only mode."""
    state: dict[str, Any] = {
        "protocol": 1,
        "bridge": "eufy-robomow-bridge",
        "version": "0.6.0",
        "bridge_id": "00000000-0000-4000-8000-000000000000",
        "lifecycle": "running",
        "operating_mode": "observe_only",
        "auth": {"state": "authenticated", "last_error": None, "attempted_at": OBSERVED_AT},
        "client": {"package": "@keesmod/eufy-mega-client", "version": "0.17.0", "module": "mowers", "lifecycle": "open", "connected": True},
        "mowers": {"count": 1, "discovered_at": OBSERVED_AT, "error": None},
        "routes": {"discovery": True, "state": True, "control": False, "maps": False},
        "control": None,
    }
    state.update(overrides)
    return state


def _control_state() -> dict[str, Any]:
    return _bridge_state(
        operating_mode="control",
        routes={"discovery": True, "state": True, "control": True, "maps": False},
        control=dict(CONTROL_BLOCK),
    )


def _outcome(
    command: str,
    result: str = "confirmed",
    activity: str | None = "mowing",
    stage: str = "reflected",
    end: str = "reflected",
    payload: str | None = None,
) -> dict[str, Any]:
    """A contract 1 command answer. Raw data points are never part of it.

    A stop carries the map-saving ``payload`` instead of an activity: the library
    received it at the dock arrival about 30 seconds after the write.
    """
    return {
        "contract": 1,
        "id": MOWER_ID,
        "command": command,
        "result": result,
        "write": {"dp": "1", "code": "switch_go", "value": command != "stop"},
        "sent_at": "2026-09-19T16:42:38.199Z",
        "stage": stage,
        "end": end,
        "before_observed_at": "2026-09-19T16:42:38.198Z",
        "reply": {"observed_at": "2026-09-19T16:42:38.202Z", "return_code_zero": True, "rejected": result == "failed"},
        "acknowledgement": {"observed_at": "2026-09-19T16:42:39.150Z", "sequence": 63859, "dp": "1"},
        "activity": {"observed_at": "2026-09-19T16:42:39.351Z", "sequence": 63860, "value": activity} if activity else None,
        "payload": {"observed_at": "2026-09-20T10:58:18.236Z", "sequence": 47724, "name": payload} if payload else None,
        "reports": 3,
    }


def _reported_status(activity: str, observed_at: str = OBSERVED_AT) -> dict[str, Any]:
    return {"state": "reported", "value": activity, "dp": STATUS_DP, "source": "local-tuya-3.5", "observedAt": observed_at}


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "contract": 1,
        "id": MOWER_ID,
        "source": "local-tuya-3.5",
        "observed_at": OBSERVED_AT,
        "age_ms": 12,
        "stale": False,
        "error": None,
        "status": {"state": "missing", "dp": STATUS_DP},
        "battery": {"state": "reported", "value": {"percent": 85}, "dp": ["8"], "source": "local-tuya-3.5", "observedAt": OBSERVED_AT},
        "progress": {"state": "unconfirmed"},
        "network": {"state": "reported", "value": {"kind": "wifi", "signalPercent": 70}, "dp": ["134", "109"], "source": "local-tuya-3.5", "observedAt": OBSERVED_AT},
    }
    document.update(overrides)
    return document


class _FakeBridge:
    """Stands in for BridgeClient. Answers are documents or errors, in order.

    ``state`` is the bridge state document (or error) every ``async_state`` call
    answers. ``command_answers`` are consumed by ``async_send_command`` in order,
    each after ``gate`` is set when a gate exists, so a test can hold a command
    in flight. Every request is recorded, so a test can prove nothing was resent.
    """

    def __init__(
        self,
        answers: list[Any],
        state: Any = None,
        command_answers: list[Any] | None = None,
        setting_answers: list[Any] | None = None,
        work_parameter_answers: list[Any] | None = None,
    ) -> None:
        self.answers = answers
        self.requested: list[str] = []
        self.state = _bridge_state() if state is None else state
        self.state_requests = 0
        self.command_answers = command_answers or []
        self.commands: list[tuple[str, str]] = []
        self.setting_answers = setting_answers or []
        self.settings: list[tuple[str, str, Any]] = []
        self.work_parameter_answers = work_parameter_answers or []
        self.work_parameters: list[tuple[str, str, str]] = []
        self.gate: asyncio.Event | None = None

    async def async_mower_state(self, mower_id: str) -> dict[str, Any]:
        self.requested.append(mower_id)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def async_state(self) -> dict[str, Any]:
        self.state_requests += 1
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    async def async_send_command(self, mower_id: str, kind: str) -> Any:
        self.commands.append((mower_id, kind))
        if self.gate is not None:
            await self.gate.wait()
        answer = self.command_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        from custom_components.eufy_robomow.bridge_client import parse_command_outcome

        return parse_command_outcome(answer, mower_id, kind)

    async def async_set_setting(self, mower_id: str, key: str, value: Any) -> Any:
        self.settings.append((mower_id, key, value))
        answer = self.setting_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        from custom_components.eufy_robomow.bridge_client import parse_setting_outcome

        return parse_setting_outcome(answer, mower_id, key, value)

    async def async_set_work_parameter(self, mower_id: str, key: str, value: str) -> Any:
        self.work_parameters.append((mower_id, key, value))
        answer = self.work_parameter_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        from custom_components.eufy_robomow.bridge_client import parse_work_parameter_outcome

        return parse_work_parameter_outcome(answer, mower_id, key, value)


def _coordinator(hass: HomeAssistant, bridge: _FakeBridge, operating_mode: str = OPERATING_MODE_CONTROL) -> EufyMowerCoordinator:
    coordinator = EufyMowerCoordinator(
        hass,
        host="192.0.2.1",
        device_id="synthetic-device",
        local_key="not-a-real-local-key",
        operating_mode=operating_mode,
        backend=BACKEND_BRIDGE,
        bridge=cast(BridgeClient, bridge),
        bridge_mower_id=MOWER_ID,
    )
    # The refresh after a command is recorded, not run, so the fake's answers stay in order.
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    return coordinator


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _entry(**options: Any) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CONF_HOST: "192.0.2.1",
            CONF_DEVICE_ID: "synthetic-device",
            CONF_LOCAL_KEY: "not-a-real-local-key",
            CONF_EUFY_EMAIL: "owner@example.invalid",
            CONF_EUFY_PASSWORD: "not-a-real-password",
        },
        options=options,
        entry_id="test-entry",
    )


def _run(test: Any, tmp_path: Path) -> None:
    async def wrapper() -> None:
        hass = HomeAssistant(str(tmp_path))
        # The coordinator reports usage through the frame helper, as in a running core.
        frame.async_setup(hass)
        try:
            await test(hass)
        finally:
            await hass.async_stop()

    asyncio.run(wrapper())


def test_bridge_backend_owns_the_mower_without_a_local_device(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge([_document()])
        with patch("custom_components.eufy_robomow.coordinator.tinytuya.Device") as device:
            coordinator = EufyMowerCoordinator(
                hass,
                host="192.0.2.1",
                device_id="synthetic-device",
                local_key="not-a-real-local-key",
                operating_mode=OPERATING_MODE_CONTROL,
                backend=BACKEND_BRIDGE,
                bridge=cast(BridgeClient, bridge),
                bridge_mower_id=MOWER_ID,
            )
            device.assert_not_called()
        assert coordinator.cloud_client is None
        assert coordinator.control_enabled is True
        assert coordinator.writes_available is False, "the local backend's cloud client is never used"
        assert coordinator.setting_entities_available is True, "the local settings read through the bridge"
        assert coordinator.work_parameter_entities_available is True, "the work parameters read through the bridge"
        assert coordinator.commands_available is False, "nothing before the bridge reports its mode"

        data = await coordinator._async_update_data()
        assert data == {"8": 85, "134": "Wifi", "109": 70}
        assert bridge.requested == [MOWER_ID]
        assert bridge.state_requests == 1
        assert coordinator.last_local_update == datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC)
        assert coordinator.bridge_status == "missing"
        assert coordinator.bridge_activity is None
        assert coordinator.bridge_error is None
        assert coordinator.bridge_routes_control is False, "the bridge runs in observe_only"
        assert coordinator.bridge_control is None
        assert coordinator.commands_available is False
        assert coordinator.local_dps == {}, "no raw local data points exist in bridge mode"

        with pytest.raises(HomeAssistantError, match="not in control mode"):
            await coordinator.async_send_mower_command("start")
        with pytest.raises(HomeAssistantError, match="does not accept setting writes"):
            await coordinator.async_send_command("110", 40)
        with pytest.raises(HomeAssistantError, match="read only through the mower bridge"):
            await coordinator.async_set_cloud_setting(edge_mm=10)
        with pytest.raises(HomeAssistantError, match="does not accept setting writes"):
            await coordinator.async_set_cloud_setting(travel_speed="fast")
        assert bridge.settings == [], "a refused setting never reaches the bridge"
        device.assert_not_called()
        assert bridge.commands == [], "a refused command never reaches the bridge"
        assert coordinator.command is None, "a refused command never becomes pending"

    _run(scenario, tmp_path)


def test_bridge_backend_reads_routes_control_from_the_bridge_state(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge([_document() for _ in range(4)], state=_control_state())
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        assert entity.supported_features == LawnMowerEntityFeature(0), "nothing before the first bridge answer"

        await coordinator._async_update_data()
        assert coordinator.bridge_routes_control is True
        assert coordinator.bridge_control == CONTROL_BLOCK
        assert coordinator.commands_available is True
        assert coordinator.writes_available is False, "the local backend's cloud client is still never used"
        assert coordinator.bridge_routes_settings is False, "the control opt-in does not open the settings route"
        assert entity.supported_features == ALL_CONTROLS
        assert entity.supported_features & LawnMowerEntityFeature.DOCK, "dock goes through the bridge's stop route"
        assert entity.extra_state_attributes["bridge_control"] == CONTROL_BLOCK

        bridge.state = BridgeClientError("cannot_connect")
        data = await coordinator._async_update_data()
        assert data == {"8": 85, "134": "Wifi", "109": 70}, "a failed bridge state query alone keeps the telemetry"
        assert coordinator.bridge_error is None
        assert coordinator.bridge_routes_control is False, "but control becomes unavailable"
        assert coordinator.bridge_control is None
        assert coordinator.commands_available is False
        assert entity.supported_features == LawnMowerEntityFeature(0)
        with pytest.raises(HomeAssistantError, match="not in control mode"):
            await coordinator.async_send_mower_command("pause")

        bridge.state = _control_state()
        await coordinator._async_update_data()
        assert coordinator.commands_available is True

        bridge.state = _bridge_state(
            routes={"discovery": True, "state": True, "control": "yes", "maps": False}, control=dict(CONTROL_BLOCK)
        )
        await coordinator._async_update_data()
        assert coordinator.bridge_routes_control is False, "only a boolean true counts"
        assert coordinator.bridge_control is None
        assert bridge.state_requests == 4
        assert bridge.commands == []

    _run(scenario, tmp_path)


def test_bridge_commands_refuse_before_any_transport(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge([_document()], state=_control_state())
        coordinator = _coordinator(hass, bridge, operating_mode=OPERATING_MODE_OBSERVE_ONLY)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        await coordinator._async_update_data()
        assert coordinator.bridge_routes_control is True, "the bridge's opt-in alone"
        assert coordinator.commands_available is False, "does not override the integration's observe-only"
        assert entity.supported_features == LawnMowerEntityFeature(0)
        for action in ("start", "resume", "pause", "dock"):
            with pytest.raises(HomeAssistantError, match="observe-only"):
                await coordinator.async_send_mower_command(action)
        with pytest.raises(HomeAssistantError, match="observe-only"):
            await entity.async_start_mowing()

        coordinator.operating_mode = OPERATING_MODE_CONTROL
        assert coordinator.commands_available is True
        for action in ("stop", "return"):
            with pytest.raises(HomeAssistantError, match="Unsupported"):
                await coordinator.async_send_mower_command(action)

        assert bridge.commands == [], "no refusal reached the bridge"
        assert coordinator.command is None
        cast(AsyncMock, coordinator.async_request_refresh).assert_not_awaited()

    _run(scenario, tmp_path)


def test_bridge_commands_route_start_pause_and_resume_with_each_outcome(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document(status=_reported_status("paused"))],
            state=_control_state(),
            command_answers=[
                _outcome("start"),
                _outcome("pause", result="failed", activity=None, stage="sent", end="rejected"),
                _outcome("resume", result="uncertain", activity=None, stage="acknowledged", end="timed_out"),
                _outcome("resume", activity="mowing"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        await coordinator._async_update_data()
        refresh = cast(AsyncMock, coordinator.async_request_refresh)

        await coordinator.async_send_mower_command("start")
        command = coordinator.command
        assert command is not None
        assert (command.action, command.state, command.evidence) == ("start", "confirmed", "bridge:mowing")
        assert command.finished_at is not None
        attribute = entity.extra_state_attributes["command"]
        assert attribute["action"] == "start"
        assert attribute["state"] == "confirmed"
        assert attribute["evidence"] == "bridge:mowing"
        assert refresh.await_count == 1, "fresh telemetry is requested after the answer"

        with pytest.raises(HomeAssistantError, match="rejected"):
            await entity.async_pause()
        assert coordinator.command is not None
        assert (coordinator.command.action, coordinator.command.state) == ("pause", "rejected")
        assert coordinator.command.evidence == "bridge:rejected"

        with pytest.raises(HomeAssistantError, match="did not confirm it"):
            await entity.async_start_mowing()
        assert coordinator.command is not None
        assert (coordinator.command.action, coordinator.command.state) == ("resume", "uncertain"), "paused picks resume"
        assert coordinator.command.evidence == "bridge:timed_out"
        assert entity.extra_state_attributes["command"]["state"] == "uncertain"

        await entity.async_start_mowing()
        assert coordinator.command is not None
        assert (coordinator.command.state, coordinator.command.evidence) == ("confirmed", "bridge:mowing")

        assert bridge.commands == [(MOWER_ID, "start"), (MOWER_ID, "pause"), (MOWER_ID, "resume"), (MOWER_ID, "resume")]
        assert refresh.await_count == 4
        assert coordinator.local_dps == {}, "no raw data point was invented for the confirmation"

    _run(scenario, tmp_path)


def test_bridge_dock_goes_through_the_stop_route_and_reports_the_dock_arrival(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document(status=_reported_status("mowing"))],
            state=_control_state(),
            command_answers=[
                _outcome("stop", activity=None, payload="map_saving"),
                _outcome("stop", result="uncertain", activity=None, stage="acknowledged", end="timed_out"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        await coordinator._async_update_data()
        assert entity.supported_features & LawnMowerEntityFeature.DOCK

        await entity.async_dock()
        command = coordinator.command
        assert command is not None
        assert (command.action, command.state) == ("dock", "confirmed"), "the entity action stays dock"
        assert command.evidence == "bridge:map_saving", "the map-saving payload is the observed dock arrival"
        assert bridge.commands == [(MOWER_ID, "stop")], "dock is the bridge's stop class"
        assert entity.extra_state_attributes["command"]["evidence"] == "bridge:map_saving"

        with pytest.raises(HomeAssistantError, match="did not confirm it"):
            await coordinator.async_send_mower_command("dock")
        assert coordinator.command is not None
        assert (coordinator.command.action, coordinator.command.state) == ("dock", "uncertain")
        assert coordinator.command.evidence == "bridge:timed_out"
        assert bridge.commands == [(MOWER_ID, "stop"), (MOWER_ID, "stop")], "an uncertain dock is never repeated by the integration"
        assert cast(AsyncMock, coordinator.async_request_refresh).await_count == 2

    _run(scenario, tmp_path)


def test_bridge_refusals_end_failed_and_transport_loss_ends_uncertain(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        cases = [
            ("telemetry_stale", 409, "failed", "before sending it: telemetry_stale"),
            ("mower_command_map_saving", 409, "failed", "mower_command_map_saving"),
            ("control_disabled", 403, "failed", "control_disabled"),
            ("command_in_progress", 409, "failed", "command_in_progress"),
            ("mower_local_unreachable", 503, "failed", "mower_local_unreachable"),
            ("timeout", None, "uncertain", "may have been written"),
            ("mower_local_disconnected", 503, "uncertain", "may have been written"),
            ("request_aborted", 503, "uncertain", "may have been written"),
            ("invalid_document", None, "uncertain", "may have been written"),
        ]
        bridge = _FakeBridge(
            [_document()],
            state=_control_state(),
            command_answers=[BridgeClientError(code, status) for code, status, _, _ in cases],
        )
        coordinator = _coordinator(hass, bridge)
        await coordinator._async_update_data()

        for code, _, state, match in cases:
            with pytest.raises(HomeAssistantError, match=match):
                await coordinator.async_send_mower_command("start")
            assert coordinator.command is not None
            assert coordinator.command.state == state, code
            assert coordinator.command.evidence == code
            assert coordinator.command.finished_at is not None
        assert len(bridge.commands) == len(cases), "each request went out exactly once, nothing was retried"
        assert cast(AsyncMock, coordinator.async_request_refresh).await_count == len(cases)

    _run(scenario, tmp_path)


def test_bridge_pause_supersedes_an_in_flight_start_and_waits_for_its_answer(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document(status=_reported_status("mowing"))],
            state=_control_state(),
            command_answers=[_outcome("start"), _outcome("pause", activity="paused")],
        )
        bridge.gate = asyncio.Event()
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        await coordinator._async_update_data()

        start = asyncio.create_task(coordinator.async_send_mower_command("start"))
        await _settle()
        started = coordinator.command
        assert started is not None
        assert started.state == "pending"
        assert bridge.commands == [(MOWER_ID, "start")]
        assert entity.extra_state_attributes["command"]["state"] == "pending"

        with pytest.raises(HomeAssistantError, match="already pending"):
            await coordinator.async_send_mower_command("resume")
        with pytest.raises(HomeAssistantError, match="already pending"):
            await coordinator.async_send_mower_command("start")
        assert len(bridge.commands) == 1

        pause = asyncio.create_task(entity.async_pause())
        await _settle()
        assert started.state == "superseded"
        assert coordinator.command is not None
        assert coordinator.command.action == "pause"
        assert coordinator.command.state == "sending", "the pause waits for the answer to the start"
        assert len(bridge.commands) == 1, "waiting is not a second request"

        bridge.gate.set()
        with pytest.raises(HomeAssistantError, match="superseded"):
            await start
        await pause
        assert bridge.commands == [(MOWER_ID, "start"), (MOWER_ID, "pause")]
        assert started.state == "superseded", "the start's late answer never overwrites the superseded state"
        assert (coordinator.command.state, coordinator.command.evidence) == ("confirmed", "bridge:paused")
        assert cast(AsyncMock, coordinator.async_request_refresh).await_count == 2

    _run(scenario, tmp_path)


def test_a_superseded_command_that_never_left_is_never_sent(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document(status=_reported_status("mowing"))],
            state=_control_state(),
            command_answers=[_outcome("start"), _outcome("pause", activity="paused")],
        )
        bridge.gate = asyncio.Event()
        coordinator = _coordinator(hass, bridge)
        await coordinator._async_update_data()

        start = asyncio.create_task(coordinator.async_send_mower_command("start"))
        await _settle()
        first_pause = asyncio.create_task(coordinator.async_send_mower_command("pause"))
        await _settle()
        waiting = coordinator.command
        second_pause = asyncio.create_task(coordinator.async_send_mower_command("pause"))
        await _settle()
        assert waiting is not None
        assert waiting.state == "superseded", "a pause that is still waiting can be superseded too"

        bridge.gate.set()
        with pytest.raises(HomeAssistantError, match="superseded"):
            await start
        with pytest.raises(HomeAssistantError, match="superseded"):
            await first_pause
        await second_pause
        assert bridge.commands == [(MOWER_ID, "start"), (MOWER_ID, "pause")], "the superseded pause was never sent"
        assert coordinator.command is not None
        assert coordinator.command.state == "confirmed"

    _run(scenario, tmp_path)


def test_backend_switch_reload_or_interruption_replays_nothing(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge([_document()], state=_control_state(), command_answers=[_outcome("start")])
        bridge.gate = asyncio.Event()
        coordinator = _coordinator(hass, bridge)
        await coordinator._async_update_data()
        start = asyncio.create_task(coordinator.async_send_mower_command("start"))
        await _settle()
        assert coordinator.command is not None
        assert coordinator.command.state == "pending"

        # A reload or a backend switch builds a new coordinator without any memory of the pending command.
        with patch("custom_components.eufy_robomow.coordinator.tinytuya.Device"):
            local = EufyMowerCoordinator(
                hass,
                host="192.0.2.1",
                device_id="synthetic-device",
                local_key="not-a-real-local-key",
                operating_mode=OPERATING_MODE_CONTROL,
            )
        assert local.backend == BACKEND_LOCAL
        assert local.command is None
        rebuilt = _coordinator(hass, _FakeBridge([_document()], state=_control_state()))
        assert rebuilt.command is None
        await rebuilt._async_update_data()
        assert rebuilt.command is None, "a bridge reconnect replays nothing"

        # Unloading the old coordinator interrupts its command. It is never resent.
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert coordinator.command.state == "interrupted"
        assert coordinator.command.finished_at is not None
        assert bridge.commands == [(MOWER_ID, "start")], "sent once, never again"
        cast(AsyncMock, coordinator.async_request_refresh).assert_not_awaited()

    _run(scenario, tmp_path)


def test_session_history_in_bridge_mode_observes_reported_activities_only(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [
                _document(status=_reported_status("mowing")),
                _document(status={"state": "missing", "dp": STATUS_DP}, observed_at=_at(10)),
                _document(status=_reported_status("paused", _at(20)), observed_at=_at(20)),
                _document(status={"state": "invalid", "dp": STATUS_DP}, observed_at=_at(30)),
                _document(status={"state": "unconfirmed"}, observed_at=_at(35)),
                _document(status=_reported_status("returning", _at(40)), observed_at=_at(40)),
                _document(status=_reported_status("mowing", _at(50)), observed_at=_at(50), stale=True, error="mower_local_unreachable", age_ms=30_000),
            ]
        )
        coordinator = _coordinator(hass, bridge)
        store = SessionStore(hass, "test-entry")
        coordinator.session_store = store
        history = store.history

        await coordinator._async_update_data()
        assert history.current is not None
        assert history.current["phase"] == "mowing"
        assert history.current["started_at"] == datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC).isoformat()
        assert history.current["start_observed"] is False, "no inactive observation preceded it"
        assert (history.current["area_raw"], history.current["distance_m"], history.current["progress"]) == (None, None, None)

        await coordinator._async_update_data()
        assert history.current["phase"] == "mowing"
        assert history.current["last_observed_at"] == datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC).isoformat(), "a missing status is not an observation"

        await coordinator._async_update_data()
        assert history.current["phase"] == "paused"
        assert history.current["mowing_seconds"] == 20
        assert history.current["pause_count"] == 1

        await coordinator._async_update_data()
        await coordinator._async_update_data()
        assert history.current["phase"] == "paused", "invalid and unconfirmed change nothing"
        assert history.current["last_observed_at"] == datetime(2026, 9, 19, 10, 0, 21, 250000, tzinfo=UTC).isoformat()

        await coordinator._async_update_data()
        assert history.current["phase"] == "returning"
        assert history.current["paused_seconds"] == 20

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        assert history.current["phase"] == "returning", "a stale document is never an observation"
        assert history.recent == [], "no E15 payload reports docked, so the end is never observed in bridge mode"
        assert history.current["observation_gap"] is True, "the session began without an observed start"

    _run(scenario, tmp_path)


def _reflected(command: str, observed_at: str, activity: str | None = "mowing", payload: str | None = None) -> dict[str, Any]:
    """A confirmed command answer whose reflecting report the library received at ``observed_at``."""
    answer = _outcome(command, activity=activity, payload=payload)
    for field in ("activity", "payload"):
        if answer[field] is not None:
            answer[field]["observed_at"] = observed_at
    return answer


def _clock(now: datetime) -> Any:
    return patch("custom_components.eufy_robomow.coordinator.dt_util.utcnow", return_value=now)


def test_a_confirmed_command_stands_in_for_the_missing_activity_and_makes_resume_reachable(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [
                _document(),
                _document(status=_reported_status("returning", "2026-09-24T08:40:00.000Z"), observed_at="2026-09-24T08:40:00.000Z"),
            ],
            state=_control_state(),
            command_answers=[
                _reflected("pause", "2026-09-24T08:32:15.160Z", activity="paused"),
                _reflected("resume", "2026-09-24T08:33:29.369Z"),
                _reflected("stop", "2026-09-24T08:36:20.993Z", activity=None, payload="map_saving"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        with _clock(datetime(2026, 9, 24, 8, 37, tzinfo=UTC)):
            await coordinator._async_update_data()
            assert entity.activity is None, "the E15 answers a state query without DP 107"

            await entity.async_pause()
            assert entity.activity == LawnMowerActivity.PAUSED
            attributes = entity.extra_state_attributes
            assert attributes["bridge_activity_source"] == "command"
            assert attributes["bridge_activity_observed_at"] == "2026-09-24T08:32:15.160000+00:00"
            assert attributes["bridge_activity"] is None, "the polled activity stays what the poll reported"
            assert coordinator.bridge_task_active is True

            await entity.async_start_mowing()
            assert bridge.commands[-1] == (MOWER_ID, "resume"), "paused from the confirmed pause picks resume"
            assert entity.activity == LawnMowerActivity.MOWING

            await entity.async_dock()
            assert entity.activity == LawnMowerActivity.DOCKED, "the confirmed dock is the observed dock arrival"
            assert coordinator.bridge_task_active is False
        assert bridge.commands == [(MOWER_ID, "pause"), (MOWER_ID, "resume"), (MOWER_ID, "stop")]

        with _clock(datetime(2026, 9, 24, 9, 6, 21, tzinfo=UTC)):
            assert entity.activity is None, "after the bound the mower may have changed by itself"
            assert entity.extra_state_attributes["bridge_activity_source"] is None

        with _clock(datetime(2026, 9, 24, 8, 40, 5, tzinfo=UTC)):
            await coordinator._async_update_data()
            assert entity.activity == LawnMowerActivity.RETURNING, "a newer reported poll wins"
            assert entity.extra_state_attributes["bridge_activity_source"] == "report"

    _run(scenario, tmp_path)


def test_an_uncertain_command_or_a_contradicting_refusal_leaves_the_activity_unknown(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document()],
            state=_control_state(),
            command_answers=[
                _reflected("pause", "2026-09-24T08:32:15.160Z", activity="paused"),
                BridgeClientError("mower_command_already_set", 409),
                _reflected("start", "2026-09-24T08:33:40.000Z"),
                BridgeClientError("telemetry_stale", 409),
                _outcome("pause", result="uncertain", activity=None, stage="acknowledged", end="timed_out"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        with _clock(datetime(2026, 9, 24, 8, 34, tzinfo=UTC)):
            await coordinator._async_update_data()
            await entity.async_pause()
            assert entity.activity == LawnMowerActivity.PAUSED

            # Someone resumed in the app: the library's fresh query shows DP 2 already false.
            with pytest.raises(HomeAssistantError, match="mower_command_already_set"):
                await entity.async_start_mowing()
            assert bridge.commands[-1] == (MOWER_ID, "resume")
            assert entity.activity is None, "the refusal contradicted paused, so it is no longer evidence"

            await entity.async_start_mowing()
            assert bridge.commands[-1] == (MOWER_ID, "start"), "without paused evidence start is sent"
            assert entity.activity == LawnMowerActivity.MOWING

            with pytest.raises(HomeAssistantError, match="telemetry_stale"):
                await entity.async_pause()
            assert entity.activity == LawnMowerActivity.MOWING, "a refusal that says nothing about the mower keeps it"

            with pytest.raises(HomeAssistantError, match="did not confirm it"):
                await entity.async_pause()
            assert entity.activity is None, "an uncertain command leaves the mower's state unknown"
        assert len(bridge.commands) == 5, "every command went out once, nothing was retried"

    _run(scenario, tmp_path)


def _running(command: str, value: str, observed_at: str) -> dict[str, Any]:
    """The running command block of bridge 0.9.0 with the latest confirmed activity."""
    return {"command": command, "acknowledged_at": observed_at, "activity": {"observed_at": observed_at, "sequence": 12, "value": value}}


def test_a_dock_shows_the_drive_home_from_the_running_command_and_then_the_arrival(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [
                _document(),
                _document(observed_at="2026-09-24T09:29:50.000Z", command=_running("stop", "returning", "2026-09-24T09:29:40.600Z")),
                _document(observed_at="2026-09-24T09:30:10.000Z"),
            ],
            state=_control_state(),
            command_answers=[
                _reflected("resume", "2026-09-24T09:29:27.239Z"),
                _reflected("stop", "2026-09-24T09:30:05.584Z", activity=None, payload="map_saving"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        with _clock(datetime(2026, 9, 24, 9, 29, 40, tzinfo=UTC)):
            await coordinator._async_update_data()
            await coordinator.async_send_mower_command("resume")
            assert entity.activity == LawnMowerActivity.MOWING
            bridge.gate = asyncio.Event()
            dock = asyncio.create_task(entity.async_dock())
            await _settle()
            await coordinator._async_update_data()
            assert entity.activity == LawnMowerActivity.RETURNING, "the running stop reported returning"
            assert entity.extra_state_attributes["bridge_activity_observed_at"] == "2026-09-24T09:29:40.600000+00:00"
            bridge.gate.set()
            await dock
            assert entity.activity == LawnMowerActivity.DOCKED, "the confirmed dock is the arrival"
            await coordinator._async_update_data()
            assert entity.activity == LawnMowerActivity.DOCKED, "a poll without a running command changes nothing"
        assert bridge.commands == [(MOWER_ID, "resume"), (MOWER_ID, "stop")]

    _run(scenario, tmp_path)


def test_an_uncertain_dock_keeps_the_drive_home_it_observed_but_not_older_evidence(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [
                _document(),
                _document(observed_at="2026-09-24T09:29:50.000Z", command=_running("stop", "returning", "2026-09-24T09:29:40.600Z")),
                _document(observed_at="2026-09-24T09:31:00.000Z", command={"command": "stop", "activity": {"value": 7}}),
            ],
            state=_control_state(),
            command_answers=[
                _reflected("start", "2026-09-24T09:29:07.646Z"),
                _outcome("stop", result="uncertain", activity=None, stage="acknowledged", end="timed_out"),
                _outcome("stop", result="uncertain", activity=None, stage="acknowledged", end="timed_out"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))
        with _clock(datetime(2026, 9, 24, 9, 29, 40, tzinfo=UTC)):
            await coordinator._async_update_data()
            await coordinator.async_send_mower_command("start")
            bridge.gate = asyncio.Event()
            dock = asyncio.create_task(entity.async_dock())
            await _settle()
            await coordinator._async_update_data()
            bridge.gate.set()
            with pytest.raises(HomeAssistantError, match="did not confirm it"):
                await dock
            assert entity.activity == LawnMowerActivity.RETURNING, "returning was observed after the write"

        with _clock(datetime(2026, 9, 24, 9, 31, tzinfo=UTC)):
            await coordinator._async_update_data()
            assert entity.activity == LawnMowerActivity.RETURNING, "an unknown command block is ignored, not a failed poll"
            bridge.gate = None
            with pytest.raises(HomeAssistantError, match="did not confirm it"):
                await entity.async_dock()
            assert entity.activity is None, "evidence from before this command is gone after an uncertain answer"

    _run(scenario, tmp_path)


def test_session_history_in_bridge_mode_follows_confirmed_commands(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document()],
            state=_control_state(),
            command_answers=[
                _reflected("start", "2026-09-24T08:31:19.442Z"),
                _reflected("stop", "2026-09-24T08:36:20.993Z", activity=None, payload="map_saving"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        store = SessionStore(hass, "test-entry")
        coordinator.session_store = store
        history = store.history
        with _clock(datetime(2026, 9, 24, 8, 37, tzinfo=UTC)):
            await coordinator._async_update_data()
            assert history.current is None, "a missing status starts nothing"
            await coordinator.async_send_mower_command("start")
            assert history.current is not None
            assert history.current["phase"] == "mowing"
            assert history.current["started_at"] == datetime(2026, 9, 24, 8, 31, 19, 442000, tzinfo=UTC).isoformat()
            await coordinator.async_send_mower_command("dock")
        assert history.current is None
        assert len(history.recent) == 1
        session = history.recent[0]
        assert session["ended_at"] == datetime(2026, 9, 24, 8, 36, 20, 993000, tzinfo=UTC).isoformat()
        assert session["end_observed"] is False, "five minutes without an observation is a gap, not an observed end"
        assert session["observation_gap"] is True

    _run(scenario, tmp_path)


def test_bridge_backend_fails_the_update_on_stale_data_errors_or_bad_documents(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [
                _document(),
                _document(stale=True, error="mower_local_unreachable", age_ms=30_000),
                BridgeClientError("cannot_connect"),
                BridgeClientError("authentication_required", 503),
                {"contract": 1, "id": MOWER_ID, "source": "local-tuya-3.5"},
                _document(observed_at="2026-09-19T10:05:00Z", battery={"state": "missing", "dp": ["8"]}),
            ]
        )
        coordinator = EufyMowerCoordinator(
            hass,
            host="192.0.2.1",
            device_id="synthetic-device",
            local_key="not-a-real-local-key",
            backend=BACKEND_BRIDGE,
            bridge=cast(BridgeClient, bridge),
            bridge_mower_id=MOWER_ID,
        )
        await coordinator._async_update_data()
        first_observed = coordinator.last_local_update

        with pytest.raises(UpdateFailed, match="mower_local_unreachable"):
            await coordinator._async_update_data()
        assert coordinator.bridge_error == "mower_local_unreachable"
        assert coordinator.last_local_update == first_observed, "stale data never refreshes the timestamp"

        with pytest.raises(UpdateFailed, match="cannot_connect"):
            await coordinator._async_update_data()
        with pytest.raises(UpdateFailed, match="authentication_required"):
            await coordinator._async_update_data()
        with pytest.raises(UpdateFailed, match="invalid_document"):
            await coordinator._async_update_data()
        assert coordinator.last_local_update == first_observed

        data = await coordinator._async_update_data()
        assert data == {"134": "Wifi", "109": 70}, "a missing field is absent, never carried forward"
        assert coordinator.bridge_error is None
        assert coordinator.last_local_update == datetime(2026, 9, 19, 10, 5, tzinfo=UTC)

    _run(scenario, tmp_path)


def test_bridge_backend_reports_the_confirmed_activity_and_keeps_missing_and_invalid_explicit(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        later = "2026-09-19T10:00:11.250Z"
        bridge = _FakeBridge(
            [
                _document(status=_reported_status("mowing")),
                _document(status=_reported_status("paused"), observed_at=later),
                _document(status=_reported_status("returning")),
                _document(status={"state": "invalid", "dp": STATUS_DP}),
                _document(status={"state": "missing", "dp": STATUS_DP}),
                _document(status=_reported_status("mowing"), stale=True, error="mower_local_unreachable", age_ms=30_000),
            ]
        )
        coordinator = EufyMowerCoordinator(
            hass,
            host="192.0.2.1",
            device_id="synthetic-device",
            local_key="not-a-real-local-key",
            backend=BACKEND_BRIDGE,
            bridge=cast(BridgeClient, bridge),
            bridge_mower_id=MOWER_ID,
        )
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry(**{CONF_BACKEND: BACKEND_BRIDGE})))

        for expected, observed in (
            (LawnMowerActivity.MOWING, datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC)),
            (LawnMowerActivity.PAUSED, datetime(2026, 9, 19, 10, 0, 11, 250000, tzinfo=UTC)),
            (LawnMowerActivity.RETURNING, datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC)),
        ):
            data = await coordinator._async_update_data()
            assert data == {"8": 85, "134": "Wifi", "109": 70}, "the activity is typed state, never a raw data point"
            assert coordinator.bridge_status == "reported"
            assert entity.activity == expected
            assert coordinator.last_local_update == observed, "freshness is the library's observation time"
            attributes = entity.extra_state_attributes
            assert attributes["bridge_status"] == "reported"
            assert attributes["bridge_activity"] == expected.value
            assert attributes["bridge_error"] is None

        await coordinator._async_update_data()
        assert coordinator.bridge_status == "invalid"
        assert coordinator.bridge_activity is None
        assert entity.activity is None, "an invalid report never keeps the earlier activity"
        assert entity.extra_state_attributes["bridge_status"] == "invalid"

        await coordinator._async_update_data()
        assert coordinator.bridge_status == "missing"
        assert entity.activity is None, "an absent DP 107 is never an activity"
        assert entity.extra_state_attributes["bridge_status"] == "missing"

        with pytest.raises(UpdateFailed, match="mower_local_unreachable"):
            await coordinator._async_update_data()
        assert coordinator.bridge_status == "missing", "a stale document changes nothing"
        assert coordinator.bridge_activity is None
        assert bridge.requested == [MOWER_ID] * 6

    _run(scenario, tmp_path)


def test_bridge_backend_requires_a_client_and_a_mower_id(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        with pytest.raises(ValueError):
            EufyMowerCoordinator(
                hass,
                host="192.0.2.1",
                device_id="synthetic-device",
                local_key="not-a-real-local-key",
                backend=BACKEND_BRIDGE,
            )

    _run(scenario, tmp_path)


def _bridge_coordinator(**attributes: Any) -> EufyMowerCoordinator:
    # Built without __init__, so the class defaults apply: no bridge control reported yet.
    coordinator = object.__new__(EufyMowerCoordinator)
    coordinator.operating_mode = OPERATING_MODE_CONTROL
    coordinator.backend = BACKEND_BRIDGE
    coordinator.local_dps = {}
    coordinator.command = None
    coordinator.last_local_update = datetime(2026, 9, 19, 10, 0, 1, tzinfo=UTC)
    coordinator.data = {"8": 85, "134": "Wifi", "109": 70}
    for name, value in attributes.items():
        setattr(coordinator, name, value)
    return coordinator


def test_mower_entity_keeps_its_identity_and_exposes_every_control_only_with_the_bridge_opt_in() -> None:
    entry = _entry(**{CONF_BACKEND: BACKEND_BRIDGE})
    coordinator = _bridge_coordinator(bridge_status="missing", bridge_activity=None, bridge_error=None)
    entity = EufyRobomowEntity(coordinator, cast(Any, entry))
    assert entity.unique_id == "synthetic-device_mower"
    assert entity.supported_features == LawnMowerEntityFeature(0), "no control before the bridge reports routes.control"
    assert entity.activity is None, "a missing status stays unknown"
    attributes = entity.extra_state_attributes
    assert attributes["backend"] == BACKEND_BRIDGE
    assert attributes["telemetry_updated_at"] == datetime(2026, 9, 19, 10, 0, 1, tzinfo=UTC)
    assert attributes["bridge_status"] == "missing"
    assert attributes["bridge_activity"] is None
    assert attributes["bridge_error"] is None
    assert attributes["bridge_control"] is None

    coordinator.bridge_routes_control = True
    coordinator.bridge_control = dict(CONTROL_BLOCK)
    assert entity.supported_features == ALL_CONTROLS, "start, pause, resume and dock have a route, dock through stop"
    assert entity.extra_state_attributes["bridge_control"] == CONTROL_BLOCK
    coordinator.operating_mode = OPERATING_MODE_OBSERVE_ONLY
    assert entity.supported_features == LawnMowerEntityFeature(0), "observe-only hides every control"
    coordinator.operating_mode = OPERATING_MODE_CONTROL

    for reported, expected in (
        ("mowing", LawnMowerActivity.MOWING),
        ("paused", LawnMowerActivity.PAUSED),
        ("returning", LawnMowerActivity.RETURNING),
        ("docked", LawnMowerActivity.DOCKED),
        ("charging", LawnMowerActivity.DOCKED),
        ("error", LawnMowerActivity.ERROR),
        ("unknown", None),
    ):
        coordinator.bridge_status = "reported"
        coordinator.bridge_activity = reported
        assert entity.activity == expected, reported

    local = _bridge_coordinator(backend=BACKEND_LOCAL)
    assert EufyRobomowEntity(local, cast(Any, entry)).unique_id == "synthetic-device_mower"
    assert EufyRobomowEntity(local, cast(Any, entry)).supported_features == ALL_CONTROLS
    assert "bridge_control" not in EufyRobomowEntity(local, cast(Any, entry)).extra_state_attributes
    local.operating_mode = OPERATING_MODE_OBSERVE_ONLY
    assert EufyRobomowEntity(local, cast(Any, entry)).supported_features == LawnMowerEntityFeature(0)


def test_sensors_keep_their_unique_ids_and_read_bridge_values() -> None:
    entry = _entry(**{CONF_BACKEND: BACKEND_BRIDGE})
    coordinator = _bridge_coordinator()
    by_key = {description.key: description for description in SENSORS}
    battery = EufySensor(coordinator, cast(Any, entry), by_key["battery"])
    signal = EufySensor(coordinator, cast(Any, entry), by_key["signal"])
    network = EufySensor(coordinator, cast(Any, entry), by_key["network"])
    assert (battery.unique_id, signal.unique_id, network.unique_id) == (
        "synthetic-device_battery",
        "synthetic-device_signal",
        "synthetic-device_network",
    )
    assert battery.native_value == 85
    assert signal.native_value == 70, "the device-declared percentage, never negated"
    assert network.native_value == "Wifi"


class _RecordingCoordinator:
    instances: list[_RecordingCoordinator] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.data: dict[str, object] = {}
        _RecordingCoordinator.instances.append(self)

    async def async_config_entry_first_refresh(self) -> None:
        pass


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: SimpleNamespace) -> bool:
    monkeypatch.setattr(integration, "EufyMowerCoordinator", _RecordingCoordinator)
    monkeypatch.setattr(
        integration, "SessionStore", lambda *args: SimpleNamespace(async_load=AsyncMock())
    )
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
    )
    return asyncio.run(integration.async_setup_entry(cast(HomeAssistant, hass), cast(Any, entry)))


def test_setup_with_the_bridge_backend_creates_no_cloud_client_and_logs_no_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    _RecordingCoordinator.instances.clear()
    entry = _entry(
        **{
            CONF_BACKEND: BACKEND_BRIDGE,
            CONF_BRIDGE_URL: "http://127.0.0.1:8090",
            CONF_BRIDGE_TOKEN: TOKEN,
            CONF_BRIDGE_MOWER_ID: MOWER_ID,
            CONF_OPERATING_MODE: OPERATING_MODE_CONTROL,
        }
    )
    with caplog.at_level(logging.DEBUG, logger=integration.__name__):
        assert _setup(monkeypatch, tmp_path, entry) is True
    coordinator = _RecordingCoordinator.instances[-1]
    assert coordinator.kwargs["backend"] == BACKEND_BRIDGE
    assert coordinator.kwargs["cloud_client"] is None
    assert isinstance(coordinator.kwargs["bridge"], BridgeClient)
    assert coordinator.kwargs["bridge_mower_id"] == MOWER_ID
    assert "Cloud client created" not in caplog.text
    assert TOKEN not in caplog.text
    assert "synthetic-device" not in caplog.text


def test_setup_with_the_local_backend_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _RecordingCoordinator.instances.clear()
    monkeypatch.setattr(
        "custom_components.eufy_robomow.cloud.EufyCloudClient",
        lambda **kwargs: SimpleNamespace(kwargs=kwargs),
    )
    assert _setup(monkeypatch, tmp_path, _entry()) is True
    coordinator = _RecordingCoordinator.instances[-1]
    assert coordinator.kwargs["backend"] == BACKEND_LOCAL
    assert coordinator.kwargs["bridge"] is None
    assert coordinator.kwargs["cloud_client"] is not None


@pytest.mark.parametrize(
    "options",
    [
        {CONF_BACKEND: "cloud"},
        {CONF_BACKEND: BACKEND_BRIDGE},
        {CONF_BACKEND: BACKEND_BRIDGE, CONF_BRIDGE_URL: "http://127.0.0.1:8090", CONF_BRIDGE_TOKEN: TOKEN},
        {CONF_BACKEND: BACKEND_BRIDGE, CONF_BRIDGE_URL: "ftp://x", CONF_BRIDGE_TOKEN: TOKEN, CONF_BRIDGE_MOWER_ID: MOWER_ID},
    ],
)
def test_setup_refuses_incomplete_bridge_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, options: dict[str, str]
) -> None:
    with pytest.raises(ConfigEntryError):
        _setup(monkeypatch, tmp_path, _entry(**options))


# ── Settings through the bridge, bridge 0.10.0 ────────────────────────────────


def _setting(value: Any, writable: bool = True) -> dict[str, Any]:
    return {"state": "reported", "value": value, "writable": writable}


SETTINGS_BLOCK: dict[str, Any] = {
    "mow_height": {**_setting(40), "min": 25, "max": 75, "step": 1, "unit": "mm"},
    "volume": {**_setting(20), "min": 0, "max": 100, "step": 1, "unit": "%"},
    "smart_no_go_zones": _setting(True),
    "sparse_lawn_optimization": _setting(False),
    "rain_auto_return": _setting(True, writable=False),
    "child_lock": _setting(True, writable=False),
    "bird_view_capture": _setting(False, writable=False),
}


def _settings_state(**routes: Any) -> dict[str, Any]:
    return _bridge_state(
        settings_mode="write",
        routes={"discovery": True, "state": True, "control": False, "maps": False, "settings": True, **routes},
    )


def _setting_outcome(key: str, dp: str, code: str, value: Any, previous: Any, result: str = "confirmed") -> dict[str, Any]:
    """A contract 1 setting answer. Raw data points are never part of it."""
    confirmed = result == "confirmed"
    return {
        "contract": 1,
        "id": MOWER_ID,
        "setting": key,
        "result": result,
        "write": {"dp": dp, "code": code, "value": value},
        "previous": previous,
        "sent_at": "2026-09-25T09:00:00.100Z",
        "stage": "reflected" if confirmed else "sent",
        "end": "reflected" if confirmed else ("rejected" if result == "failed" else "timed_out"),
        "before_observed_at": "2026-09-25T09:00:00.050Z",
        "reply": {"observed_at": "2026-09-25T09:00:00.120Z", "return_code_zero": True, "rejected": result == "failed"},
        "reflection": {"observed_at": "2026-09-25T09:00:00.600Z", "sequence": 71, "value": value} if confirmed else None,
        "other": None,
        "reports": 1 if confirmed else 0,
    }


def test_bridge_settings_are_read_from_the_state_document_and_written_once(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document(settings=SETTINGS_BLOCK) for _ in range(3)],
            state=_settings_state(),
            setting_answers=[
                _setting_outcome("mow_height", "110", "mow_height", 45, 40),
                _setting_outcome("smart_no_go_zones", "132", "enable_smart_forbid_zone", False, True),
                _setting_outcome("mow_height", "110", "mow_height", 40, 45),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        data = await coordinator._async_update_data()
        assert data == {
            "8": 85, "134": "Wifi", "109": 70,
            "110": 40, "26": 20, "132": True, "141": False, "101": True, "47": True, "133": False,
        }, "the local setting data points come from the same bridge query"
        assert coordinator.bridge_routes_settings is True
        assert coordinator.bridge_routes_control is False, "settings writes need no control mode on the bridge"
        assert coordinator.bridge_settings_writable["110"] is True
        assert coordinator.bridge_settings_writable["101"] is False
        # Set as the coordinator's own refresh would, which the harness records instead.
        coordinator.data = data

        await coordinator.async_send_command("110", 45)
        assert coordinator.data["110"] == 45, "the confirmed value shows before the debounced refresh"
        await coordinator.async_send_command("132", False)
        assert coordinator.data["132"] is False
        # A restore is a second, deliberate write with its own fresh query behind the bridge.
        await coordinator.async_send_command("110", 40)
        assert coordinator.data["110"] == 40, "a restore within the refresh cooldown shows at once"
        assert coordinator.data["26"] == 20, "only the written setting changes"
        assert bridge.settings == [
            (MOWER_ID, "mow_height", 45),
            (MOWER_ID, "smart_no_go_zones", False),
            (MOWER_ID, "mow_height", 40),
        ]
        assert cast(AsyncMock, coordinator.async_request_refresh).await_count == 3
        assert bridge.commands == []

        # Rain stop, child protection and the real lawn map are refused before any request.
        for dp in ("101", "47", "133"):
            for value in (False, True):
                with pytest.raises(HomeAssistantError, match="read only"):
                    await coordinator.async_send_command(dp, value)
        with pytest.raises(HomeAssistantError, match="no route"):
            await coordinator.async_send_command("26x", 1)
        assert len(bridge.settings) == 3, "no refused setting reached the bridge"

    _run(scenario, tmp_path)


def test_bridge_settings_need_both_opt_ins_and_a_writable_declaration(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        not_writable = {**SETTINGS_BLOCK, "mow_height": _setting(40, writable=False)}
        bridge = _FakeBridge(
            [_document(settings=SETTINGS_BLOCK), _document(settings=not_writable), _document(settings=SETTINGS_BLOCK)],
            state=_bridge_state(),
        )
        coordinator = _coordinator(hass, bridge)
        await coordinator._async_update_data()
        with pytest.raises(HomeAssistantError, match="does not accept setting writes"):
            await coordinator.async_send_command("110", 45)
        bridge.state = _settings_state()
        await coordinator._async_update_data()
        with pytest.raises(HomeAssistantError, match="not report this setting as writable"):
            await coordinator.async_send_command("110", 45)
        await coordinator._async_update_data()
        observing = _coordinator(hass, bridge, operating_mode=OPERATING_MODE_OBSERVE_ONLY)
        observing.bridge_routes_settings = True
        observing.bridge_settings_writable = {"110": True}
        assert observing.setting_entities_available is False
        with pytest.raises(HomeAssistantError, match="observe-only"):
            await observing.async_send_command("110", 45)
        bridge.state = BridgeClientError("cannot_connect")
        bridge.answers.append(_document(settings=SETTINGS_BLOCK))
        await coordinator._async_update_data()
        assert coordinator.bridge_routes_settings is False, "a failed bridge state query closes the route"
        assert bridge.settings == []

    _run(scenario, tmp_path)


def test_bridge_setting_outcomes_and_failures_are_never_repeated(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document(settings=SETTINGS_BLOCK)],
            state=_settings_state(),
            setting_answers=[
                _setting_outcome("volume", "26", "volume_set", 30, 20, result="failed"),
                _setting_outcome("volume", "26", "volume_set", 30, 20, result="uncertain"),
                BridgeClientError("mower_setting_already_set", 409),
                BridgeClientError("timeout"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        coordinator.data = await coordinator._async_update_data()
        for match in ("rejected the setting", "did not report it", "refused the setting before writing it", "may have been written"):
            with pytest.raises(HomeAssistantError, match=match):
                await coordinator.async_send_command("26", 30)
        assert bridge.settings == [(MOWER_ID, "volume", 30)] * 4, "each write reached the bridge exactly once"
        assert coordinator.data["26"] == 20, "an unconfirmed write never changes the shown value"
        assert cast(AsyncMock, coordinator.async_request_refresh).await_count == 4

    _run(scenario, tmp_path)


WORK_PARAMETERS_BLOCK: dict[str, Any] = {
    "state": "reported",
    "source": "cloud",
    "observed_at": "2026-09-25T08:59:00.000Z",
    "error": None,
    "mow_speed": {"value": "medium", "writable": True, "options": ["low", "medium", "adaptive_high"]},
    "blade_speed": {"value": "medium", "writable": True, "options": ["low", "medium", "high"]},
    "edge_distance": 120,
    "mow_spacing": 80,
    "direction": {"mode": "single", "single_angle": 90, "current_angle": 90},
}


def _work_parameter_outcome(key: str, field: int, value: str, previous: str, result: str = "confirmed") -> dict[str, Any]:
    """A contract 1 work parameter answer of bridge 0.11.0. Raw data points are never part of it."""
    confirmed = result == "confirmed"
    return {
        "contract": 1,
        "id": MOWER_ID,
        "setting": key,
        "result": result,
        "write": {"dp": "155", "code": "reserved_raw_155", "field": field, "value": value},
        "previous": previous,
        "cloud_observed_at": "2026-09-25T08:59:00.000Z",
        "sent_at": "2026-09-25T09:00:00.100Z",
        "stage": "reflected" if confirmed else "sent",
        "end": "reflected" if confirmed else ("rejected" if result == "failed" else "timed_out"),
        "before_observed_at": "2026-09-25T09:00:00.050Z",
        "reply": {"observed_at": "2026-09-25T09:00:00.120Z", "return_code_zero": True, "rejected": result == "failed"},
        "reflection": {"observed_at": "2026-09-25T09:00:00.300Z", "sequence": 81, "value": value} if confirmed else None,
        "other": None,
        "reports": 1 if confirmed else 0,
    }


def test_bridge_work_parameter_speeds_are_written_once_and_shown_at_once(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        bridge = _FakeBridge(
            [_document(settings=SETTINGS_BLOCK, work_parameters=WORK_PARAMETERS_BLOCK) for _ in range(2)],
            state=_settings_state(),
            work_parameter_answers=[
                _work_parameter_outcome("mow_speed", 2, "adaptive_high", "medium"),
                _work_parameter_outcome("blade_speed", 6, "low", "medium"),
                _work_parameter_outcome("blade_speed", 6, "medium", "low", result="failed"),
                _work_parameter_outcome("blade_speed", 6, "medium", "low", result="uncertain"),
                BridgeClientError("mower_setting_evidence_missing", 409),
                BridgeClientError("timeout"),
            ],
        )
        coordinator = _coordinator(hass, bridge)
        coordinator.data = await coordinator._async_update_data()
        assert coordinator.data["cloud_travel_speed"] == "normal"
        assert coordinator.data["cloud_pad_direction"] == 90
        assert coordinator.bridge_settings_writable["cloud_travel_speed"] is True

        await coordinator.async_set_cloud_setting(travel_speed="fast")
        assert coordinator.data["cloud_travel_speed"] == "fast", "the confirmed option shows at once"
        await coordinator.async_set_cloud_setting(blade_speed="slow")
        assert coordinator.data["cloud_blade_speed"] == "slow"
        assert bridge.work_parameters == [
            (MOWER_ID, "mow_speed", "adaptive_high"),
            (MOWER_ID, "blade_speed", "low"),
        ], "the library's own names travel to the bridge"

        for match in ("rejected the setting", "did not report it", "refused the setting before writing it", "may have been written"):
            with pytest.raises(HomeAssistantError, match=match):
                await coordinator.async_set_cloud_setting(blade_speed="normal")
        assert coordinator.data["cloud_blade_speed"] == "slow", "an unconfirmed write never changes the shown option"
        assert len(bridge.work_parameters) == 6, "each write reached the bridge exactly once"

        # Edge distance, path distance and pad direction are read only, whatever the value.
        for change in ({"edge_mm": 100}, {"path_mm": 100}, {"pad_direction": 180}):
            with pytest.raises(HomeAssistantError, match="read only through the mower bridge"):
                await coordinator.async_set_cloud_setting(**change)
        with pytest.raises(HomeAssistantError, match="no value for this option"):
            await coordinator.async_set_cloud_setting(travel_speed="turbo")
        with pytest.raises(HomeAssistantError, match="one setting at a time"):
            await coordinator.async_set_cloud_setting(travel_speed="fast", blade_speed="fast")
        # A speed the library cannot restore is not writable, and a closed route refuses first.
        coordinator.bridge_settings_writable["cloud_travel_speed"] = False
        with pytest.raises(HomeAssistantError, match="not report this setting as writable"):
            await coordinator.async_set_cloud_setting(travel_speed="slow")
        coordinator.bridge_routes_settings = False
        with pytest.raises(HomeAssistantError, match="does not accept setting writes"):
            await coordinator.async_set_cloud_setting(blade_speed="fast")
        assert len(bridge.work_parameters) == 6, "no refused write reached the bridge"
        assert bridge.settings == [] and bridge.commands == []

    _run(scenario, tmp_path)


def test_setting_entities_keep_their_unique_ids_and_exist_in_bridge_mode() -> None:
    from custom_components.eufy_robomow import number, select, switch

    entry = _entry(**{CONF_BACKEND: BACKEND_BRIDGE})
    numbers = [
        "synthetic-device_cut_height",
        "synthetic-device_volume",
        "synthetic-device_edge_distance",
        "synthetic-device_pad_direction",
    ]
    selects = ["synthetic-device_path_mm", "synthetic-device_travel_speed", "synthetic-device_blade_speed"]
    for mode, expected_numbers, expected_switches, expected_selects in (
        (OPERATING_MODE_CONTROL, numbers, 5, selects),
        (OPERATING_MODE_OBSERVE_ONLY, [], 0, []),
    ):
        coordinator = _bridge_coordinator(operating_mode=mode, cloud_client=None)
        coordinator.data = {
            "8": 85, "110": 40, "26": 20, "101": True, "47": True, "132": True, "141": False, "133": False,
            "cloud_travel_speed": "normal", "cloud_blade_speed": "fast",
            "cloud_edge_mm": 120, "cloud_path_mm": 80, "cloud_pad_direction": 90,
        }
        hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: coordinator}})
        added: dict[str, list[Any]] = {"number": [], "switch": [], "select": []}
        for name, platform in (("number", number), ("switch", switch), ("select", select)):
            asyncio.run(platform.async_setup_entry(cast(HomeAssistant, hass), cast(Any, entry), added[name].extend))
        assert [entity.unique_id for entity in added["number"]] == expected_numbers
        assert len(added["switch"]) == expected_switches
        assert [entity.unique_id for entity in added["select"]] == expected_selects
        if expected_switches:
            height, volume, edge, pad = added["number"]
            assert (height.native_value, volume.native_value) == (40.0, 20.0)
            assert (edge.native_value, pad.native_value) == (12.0, 0.0), "the same reading as the local backend"
            by_id = {entity.unique_id: entity for entity in added["switch"]}
            assert by_id["synthetic-device_rain_detection"].is_on is True
            assert by_id["synthetic-device_mow_yellow_grass"].is_on is False
            path, travel, blade = added["select"]
            assert (path.current_option, travel.current_option, blade.current_option) == ("8 cm", "normal", "fast")