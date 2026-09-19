"""Regression tests for sensor units that follow the mower's own declarations."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from homeassistant.const import PERCENTAGE

from custom_components.eufy_robomow.const import CONF_DEVICE_ID, DP_SIGNAL
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.sensor import SENSORS, EufySensor


def _coordinator(data: dict[str, Any]) -> EufyMowerCoordinator:
    coordinator = object.__new__(EufyMowerCoordinator)
    coordinator.data = data
    coordinator.last_local_update = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    return coordinator


def _signal_sensor(data: dict[str, Any]) -> EufySensor:
    description = next(entry for entry in SENSORS if entry.dp == DP_SIGNAL)
    entry = SimpleNamespace(data={CONF_DEVICE_ID: "synthetic-device"})
    return EufySensor(_coordinator(data), cast(Any, entry), description)


def test_signal_strength_is_the_declared_percentage_without_sign_change() -> None:
    """DP 109 is wifi_signal_strength, integer 0 to 100, unit %, as the E15 declares it."""
    sensor = _signal_sensor({DP_SIGNAL: 68})
    assert sensor.native_value == 68
    assert sensor.native_unit_of_measurement == PERCENTAGE
    assert sensor.device_class is None, "a percentage is not a signal_strength (dBm) reading"
    assert sensor.state_class == "measurement"
    assert sensor.unique_id == "synthetic-device_signal", "the entity keeps its identity"
    assert _signal_sensor({}).native_value is None
    assert _signal_sensor({DP_SIGNAL: 0}).native_value == 0
