"""The map entity on the mower bridge's read-only map route.

The bundle contract is shared with the bridge: ``tests/fixtures/bridge_map_bundle.json``
holds the bundle the bridge builds from the synthetic streams of ``map_fixtures``, and
``bridge/test/maps.test.ts`` proves the bridge produces exactly these bytes.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from zipfile import ZIP_STORED, ZipFile

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.eufy_robomow import image as image_platform
from custom_components.eufy_robomow.bridge_client import BridgeClient, BridgeSettings
from custom_components.eufy_robomow.const import (
    BACKEND_BRIDGE,
    BACKEND_LOCAL,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_MAP_SOURCE,
    CONF_MAP_SOURCE_URL,
    DOMAIN,
    MAP_SOURCE_BRIDGE,
    MAP_SOURCE_EXTERNAL,
)
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.image import EufyRobomowMapImage, map_source_for_entry
from custom_components.eufy_robomow.map import MapSnapshot, Point
from custom_components.eufy_robomow.map_source import (
    BRIDGE_MAP_REFRESH_INTERVAL,
    MAP_BUNDLE_CONTENT_TYPE,
    MAP_REFRESH_INTERVAL,
    LoadedMap,
    MapSource,
    MapSourceError,
    MapSourceSettings,
    decode_map_bundle,
)

from .map_fixtures import clean_path_payload, map_payload, point

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "bridge_map_bundle.json").read_text())
BUNDLE = base64.b64decode(FIXTURE["bundle"])
MOWER_ID = FIXTURE["device_id"]
TOKEN = "synthetic-bridge-token-0123456789abcdef"
# Synthetic, 16 characters as the local key format requires.
LOCAL_KEY = "a" * 16


def _bridge_settings(url: str = "http://127.0.0.1:8090", fingerprint: str = "") -> BridgeSettings:
    return BridgeSettings.from_values(
        base_url=url, token=TOKEN, mower_id=MOWER_ID, certificate_fingerprint=fingerprint
    )


def test_the_bundle_the_bridge_builds_passes_the_integration_validator() -> None:
    files = {name: base64.b64decode(value) for name, value in FIXTURE["files"].items()}
    assert files == {
        "map.bin.stream": map_payload(),
        "cleanPath.bin.stream": clean_path_payload(),
        "navPath.bin.stream": point(11, 12, x_field=2, y_field=3),
    }, "the fixture's streams are the synthetic ones of map_fixtures"

    loaded = decode_map_bundle(BUNDLE, MOWER_ID)

    assert loaded.snapshot_id == FIXTURE["snapshot_id"]
    assert loaded.captured_at == datetime.fromtimestamp(FIXTURE["received_at"] // 1000, tz=UTC)
    assert loaded.snapshot.map_id == 539
    assert len(loaded.snapshot.boundary) == 4
    with ZipFile(BytesIO(BUNDLE)) as archive:
        assert [member.filename for member in archive.infolist()] == [
            "manifest.json",
            "map.bin.stream",
            "cleanPath.bin.stream",
            "navPath.bin.stream",
        ]
        assert all(member.compress_type == ZIP_STORED for member in archive.infolist())
        assert archive.testzip() is None
    with pytest.raises(MapSourceError, match="different device"):
        decode_map_bundle(BUNDLE, "e" * 64)


def test_bridge_map_settings_use_the_bridge_route_token_and_pin() -> None:
    settings = MapSourceSettings.for_bridge(
        _bridge_settings("https://bridge.example.test:8090/", "AA:" * 31 + "AA"), MOWER_ID
    )
    assert settings.map_url == f"https://bridge.example.test:8090/v1/mowers/{MOWER_ID}/map"
    assert settings.access_token == TOKEN, "the bridge token, never a key-derived token"
    assert settings.certificate_fingerprint == bytes.fromhex("aa" * 32)
    assert settings.idle_interval == BRIDGE_MAP_REFRESH_INTERVAL == timedelta(minutes=1)
    plain = MapSourceSettings.for_bridge(_bridge_settings(), MOWER_ID)
    assert plain.map_url == f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/map"
    assert plain.certificate_fingerprint is None
    with pytest.raises(MapSourceError):
        MapSourceSettings.for_bridge(_bridge_settings(), "not-a-mower-id")
    external = MapSourceSettings.from_values(
        base_url="https://map.example.test", certificate_fingerprint="", local_key=LOCAL_KEY
    )
    assert external is not None
    assert external.map_url == "https://map.example.test/v1/map", "the external source is unchanged"
    assert external.idle_interval == MAP_REFRESH_INTERVAL


class _FakeHass:
    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        return target(*args)


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, size: int) -> bytes:
        return self._body[:size]


class _Response:
    def __init__(
        self, status: int, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content_length = len(body)
        self.content = _Content(body)

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, str], object]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append((url, dict(kwargs["headers"]), kwargs["ssl"]))
        return self._responses.pop(0)


def _bundle_response(stale: str = "false", error: str | None = None) -> _Response:
    headers = {
        "Content-Type": MAP_BUNDLE_CONTENT_TYPE,
        "ETag": FIXTURE["etag"],
        "X-Eufy-Map-Stale": stale,
        "X-Eufy-Map-Age-Ms": "1250",
    }
    if error is not None:
        headers["X-Eufy-Map-Error"] = error
    return _Response(200, BUNDLE, headers)


def _bridge_source(tmp_path: Path) -> MapSource:
    return MapSource(
        cast(HomeAssistant, _FakeHass()),
        settings=MapSourceSettings.for_bridge(_bridge_settings(), MOWER_ID),
        device_id=MOWER_ID,
        cache_file=tmp_path / "entry" / "bridge.mapbundle",
        cache_root=tmp_path,
    )


def test_the_bridge_map_source_reads_the_route_and_reports_the_bridges_last_good_state(
    tmp_path: Path,
) -> None:
    source = _bridge_source(tmp_path)
    session = _Session(
        [
            _bundle_response(),
            _Response(
                304,
                headers={
                    "X-Eufy-Map-Stale": "true",
                    "X-Eufy-Map-Error": "mower_map_connection_failed",
                },
            ),
            _Response(304, headers={"X-Eufy-Map-Stale": "false"}),
            _Response(304, headers={"X-Eufy-Map-Stale": "true", "X-Eufy-Map-Error": "Not A Code!"}),
        ]
    )

    async def refresh() -> LoadedMap:
        source._last_attempt = None
        return await source.async_refresh()

    with patch(
        "custom_components.eufy_robomow.map_source.async_get_clientsession", return_value=session
    ):
        loaded = asyncio.run(refresh())
        assert source.status.state == "healthy"
        assert loaded.snapshot_id == FIXTURE["snapshot_id"]
        assert (tmp_path / "entry" / "bridge.mapbundle").read_bytes() == BUNDLE

        assert asyncio.run(refresh()) is loaded, "the last good map is kept"
        status = source.status
        assert status.state == "stale"
        assert (
            status.last_error == "Map source serves its last good map: mower_map_connection_failed"
        )
        assert status.last_success == loaded.captured_at

        asyncio.run(refresh())
        assert source.status.state == "healthy"
        asyncio.run(refresh())
        assert source.status.last_error == "Map source serves its last good map: stale"

    url, headers, ssl = session.requests[0]
    assert url == f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/map"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == MAP_BUNDLE_CONTENT_TYPE
    assert headers["X-Eufy-Map-Mode"] == "idle"
    assert "If-None-Match" not in headers
    assert ssl is True
    assert [request[1].get("If-None-Match") for request in session.requests[1:]] == [
        FIXTURE["etag"]
    ] * 3


def test_the_bridge_map_source_names_the_bridges_code_when_it_has_no_map(tmp_path: Path) -> None:
    source = _bridge_source(tmp_path)
    json_headers = {"Content-Type": "application/json"}
    session = _Session(
        [
            _Response(
                503, json.dumps({"error": "map_provisioning_unreadable"}).encode(), json_headers
            ),
            _Response(404, json.dumps({"error": "map_unconfigured"}).encode(), json_headers),
            _Response(503, b'{"error": "Upstream <detail>"}', json_headers),
            _Response(503, b"not json", {"Content-Type": "text/plain"}),
        ]
    )
    messages = []
    with patch(
        "custom_components.eufy_robomow.map_source.async_get_clientsession", return_value=session
    ):
        for _ in range(4):
            with pytest.raises(MapSourceError) as error:
                asyncio.run(source._async_fetch(streaming=True))
            messages.append(str(error.value))
    assert messages == [
        "Map source has no complete snapshot: map_provisioning_unreadable",
        "Map source returned HTTP 404: map_unconfigured",
        "Map source has no complete snapshot",
        "Map source has no complete snapshot",
    ]
    assert session.requests[0][1]["X-Eufy-Map-Mode"] == "stream"


def test_the_bridge_idle_map_is_checked_every_minute_and_the_external_one_every_five(
    tmp_path: Path,
) -> None:
    bridge = _bridge_source(tmp_path)
    external_settings = MapSourceSettings.from_values(
        base_url="https://map.example.test", certificate_fingerprint="", local_key=LOCAL_KEY
    )
    assert external_settings is not None
    external = MapSource(
        cast(HomeAssistant, _FakeHass()),
        settings=external_settings,
        device_id="synthetic-device",
        cache_file=tmp_path / "entry" / "latest.mapbundle",
    )
    assert bridge._next_refresh_delay(False) == 60
    assert external._next_refresh_delay(False) == 300
    assert bridge._next_refresh_delay(True) == external._next_refresh_delay(True) == 2


def _entry(**options: Any) -> SimpleNamespace:
    return SimpleNamespace(
        data={CONF_DEVICE_ID: "synthetic-device", CONF_LOCAL_KEY: LOCAL_KEY},
        options=options,
        entry_id="test-entry",
    )


def _coordinator(backend: str) -> SimpleNamespace:
    hass = SimpleNamespace()
    bridge = (
        BridgeClient(cast(HomeAssistant, hass), _bridge_settings())
        if backend == BACKEND_BRIDGE
        else None
    )
    return SimpleNamespace(
        backend=backend,
        bridge=bridge,
        bridge_mower_id=MOWER_ID if backend == BACKEND_BRIDGE else None,
        data={},
    )


def test_the_entry_selects_exactly_one_map_source_with_its_own_binding_and_cache(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hass = cast(HomeAssistant, SimpleNamespace())
    url = {CONF_MAP_SOURCE_URL: "https://map.example.test"}

    external = map_source_for_entry(
        hass,
        cast(ConfigEntry, _entry(**url)),
        cast(EufyMowerCoordinator, _coordinator(BACKEND_LOCAL)),
        tmp_path,
    )
    assert external is not None
    assert external._settings.map_url == "https://map.example.test/v1/map"
    assert external._device_id == "synthetic-device"
    assert external._cache_file == tmp_path / "test-entry" / "latest.mapbundle"
    assert (
        map_source_for_entry(
            hass,
            cast(ConfigEntry, _entry()),
            cast(EufyMowerCoordinator, _coordinator(BACKEND_LOCAL)),
            tmp_path,
        )
        is None
    ), "no URL and no bridge map means no map entity, as before"

    bridge = map_source_for_entry(
        hass,
        cast(ConfigEntry, _entry(**url, **{CONF_MAP_SOURCE: MAP_SOURCE_BRIDGE})),
        cast(EufyMowerCoordinator, _coordinator(BACKEND_BRIDGE)),
        tmp_path,
    )
    assert bridge is not None
    assert bridge._settings.map_url == f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/map"
    assert bridge._settings.access_token == TOKEN
    assert bridge._device_id == MOWER_ID, "bridge bundles bind to the bridge's mower id"
    assert bridge._cache_file == tmp_path / "test-entry" / "bridge.mapbundle", (
        "the external source's last good map stays untouched for recovery"
    )

    with caplog.at_level(logging.WARNING):
        refused = map_source_for_entry(
            hass,
            cast(ConfigEntry, _entry(**{CONF_MAP_SOURCE: MAP_SOURCE_BRIDGE})),
            cast(EufyMowerCoordinator, _coordinator(BACKEND_LOCAL)),
            tmp_path,
        )
    assert refused is None
    assert "needs the bridge backend" in caplog.text

    explicit_external = map_source_for_entry(
        hass,
        cast(ConfigEntry, _entry(**url, **{CONF_MAP_SOURCE: MAP_SOURCE_EXTERNAL})),
        cast(EufyMowerCoordinator, _coordinator(BACKEND_BRIDGE)),
        tmp_path,
    )
    assert explicit_external is not None
    assert explicit_external._settings.map_url == "https://map.example.test/v1/map", (
        "the external source stays selectable in bridge mode as manual recovery"
    )


def test_setup_creates_the_same_map_entity_for_the_bridge_source(tmp_path: Path) -> None:
    async def run_test() -> None:
        hass = HomeAssistant(str(tmp_path))
        hass.data[DOMAIN] = {"test-entry": _coordinator(BACKEND_BRIDGE)}
        added: list[tuple[list[Any], bool]] = []
        await image_platform.async_setup_entry(
            hass,
            cast(ConfigEntry, _entry(**{CONF_MAP_SOURCE: MAP_SOURCE_BRIDGE})),
            lambda entities, update_before_add=False: added.append(
                (list(entities), update_before_add)
            ),
        )
        [(entities, update_before_add)] = added
        [entity] = entities
        assert update_before_add is True
        assert entity.unique_id == "synthetic-device_map", "the image entity keeps its unique id"
        assert entity.device_info["identifiers"] == {(DOMAIN, "synthetic-device")}
        assert (
            entity._source._cache_file
            == tmp_path / "eufy_robomow_maps" / "test-entry" / "bridge.mapbundle"
        )
        await hass.async_stop()

    asyncio.run(run_test())


def _bridge_coordinator(**attributes: Any) -> EufyMowerCoordinator:
    coordinator = object.__new__(EufyMowerCoordinator)
    coordinator.backend = BACKEND_BRIDGE
    coordinator.last_update_success = True
    coordinator.bridge_status = "reported"
    coordinator.bridge_activity = "mowing"
    for name, value in attributes.items():
        setattr(coordinator, name, value)
    return coordinator


def test_a_bridge_task_is_only_a_reported_activity_from_a_successful_poll() -> None:
    for activity in ("mowing", "paused", "returning"):
        assert _bridge_coordinator(bridge_activity=activity).bridge_task_active is True, activity
    for attributes in (
        {"bridge_activity": "docked"},
        {"bridge_activity": None},
        {"bridge_status": "missing", "bridge_activity": None},
        {"bridge_status": "invalid", "bridge_activity": None},
        {"bridge_status": "unconfirmed", "bridge_activity": "mowing"},
        {"last_update_success": False},
    ):
        assert _bridge_coordinator(**attributes).bridge_task_active is False, attributes


class _SequenceSource:
    def __init__(self) -> None:
        self.streaming_requests: list[bool] = []
        self.status = SimpleNamespace(state="healthy", last_success=None, last_error=None)

    async def async_refresh(self, *, streaming: bool = False) -> LoadedMap:
        self.streaming_requests.append(streaming)
        return LoadedMap(
            snapshot_id=f"snapshot-{len(self.streaming_requests)}",
            captured_at=datetime(2026, 9, 23, 10, len(self.streaming_requests), tzinfo=UTC),
            snapshot=MapSnapshot(
                map_id=539,
                boundary=(Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)),
                base_areas=(),
                no_go_areas=(),
                pathways=(),
                cleaned_paths=((Point(10, 10), Point(10, 80)),),
                mower_position=Point(5, 50),
                tracking_position=Point(10, 80),
            ),
        )


def test_the_map_follows_the_bridge_task_live_and_idles_without_one(tmp_path: Path) -> None:
    async def run_test() -> None:
        coordinator = _bridge_coordinator(data={})
        source = _SequenceSource()
        hass = HomeAssistant(str(tmp_path))
        entity = EufyRobomowMapImage(
            hass, coordinator, cast(MapSource, source), cast(ConfigEntry, _entry())
        )
        await entity.async_update()
        assert b'class="cleaned-path"' in (entity.image() or b"")
        coordinator.bridge_activity = None
        coordinator.bridge_status = "invalid"
        await entity.async_update()
        assert b'class="cleaned-path"' not in (entity.image() or b"")
        assert source.streaming_requests == [True, False]
        await hass.async_stop()

    asyncio.run(run_test())
