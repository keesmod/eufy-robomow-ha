"""Lawn mower entity for Eufy Robomow."""

from __future__ import annotations

import logging

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    BACKEND_BRIDGE,
    CONF_DEVICE_ID,
)
from .coordinator import EufyMowerCoordinator

_LOGGER = logging.getLogger(__name__)

# The local backend's readings, see telemetry.read_local_activity: the local
# status reply with DP 107 from a cloud poll taken since it took its shape.
LOCAL_ACTIVITIES: dict[str, LawnMowerActivity] = {
    "mowing": LawnMowerActivity.MOWING,
    "paused": LawnMowerActivity.PAUSED,
    "returning": LawnMowerActivity.RETURNING,
    "docked": LawnMowerActivity.DOCKED,
}

# The library's typed activity, only when it is reported from a confirmed
# definition. Nothing here is inferred from age, absence or inactivity. Library
# 0.22.0 reads DP 107 as the mower's mission status: every mowing mission reports
# mowing or paused, the recharge mission returning, and a message without a
# mission idle, which reads as docked like the local backend's idle task. No E15
# payload identifies docked, charging or error, and a missing or invalid status
# field leaves the activity unknown.
BRIDGE_ACTIVITIES: dict[str, LawnMowerActivity] = {
    "mowing": LawnMowerActivity.MOWING,
    "paused": LawnMowerActivity.PAUSED,
    "returning": LawnMowerActivity.RETURNING,
    "docked": LawnMowerActivity.DOCKED,
    "charging": LawnMowerActivity.DOCKED,
    "idle": LawnMowerActivity.DOCKED,
    "error": LawnMowerActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EufyMowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EufyRobomowEntity(coordinator, entry)])


class EufyRobomowEntity(CoordinatorEntity[EufyMowerCoordinator], LawnMowerEntity):
    """Represents the Eufy E15 robot mower."""

    _attr_has_entity_name = True
    _attr_translation_key = "lawn_mower"
    _attr_icon = "mdi:robot-mower"
    _CONTROL_FEATURES = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )
    # The bridge routes start, pause, resume and stop. Dock goes through the stop
    # route: on the owned E15 firmware a stop over DP 1 false ends the task and
    # the mower returns to the dock by itself, while the library's return over
    # DP 3 is ignored. The same three entity features as the local backend.
    _BRIDGE_FEATURES = _CONTROL_FEATURES

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_mower"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name="Eufy Robomow E15",
            manufacturer="Eufy (Anker)",
            model="E15",
        )

    @property
    def supported_features(self) -> LawnMowerEntityFeature:
        """Expose controls only after the user explicitly opts in.

        The bridge backend exposes start, pause and dock only while the bridge
        itself reports its control opt-in.
        """
        if not self.coordinator.commands_available:
            return LawnMowerEntityFeature(0)
        if self.coordinator.backend == BACKEND_BRIDGE:
            return self._BRIDGE_FEATURES
        return self._CONTROL_FEATURES

    # ── activity ──────────────────────────────────────────────────────────────

    @property
    def activity(self) -> LawnMowerActivity | None:
        if self.coordinator.backend == BACKEND_BRIDGE:
            # A reported poll or, while recent, the activity a confirmed command
            # reflected. The E15's query replies carry no DP 107, so without the
            # command the activity would stay unknown and resume unreachable.
            evidence = self.coordinator.bridge_activity_evidence
            return BRIDGE_ACTIVITIES.get(evidence[0]) if evidence else None
        # DP 1, DP 2 and DP 118 from the local status reply, and DP 107 from a
        # cloud poll taken since that reply took its shape. DP 107 shows the drive
        # home, which runs with DP 1 false, and the map save at the arrival.
        reading = self.coordinator.local_activity
        return LOCAL_ACTIVITIES.get(reading) if reading else None

    @property
    def extra_state_attributes(self) -> dict:
        attributes = {
            "operating_mode": self.coordinator.operating_mode,
            "backend": self.coordinator.backend,
            "telemetry_updated_at": self.coordinator.last_local_update,
            "command": self.coordinator.command.as_dict() if self.coordinator.command else None,
        }
        if self.coordinator.backend != BACKEND_BRIDGE:
            # DP 107 as the last cloud poll reported it, read with the app's mission
            # status schema: mowing, paused, returning, map_saving, idle or None,
            # and its power mode: running, standby, hibernate or None.
            attributes["robot_status"] = self.coordinator.cloud_robot_status
            attributes["robot_power_mode"] = self.coordinator.cloud_power_mode
        if self.coordinator.backend == BACKEND_BRIDGE:
            attributes["bridge_status"] = self.coordinator.bridge_status
            attributes["bridge_activity"] = self.coordinator.bridge_activity
            evidence = self.coordinator.bridge_activity_evidence
            attributes["bridge_activity_source"] = evidence[1] if evidence else None
            attributes["bridge_activity_observed_at"] = (
                evidence[2].isoformat() if evidence and evidence[2] else None
            )
            attributes["bridge_error"] = self.coordinator.bridge_error
            attributes["bridge_control"] = self.coordinator.bridge_control
        return attributes

    async def async_start_mowing(self) -> None:
        action = "resume" if self.activity == LawnMowerActivity.PAUSED else "start"
        await self.coordinator.async_send_mower_command(action)

    async def async_pause(self) -> None:
        await self.coordinator.async_send_mower_command("pause")

    async def async_dock(self) -> None:
        await self.coordinator.async_send_mower_command("dock")
