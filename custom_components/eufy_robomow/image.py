"""Optional read-only E15 map image."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from functools import partial
import logging
from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    BACKEND_BRIDGE,
    BACKEND_LOCAL,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_MAP_CERTIFICATE_FINGERPRINT,
    CONF_MAP_SOURCE,
    CONF_MAP_SOURCE_URL,
    DEFAULT_MAP_SOURCE,
    DOMAIN,
    DP_TASK_ACTIVE,
    MAP_SOURCE_BRIDGE,
)
from .coordinator import EufyMowerCoordinator
from .map import merge_live_snapshots
from .map_renderer import MAP_CONTENT_TYPE, render_map_svg
from .map_source import (
    BRIDGE_CACHE_FILENAME,
    EXTERNAL_CACHE_FILENAME,
    MAP_CACHE_DIRECTORY,
    MAP_STREAM_REFRESH_INTERVAL,
    LoadedMap,
    MapSource,
    MapSourceError,
    MapSourceSettings,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = MAP_STREAM_REFRESH_INTERVAL


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the map only when a source is selected and configured."""
    coordinator: EufyMowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    source = map_source_for_entry(
        hass, entry, coordinator, Path(hass.config.path(MAP_CACHE_DIRECTORY))
    )
    if source is None:
        return
    async_add_entities(
        [EufyRobomowMapImage(hass, coordinator, source, entry)],
        update_before_add=True,
    )


def map_source_for_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EufyMowerCoordinator,
    cache_root: Path,
) -> MapSource | None:
    """The one map source this entry selects, or ``None`` without a map.

    ``external`` is the map source URL, unchanged and the manual recovery path.
    ``bridge`` is the mower bridge's read-only map route and needs the bridge
    backend. Each source binds its bundles to its own mower id and keeps its own
    last-good cache file, so switching never mixes or discards the other's map.
    """
    if entry.options.get(CONF_MAP_SOURCE, DEFAULT_MAP_SOURCE) == MAP_SOURCE_BRIDGE:
        bridge = coordinator.bridge
        mower_id = coordinator.bridge_mower_id
        if coordinator.backend != BACKEND_BRIDGE or bridge is None or mower_id is None:
            _LOGGER.warning(
                "The bridge map source needs the bridge backend, so no map entity is created"
            )
            return None
        return MapSource(
            hass,
            settings=MapSourceSettings.for_bridge(bridge.settings, mower_id),
            device_id=mower_id,
            cache_file=cache_root / entry.entry_id / BRIDGE_CACHE_FILENAME,
            cache_root=cache_root,
        )
    settings = MapSourceSettings.from_values(
        base_url=entry.options.get(CONF_MAP_SOURCE_URL, ""),
        certificate_fingerprint=entry.options.get(
            CONF_MAP_CERTIFICATE_FINGERPRINT,
            "",
        ),
        local_key=entry.data[CONF_LOCAL_KEY],
    )
    if settings is None:
        return None
    return MapSource(
        hass,
        settings=settings,
        device_id=entry.data[CONF_DEVICE_ID],
        cache_file=cache_root / entry.entry_id / EXTERNAL_CACHE_FILENAME,
        cache_root=cache_root,
    )


class EufyRobomowMapImage(ImageEntity):
    """Expose the latest validated E15 map as an SVG image."""

    _attr_has_entity_name = True
    # ImageEntity defaults to no polling; our source needs the platform timer.
    _attr_should_poll = True
    _attr_translation_key = "map"
    _attr_icon = "mdi:map"
    _attr_content_type = MAP_CONTENT_TYPE

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EufyMowerCoordinator,
        source: MapSource,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(hass)
        self._hass = hass
        self._coordinator = coordinator
        self._source = source
        self._content: bytes | None = None
        self._render_key: tuple[str, bool] | None = None
        self._live_started_at: datetime | None = None
        self._live_map: LoadedMap | None = None
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_map"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name="Eufy Robomow E15",
            manufacturer="Eufy (Anker)",
            model="E15",
        )

    @property
    def available(self) -> bool:
        """Return whether a valid current or cached map is available."""
        return self._content is not None

    def image(self) -> bytes | None:
        """Return the current rendered map."""
        return self._content

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose bounded, secret-safe acquisition health."""
        status = self._source.status
        return {
            "acquisition_status": status.state,
            "acquisition_last_success": status.last_success,
            "acquisition_last_error": status.last_error,
        }

    def _task_active(self) -> bool:
        """Whether a mowing task runs, so the map follows it live.

        The local backend reads DP 1. The bridge backend has no DP 1 and uses the
        bridge's reported activity instead, never its age or absence.
        """
        if getattr(self._coordinator, "backend", BACKEND_LOCAL) == BACKEND_BRIDGE:
            return self._coordinator.bridge_task_active
        return bool(self._coordinator.data.get(DP_TASK_ACTIVE, False))

    async def async_update(self) -> None:
        """Fetch and render the latest valid map."""
        include_cleaned_paths = self._task_active()
        if include_cleaned_paths:
            if self._live_started_at is None:
                # Also start a new window after an entity reload. Without a
                # task identifier, an earlier capture cannot seed this task.
                self._live_started_at = dt_util.utcnow()
                self._render_key = None
        else:
            # Reset before fetching, even when the idle refresh fails.
            self._live_started_at = None
            self._live_map = None
        try:
            loaded = await self._source.async_refresh(
                streaming=include_cleaned_paths,
            )
        except MapSourceError as exc:
            _LOGGER.debug("E15 map is not available yet: %s", exc)
            return

        if include_cleaned_paths:
            if (
                self._source.status.state == "healthy"
                and self._live_started_at is not None
                and self._live_started_at <= loaded.captured_at <= dt_util.utcnow()
                and (
                    self._live_map is None
                    or loaded.captured_at >= self._live_map.captured_at
                )
            ):
                snapshot = (
                    merge_live_snapshots(self._live_map.snapshot, loaded.snapshot)
                    if self._live_map is not None
                    else loaded.snapshot
                )
                self._live_map = replace(loaded, snapshot=snapshot)
            # A stream request may first return an old cache or a 304. Keep its
            # map visible, but only fresh captures contribute live paths/pose.
            loaded = self._live_map or loaded
            include_cleaned_paths = self._live_map is not None

        render_key = (loaded.snapshot_id, include_cleaned_paths)
        if render_key == self._render_key:
            return

        self._content = await self._hass.async_add_executor_job(
            partial(
                render_map_svg,
                loaded.snapshot,
                include_cleaned_paths=include_cleaned_paths,
            )
        )
        self._render_key = render_key
        self._attr_image_last_updated = loaded.captured_at
        self.async_update_token()
