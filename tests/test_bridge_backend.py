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
)
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.lawn_mower import EufyRobomowEntity
from custom_components.eufy_robomow.sensor import SENSORS, EufySensor

TOKEN = "synthetic-bridge-token-0123456789abcdef"
MOWER_ID = "c" * 64
OBSERVED_AT = "2026-09-19T10:00:01.250Z"


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "contract": 1,
        "id": MOWER_ID,
        "source": "local-tuya-3.5",
        "observed_at": OBSERVED_AT,
        "age_ms": 12,
        "stale": False,
        "error": None,
        "status": {"state": "unconfirmed", "level": "observed"},
        "battery": {"state": "reported", "value": {"percent": 85}, "dp": ["8"], "source": "local-tuya-3.5", "observedAt": OBSERVED_AT},
        "progress": {"state": "unconfirmed"},
        "network": {"state": "reported", "value": {"kind": "wifi", "signalPercent": 70}, "dp": ["134", "109"], "source": "local-tuya-3.5", "observedAt": OBSERVED_AT},
    }
    document.update(overrides)
    return document


class _FakeBridge:
    """Stands in for BridgeClient. Answers are documents or errors, in order."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.requested: list[str] = []

    async def async_mower_state(self, mower_id: str) -> dict[str, Any]:
        self.requested.append(mower_id)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


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
        assert coordinator.writes_available is False, "control never reaches the bridge in this step"

        data = await coordinator._async_update_data()
        assert data == {"8": 85, "134": "Wifi", "109": 70}
        assert bridge.requested == [MOWER_ID]
        assert coordinator.last_local_update == datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC)
        assert coordinator.bridge_activity is None
        assert coordinator.bridge_error is None
        assert coordinator.local_dps == {}, "no raw local data points exist in bridge mode"

        for write in (
            coordinator.async_send_mower_command("start"),
            coordinator.async_send_command("110", 40),
            coordinator.async_set_cloud_setting(edge_mm=10),
        ):
            with pytest.raises(HomeAssistantError, match="bridge backend"):
                await write
            device.assert_not_called()
        assert coordinator.command is None, "a refused command never becomes pending"

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


def test_mower_entity_keeps_its_identity_and_exposes_no_control_in_bridge_mode() -> None:
    entry = _entry(**{CONF_BACKEND: BACKEND_BRIDGE})
    coordinator = _bridge_coordinator(bridge_activity=None, bridge_error=None)
    entity = EufyRobomowEntity(coordinator, cast(Any, entry))
    assert entity.unique_id == "synthetic-device_mower"
    assert entity.supported_features == LawnMowerEntityFeature(0)
    assert entity.activity is None, "an unconfirmed status stays unknown"
    attributes = entity.extra_state_attributes
    assert attributes["backend"] == BACKEND_BRIDGE
    assert attributes["telemetry_updated_at"] == datetime(2026, 9, 19, 10, 0, 1, tzinfo=UTC)
    assert attributes["bridge_activity"] is None
    assert attributes["bridge_error"] is None

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
    assert EufyRobomowEntity(local, cast(Any, entry)).supported_features != LawnMowerEntityFeature(0)


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
    assert signal.native_value == -70, "the same raw device value the local backend reads"
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
