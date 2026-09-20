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
    DP_PAUSED,
    DP_PROGRESS,
    RETURNING_THRESHOLD,
)
from .coordinator import EufyMowerCoordinator
from .telemetry import task_active

_LOGGER = logging.getLogger(__name__)

# The library's typed activity, only when it is reported from a confirmed
# definition. Nothing here is inferred from age, absence or inactivity. With
# library 0.15.0 the E15 registry confirms only mowing, paused and returning.
# No E15 payload identifies docked, charging, idle or error yet, so those
# rows wait for a confirmed definition and a missing or invalid status field
# leaves the activity unknown.
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
            reported = self.coordinator.bridge_activity
            return BRIDGE_ACTIVITIES.get(reported) if reported else None
        dps = self.coordinator.local_dps
        dp1 = task_active(dps)
        if type(dp1) is not bool:
            return None
        dp2 = dps.get(DP_PAUSED, False)
        dp118 = dps.get(DP_PROGRESS, 0)

        # Paused: task active but movement stopped
        if dp1 and dp2:
            return LawnMowerActivity.PAUSED

        if dp1 and not dp2:
            # DP118 5–99 → mower returning to base
            # DP118=100 while DP1=True means briefly docked mid-session for
            # charging (will resume); treat as MOWING, not RETURNING.
            if RETURNING_THRESHOLD <= dp118 < 100:
                try:
                    return LawnMowerActivity.RETURNING
                except AttributeError:
                    return LawnMowerActivity.MOWING
            return LawnMowerActivity.MOWING

        # DP1 absent or False → no active session → docked / idle
        return LawnMowerActivity.DOCKED

    @property
    def extra_state_attributes(self) -> dict:
        attributes = {
            "operating_mode": self.coordinator.operating_mode,
            "backend": self.coordinator.backend,
            "telemetry_updated_at": self.coordinator.last_local_update,
            "command": self.coordinator.command.as_dict() if self.coordinator.command else None,
        }
        if self.coordinator.backend == BACKEND_BRIDGE:
            attributes["bridge_status"] = self.coordinator.bridge_status
            attributes["bridge_activity"] = self.coordinator.bridge_activity
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
