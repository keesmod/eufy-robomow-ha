"""Tests for live E15 map image behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.eufy_robomow.bridge_client import BridgeCloudStatus
from custom_components.eufy_robomow.const import BACKEND_BRIDGE, DP_TASK_ACTIVE
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.image import EufyRobomowMapImage
from custom_components.eufy_robomow.map import MapSnapshot, Point
from custom_components.eufy_robomow.map_source import LoadedMap, MapSource


def _snapshot(
    *,
    cleaned_paths: tuple[tuple[Point, ...], ...],
    tracking_position: Point,
) -> MapSnapshot:
    return MapSnapshot(
        map_id=539,
        boundary=(Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)),
        base_areas=(),
        no_go_areas=(),
        pathways=(),
        cleaned_paths=cleaned_paths,
        mower_position=Point(5, 50),
        tracking_position=tracking_position,
    )


class _SequenceMapSource:
    def __init__(self, snapshots: list[MapSnapshot]) -> None:
        self._snapshots = snapshots
        self._index = 0
        self.streaming_requests: list[bool] = []
        self.status = SimpleNamespace(
            state="healthy",
            last_success=datetime(2026, 7, 27, tzinfo=UTC),
            last_error=None,
        )

    async def async_refresh(self, *, streaming: bool = False) -> LoadedMap:
        self.streaming_requests.append(streaming)
        snapshot = self._snapshots[self._index]
        self._index += 1
        return LoadedMap(
            snapshot_id=f"snapshot-{self._index}",
            captured_at=datetime(2026, 7, 27, 8, self._index, tzinfo=UTC),
            snapshot=snapshot,
        )


def test_map_image_accumulates_live_deltas_and_resets_when_idle(
    tmp_path: Path,
) -> None:
    async def run_test() -> None:
        first = _snapshot(
            cleaned_paths=((Point(10, 10), Point(10, 80)),),
            tracking_position=Point(10, 80),
        )
        delta = _snapshot(
            cleaned_paths=(),
            tracking_position=Point(20, 80),
        )
        source = _SequenceMapSource([first, delta, delta])
        coordinator = SimpleNamespace(data={DP_TASK_ACTIVE: True})
        entry = SimpleNamespace(data={"device_id": "synthetic-device"})
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass,
            cast(EufyMowerCoordinator, coordinator),
            cast(MapSource, source),
            cast(ConfigEntry, entry),
        )

        await entity.async_update()
        assert b'class="cleaned-path"' in (entity.image() or b"")

        await entity.async_update()
        assert b'class="cleaned-path"' in (entity.image() or b"")

        coordinator.data[DP_TASK_ACTIVE] = False
        await entity.async_update()
        assert b'class="cleaned-path"' not in (entity.image() or b"")
        assert source.streaming_requests == [True, True, False]
        await hass.async_stop()

    asyncio.run(run_test())


def test_home_assistant_poll_updates_map_and_streaming_mode(tmp_path, monkeypatch):
    """Exercise HA's polling filter, which skips non-polling ImageEntity defaults."""

    async def run_test():
        snapshot = _snapshot(cleaned_paths=(), tracking_position=Point(20, 80))
        source = _SequenceMapSource([snapshot, snapshot])
        coordinator = SimpleNamespace(data={DP_TASK_ACTIVE: True})
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, source, SimpleNamespace(data={"device_id": "synthetic-device"})
        )
        entity.hass = hass
        platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain="image",
            platform_name="eufy_robomow",
            platform=None,
            scan_interval=timedelta(seconds=2),
            entity_namespace=None,
        )
        platform.entities["image.test_mower"] = entity

        async def update_state(force_refresh=False):
            assert force_refresh
            await entity.async_update()

        # State registration is unrelated to polling eligibility; run the real
        # platform polling path and source/render work, without writing HA state.
        monkeypatch.setattr(entity, "async_update_ha_state", update_state)
        await platform._async_update_entity_states()
        coordinator.data[DP_TASK_ACTIVE] = False
        await platform._async_update_entity_states()
        assert source.streaming_requests == [True, False]
        assert entity.image() is not None
        await hass.async_stop()

    asyncio.run(run_test())


def test_cloud_activity_starts_map_stream_and_receipt_expiry_ends_it(tmp_path, monkeypatch):
    async def run_test():
        snapshot = _snapshot(
            cleaned_paths=((Point(10, 10), Point(10, 80)),),
            tracking_position=Point(10, 80),
        )
        source = _SequenceMapSource([snapshot, snapshot])
        now = datetime(2026, 9, 26, 10, tzinfo=UTC)
        coordinator = object.__new__(EufyMowerCoordinator)
        coordinator.backend = BACKEND_BRIDGE
        coordinator.last_update_success = True
        coordinator.bridge_cloud_status = BridgeCloudStatus(
            source="cloud",
            observed_at=now,
            age_ms=0,
            stale=False,
            error=None,
            status="reported",
            activity="mowing",
        )
        monkeypatch.setattr("custom_components.eufy_robomow.coordinator.dt_util.utcnow", lambda: now)
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, source, SimpleNamespace(data={"device_id": "synthetic-device"})
        )

        await entity.async_update()
        assert b'class="cleaned-path"' in (entity.image() or b"")
        now += timedelta(seconds=91)
        await entity.async_update()
        assert b'class="cleaned-path"' not in (entity.image() or b"")
        assert source.streaming_requests == [True, False]
        await hass.async_stop()

    asyncio.run(run_test())
