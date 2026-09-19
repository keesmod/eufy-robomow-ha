"""Tests for the physical-control safety boundary."""

from __future__ import annotations

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_robomow.const import (
    BACKEND_BRIDGE,
    OPERATING_MODE_CONTROL,
    OPERATING_MODE_OBSERVE_ONLY,
)
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator


def _coordinator_for_mode(operating_mode: str) -> EufyMowerCoordinator:
    coordinator = object.__new__(EufyMowerCoordinator)
    coordinator.operating_mode = operating_mode
    return coordinator


def test_observe_only_rejects_writes() -> None:
    coordinator = _coordinator_for_mode(OPERATING_MODE_OBSERVE_ONLY)

    assert coordinator.control_enabled is False
    with pytest.raises(HomeAssistantError, match="observe-only mode"):
        coordinator._require_control_enabled()


def test_control_mode_allows_writes() -> None:
    coordinator = _coordinator_for_mode(OPERATING_MODE_CONTROL)

    assert coordinator.control_enabled is True
    coordinator._require_control_enabled()


def test_commands_follow_the_mode_the_backend_and_the_bridge_opt_in() -> None:
    local = _coordinator_for_mode(OPERATING_MODE_CONTROL)
    assert local.commands_available is True
    assert local.writes_available is True
    local._require_commands_available()

    bridge = _coordinator_for_mode(OPERATING_MODE_CONTROL)
    bridge.backend = BACKEND_BRIDGE
    assert bridge.writes_available is False, "settings have no bridge route"
    assert bridge.commands_available is False, "the bridge has not reported routes.control"
    with pytest.raises(HomeAssistantError, match="not in control mode"):
        bridge._require_commands_available()
    bridge.bridge_routes_control = True
    assert bridge.commands_available is True
    assert bridge.writes_available is False
    bridge._require_commands_available()
    with pytest.raises(HomeAssistantError, match="no settings routes"):
        bridge._require_writes_available()

    observe_only = _coordinator_for_mode(OPERATING_MODE_OBSERVE_ONLY)
    observe_only.backend = BACKEND_BRIDGE
    observe_only.bridge_routes_control = True
    assert observe_only.commands_available is False, "the bridge's opt-in never overrides observe-only"
    with pytest.raises(HomeAssistantError, match="observe-only mode"):
        observe_only._require_commands_available()
