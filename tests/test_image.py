"""Tests for live E15 map image behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util import dt as dt_util

from custom_components.eufy_robomow.bridge_client import BridgeCloudStatus
from custom_components.eufy_robomow.const import BACKEND_BRIDGE, BACKEND_LOCAL, DP_TASK_ACTIVE
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.image import EufyRobomowMapImage
from custom_components.eufy_robomow.map import MapSnapshot, Point
from custom_components.eufy_robomow.map_source import LoadedMap, MapSource, MapSourceError


def _snapshot(
    *,
    cleaned_paths: tuple[tuple[Point, ...], ...],
    tracking_position: Point | None,
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
    def __init__(self, snapshots: list[MapSnapshot | LoadedMap | MapSourceError]) -> None:
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
        if isinstance(snapshot, MapSourceError):
            raise snapshot
        if isinstance(snapshot, LoadedMap):
            return snapshot
        return LoadedMap(
            snapshot_id=f"snapshot-{self._index}",
            captured_at=dt_util.utcnow(),
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


def test_task_start_does_not_accumulate_the_old_idle_cache(tmp_path, monkeypatch):
    """The first stream reply/304 may still be the prior idle snapshot."""
    async def run_test():
        now = datetime(2026, 9, 26, 10, tzinfo=UTC)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        old = LoadedMap(
            "idle-cache",
            now - timedelta(minutes=5),
            _snapshot(
                cleaned_paths=((Point(10, 10), Point(10, 80)),),
                tracking_position=Point(10, 80),
            ),
        )
        fresh = LoadedMap(
            "task-delta",
            now + timedelta(seconds=2),
            _snapshot(
                cleaned_paths=((Point(60, 10), Point(60, 80)),),
                tracking_position=Point(60, 80),
            ),
        )
        source = _SequenceMapSource([old, old, fresh])
        coordinator = SimpleNamespace(data={DP_TASK_ACTIVE: False})
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, source, SimpleNamespace(data={"device_id": "synthetic-device"})
        )

        await entity.async_update()
        idle_image = entity.image()
        coordinator.data[DP_TASK_ACTIVE] = True
        await entity.async_update()
        assert entity.image() == idle_image, "the old map remains visible while the live capture starts"
        now += timedelta(seconds=2)
        await entity.async_update()

        assert (entity.image() or b"").count(b'class="cleaned-path"') == 1
        assert source.streaming_requests == [False, True, True]
        assert old.snapshot.cleaned_paths == ((Point(10, 10), Point(10, 80)),)
        await hass.async_stop()

    asyncio.run(run_test())


@pytest.mark.parametrize("backend", [BACKEND_LOCAL, BACKEND_BRIDGE])
def test_reload_during_task_cannot_inherit_cached_coverage_or_tracking(
    tmp_path, monkeypatch, backend,
):
    async def run_test():
        now = datetime(2026, 9, 26, 10, tzinfo=UTC)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        old = LoadedMap(
            "cached-before-reload", now - timedelta(seconds=2),
            _snapshot(
                cleaned_paths=((Point(10, 10), Point(10, 80)),),
                tracking_position=Point(10, 80),
            ),
        )
        empty = _snapshot(cleaned_paths=(), tracking_position=None)
        tracked = _snapshot(cleaned_paths=(), tracking_position=Point(60, 80))
        source = _SequenceMapSource([old, old, empty, tracked, empty])
        coordinator = SimpleNamespace(
            backend=backend, data={DP_TASK_ACTIVE: True}, bridge_task_active=True,
        )
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, source, SimpleNamespace(data={"device_id": "synthetic-device"})
        )

        await entity.async_update()
        cached_image = entity.image()
        assert cached_image is not None and b'class="cleaned-path"' not in cached_image
        await entity.async_update()
        assert entity.image() == cached_image

        now += timedelta(seconds=2)
        await entity.async_update()
        assert b'class="cleaned-path"' not in (entity.image() or b"")
        assert b'class="mower-marker"' not in (entity.image() or b"")
        assert b'class="charging-station"' in (entity.image() or b"")

        now += timedelta(seconds=2)
        await entity.async_update()
        tracked_image = entity.image()
        assert b'class="mower-marker"' in (tracked_image or b"")
        now += timedelta(seconds=2)
        await entity.async_update()
        assert entity.image() == tracked_image, "missing tracking may retain this task's last pose"
        assert old.snapshot.tracking_position == Point(10, 80)
        await hass.async_stop()

    asyncio.run(run_test())


@pytest.mark.parametrize("idle_failure", ["stale", "exception"])
def test_failed_idle_refresh_still_ends_the_previous_live_accumulation(
    tmp_path, monkeypatch, idle_failure,
):
    async def run_test():
        now = datetime(2026, 9, 26, 10, tzinfo=UTC)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        previous = LoadedMap(
            "previous-task", now,
            _snapshot(
                cleaned_paths=((Point(10, 10), Point(10, 80)),),
                tracking_position=Point(10, 80),
            ),
        )
        delta = _snapshot(
            cleaned_paths=((Point(60, 10), Point(60, 80)),),
            tracking_position=None,
        )
        source = _SequenceMapSource([
            previous, MapSourceError("synthetic failure") if idle_failure == "exception" else previous,
            previous, delta,
        ])
        coordinator = SimpleNamespace(data={DP_TASK_ACTIVE: True})
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, source, SimpleNamespace(data={"device_id": "synthetic-device"})
        )

        await entity.async_update()
        assert (entity.image() or b"").count(b'class="cleaned-path"') == 1
        now += timedelta(seconds=2)
        coordinator.data[DP_TASK_ACTIVE] = False
        source.status.state = "stale"
        await entity.async_update()

        now += timedelta(seconds=2)
        coordinator.data[DP_TASK_ACTIVE] = True
        source.status.state = "healthy"
        await entity.async_update()
        assert b'class="cleaned-path"' not in (entity.image() or b"")
        now += timedelta(seconds=2)
        await entity.async_update()
        assert (entity.image() or b"").count(b'class="cleaned-path"') == 1
        assert entity._live_map is not None
        assert entity._live_map.snapshot.tracking_position is None
        assert previous.snapshot.cleaned_paths != delta.cleaned_paths
        await hass.async_stop()

    asyncio.run(run_test())


def test_new_task_repaints_same_content_id_after_idle_fetch_failure(tmp_path, monkeypatch):
    async def run_test():
        now = datetime(2026, 9, 26, 10, tzinfo=UTC)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        first = _snapshot(
            cleaned_paths=((Point(10, 10), Point(10, 80)),), tracking_position=Point(10, 80),
        )
        second = _snapshot(
            cleaned_paths=((Point(60, 10), Point(60, 80)),), tracking_position=Point(60, 80),
        )
        source = _SequenceMapSource([
            first, LoadedMap("same-content", now + timedelta(seconds=2), second),
            MapSourceError("synthetic failure"),
            LoadedMap("same-content", now + timedelta(seconds=6), second),
        ])
        coordinator = SimpleNamespace(data={DP_TASK_ACTIVE: True})
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, source, SimpleNamespace(data={"device_id": "synthetic-device"})
        )

        await entity.async_update()
        now += timedelta(seconds=2)
        await entity.async_update()
        assert (entity.image() or b"").count(b'class="cleaned-path"') == 2
        now += timedelta(seconds=2)
        coordinator.data[DP_TASK_ACTIVE] = False
        await entity.async_update()
        now += timedelta(seconds=2)
        coordinator.data[DP_TASK_ACTIVE] = True
        await entity.async_update()
        assert (entity.image() or b"").count(b'class="cleaned-path"') == 1
        await hass.async_stop()

    asyncio.run(run_test())


@pytest.mark.parametrize("failure", ["stale", "exception", "future", "older"])
def test_invalid_refresh_preserves_only_the_last_accepted_live_map(
    tmp_path, monkeypatch, failure,
):
    async def run_test():
        now = datetime(2026, 9, 26, 10, tzinfo=UTC)
        monkeypatch.setattr(dt_util, "utcnow", lambda: now)
        first = LoadedMap(
            "task-start", now,
            _snapshot(
                cleaned_paths=((Point(10, 10), Point(10, 80)),),
                tracking_position=Point(10, 80),
            ),
        )
        accepted = LoadedMap("task-latest", now + timedelta(seconds=2), first.snapshot)
        rejected = LoadedMap(
            "rejected", now + timedelta(seconds={"future": 30, "older": 1}.get(failure, 4)),
            _snapshot(
                cleaned_paths=((Point(60, 10), Point(60, 80)),),
                tracking_position=Point(60, 80),
            ),
        )
        source = _SequenceMapSource([
            first, accepted, MapSourceError("synthetic failure") if failure == "exception" else rejected,
            _snapshot(cleaned_paths=(), tracking_position=None),
        ])
        coordinator = SimpleNamespace(data={DP_TASK_ACTIVE: True})
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, source, SimpleNamespace(data={"device_id": "synthetic-device"})
        )

        await entity.async_update()
        now += timedelta(seconds=2)
        await entity.async_update()
        live_image = entity.image()
        now += timedelta(seconds=2)
        if failure == "stale":
            source.status.state = "stale"
        await entity.async_update()
        assert entity.image() == live_image
        assert entity.image_last_updated == accepted.captured_at

        now += timedelta(seconds=2)
        source.status.state = "healthy"
        await entity.async_update()
        assert entity.image() == live_image
        assert (entity.image() or b"").count(b'class="cleaned-path"') == 1
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
