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
# The control opt-in bridge 0.5.0 reports in control mode. The stop route text is never served.
CONTROL_BLOCK = {"classes": ["start", "pause", "resume"], "max_state_age_ms": 30000, "read_back_ms": 20000}
START_AND_PAUSE = LawnMowerEntityFeature.START_MOWING | LawnMowerEntityFeature.PAUSE
ALL_CONTROLS = START_AND_PAUSE | LawnMowerEntityFeature.DOCK


def _at(seconds: int) -> str:
    return f"2026-09-19T10:00:{1 + seconds:02d}.250Z"


def _bridge_state(**overrides: Any) -> dict[str, Any]:
    """A bridge state document. The default is the bridge's own observe_only mode."""
    state: dict[str, Any] = {
        "protocol": 1,
        "bridge": "eufy-robomow-bridge",
        "version": "0.5.0",
        "bridge_id": "00000000-0000-4000-8000-000000000000",
        "lifecycle": "running",
        "operating_mode": "observe_only",
        "auth": {"state": "authenticated", "last_error": None, "attempted_at": OBSERVED_AT},
        "client": {"package": "@keesmod/eufy-mega-client", "version": "0.16.0", "module": "mowers", "lifecycle": "open", "connected": True},
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


def _outcome(command: str, result: str = "confirmed", activity: str | None = "mowing", stage: str = "reflected", end: str = "reflected") -> dict[str, Any]:
    """A contract 1 command answer. Raw data points are never part of it."""
    return {
        "contract": 1,
        "id": MOWER_ID,
        "command": command,
        "result": result,
        "write": {"dp": "1", "code": "switch_go", "value": True},
        "sent_at": "2026-09-19T16:42:38.199Z",
        "stage": stage,
        "end": end,
        "before_observed_at": "2026-09-19T16:42:38.198Z",
        "reply": {"observed_at": "2026-09-19T16:42:38.202Z", "return_code_zero": True, "rejected": result == "failed"},
        "acknowledgement": {"observed_at": "2026-09-19T16:42:39.150Z", "sequence": 63859, "dp": "1"},
        "activity": {"observed_at": "2026-09-19T16:42:39.351Z", "sequence": 63860, "value": activity} if activity else None,
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

    def __init__(self, answers: list[Any], state: Any = None, command_answers: list[Any] | None = None) -> None:
        self.answers = answers
        self.requested: list[str] = []
        self.state = _bridge_state() if state is None else state
        self.state_requests = 0
        self.command_answers = command_answers or []
        self.commands: list[tuple[str, str]] = []
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
        assert coordinator.writes_available is False, "settings have no bridge route"
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
        for write in (
            coordinator.async_send_command("110", 40),
            coordinator.async_set_cloud_setting(edge_mm=10),
        ):
            with pytest.raises(HomeAssistantError, match="bridge backend"):
                await write
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
        assert coordinator.writes_available is False, "settings still have no bridge route"
        assert entity.supported_features == START_AND_PAUSE
        assert not entity.supported_features & LawnMowerEntityFeature.DOCK, "the bridge has no return route"
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
        with pytest.raises(HomeAssistantError, match="no return route"):
            await coordinator.async_send_mower_command("dock")
        with pytest.raises(HomeAssistantError, match="no return route"):
            await entity.async_dock()
        with pytest.raises(HomeAssistantError, match="Unsupported"):
            await coordinator.async_send_mower_command("stop")

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


def test_mower_entity_keeps_its_identity_and_exposes_start_and_pause_only_with_the_bridge_opt_in() -> None:
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
    assert entity.supported_features == START_AND_PAUSE, "start, pause and resume have a route, return has none"
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
