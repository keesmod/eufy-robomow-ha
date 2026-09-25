"""What a backend switch, an upgrade and a rollback keep, for the rehearsal in issue #8.

A switch between the local backend and the mower bridge, and an upgrade or a
rollback of the integration, keep the config entry, the unique ids of its
entities and its session history. These tests pin the parts of that which live
in code. The rehearsal on the owner's installation checks the rest.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store, UnsupportedStorageVersionError

import custom_components.eufy_robomow as integration
from custom_components.eufy_robomow import lawn_mower, number, select, sensor, switch
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
    DOMAIN,
    OPERATING_MODE_CONTROL,
)
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.sessions import SessionHistory, SessionStore

DEVICE = "synthetic-device"
# One status both backends can report: battery, network, signal and the local settings.
DATA = {"8": 85, "134": "Wifi", "109": 70, "110": 40, "26": 20, "101": True, "47": True, "132": True, "141": False, "133": False}
# The cloud settings (DP 155) have no bridge route, so bridge mode does not create them.
CLOUD_SETTINGS = {
    f"{DEVICE}_edge_distance",
    f"{DEVICE}_pad_direction",
    f"{DEVICE}_path_mm",
    f"{DEVICE}_travel_speed",
    f"{DEVICE}_blade_speed",
}
BRIDGE_OPTIONS = {
    CONF_BACKEND: BACKEND_BRIDGE,
    CONF_BRIDGE_URL: "http://127.0.0.1:8090",
    CONF_BRIDGE_TOKEN: "synthetic-bridge-token-0123456789abcdef",
    CONF_BRIDGE_MOWER_ID: "c" * 64,
    CONF_OPERATING_MODE: OPERATING_MODE_CONTROL,
}


def _entry(**options: Any) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CONF_HOST: "192.0.2.1",
            CONF_DEVICE_ID: DEVICE,
            CONF_LOCAL_KEY: "not-a-real-local-key",
            CONF_EUFY_EMAIL: "owner@example.invalid",
            CONF_EUFY_PASSWORD: "not-a-real-password",
        },
        options=options,
        entry_id="synthetic-entry",
    )


def _coordinator(backend: str) -> EufyMowerCoordinator:
    # Built without __init__, with what the platforms read at setup.
    coordinator = object.__new__(EufyMowerCoordinator)
    coordinator.operating_mode = OPERATING_MODE_CONTROL
    coordinator.backend = backend
    coordinator.cloud_client = object() if backend == BACKEND_LOCAL else None
    coordinator.session_store = SimpleNamespace(history=SessionHistory())
    coordinator.local_dps = dict(DATA) if backend == BACKEND_LOCAL else {}
    coordinator.command = None
    coordinator.last_local_update = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    coordinator.data = dict(DATA)
    coordinator._new_dp_callbacks = []
    return coordinator


def _unique_ids(backend: str) -> set[str]:
    entry = _entry(**({CONF_BACKEND: backend} if backend == BACKEND_BRIDGE else {}))
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: _coordinator(backend)}})
    added: list[Any] = []
    for platform in (lawn_mower, sensor, number, select, switch):
        asyncio.run(platform.async_setup_entry(cast(HomeAssistant, hass), cast(Any, entry), added.extend))
    ids = [entity.unique_id for entity in added]
    assert len(ids) == len(set(ids)), "no platform creates a unique id twice"
    return set(ids)


def test_a_backend_switch_keeps_every_unique_id_and_hides_only_the_cloud_settings() -> None:
    local = _unique_ids(BACKEND_LOCAL)
    bridge = _unique_ids(BACKEND_BRIDGE)
    assert bridge <= local, "bridge mode creates no entity the local backend lacks"
    assert local - bridge == CLOUD_SETTINGS
    assert {f"{DEVICE}_mower", f"{DEVICE}_battery", f"{DEVICE}_mowing_session", f"{DEVICE}_cut_height"} <= bridge
    assert all(unique_id.startswith(f"{DEVICE}_") for unique_id in local), "ids come from the entry's device id"


def test_the_session_store_follows_the_entry_on_either_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[str] = []

    class RecordingStore:
        def __init__(self, hass: object, entry_id: str) -> None:
            created.append(entry_id)
            self.async_load = AsyncMock()

    class QuietCoordinator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.session_store = None

        async def async_config_entry_first_refresh(self) -> None:
            pass

    monkeypatch.setattr(integration, "SessionStore", RecordingStore)
    monkeypatch.setattr(integration, "EufyMowerCoordinator", QuietCoordinator)
    monkeypatch.setattr(
        "custom_components.eufy_robomow.cloud.EufyCloudClient", lambda **kwargs: SimpleNamespace()
    )
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
    )
    for options in ({}, BRIDGE_OPTIONS, {CONF_BACKEND: BACKEND_LOCAL}):
        assert asyncio.run(integration.async_setup_entry(cast(HomeAssistant, hass), cast(Any, _entry(**options))))
    # The store's key is the entry id, see the version test below, so every backend keeps one history.
    assert created == ["synthetic-entry"] * 3


def test_the_session_store_stays_on_major_version_1_so_a_rollback_can_read_it(tmp_path: Path) -> None:
    """Home Assistant reads a newer minor version but refuses a newer major version.

    A newer release that changes the session layout must use a minor version or
    a new key. A major version 2 would make every older version fail to set up
    the entry after a rollback.
    """

    async def scenario() -> None:
        hass = HomeAssistant(str(tmp_path))
        try:
            store = SessionStore(hass, "synthetic-entry")
            assert (store.store.version, store.store.minor_version) == (1, 1)

            session = {"id": "s1", "phase": "docked", "mowing_seconds": 60, "added_later": True}
            newer_minor = Store(hass, 1, "eufy_robomow.sessions.synthetic-entry", minor_version=2)
            await newer_minor.async_save({"current": None, "recent": [session]})
            rolled_back = SessionStore(hass, "synthetic-entry")
            await rolled_back.async_load()
            assert rolled_back.history.recent == [session], "a newer minor version loads unchanged"

            newer_major = Store(hass, 2, "eufy_robomow.sessions.other-entry")
            await newer_major.async_save({"current": None, "recent": [session]})
            with pytest.raises(UnsupportedStorageVersionError):
                await SessionStore(hass, "other-entry").async_load()
        finally:
            await hass.async_stop()

    asyncio.run(scenario())


def test_session_fields_of_a_newer_version_survive_a_load_and_a_save() -> None:
    history = SessionHistory()
    current = {"id": "now", "phase": "mowing", "started_at": "2026-09-25T10:00:00+00:00", "added_later": 1}
    recent = [{"id": "s1", "phase": "docked", "mowing_seconds": 60, "added_later": [1, 2]}]
    history.load({"current": dict(current), "recent": [dict(item) for item in recent]})
    dumped = history.dump()
    assert dumped["recent"] == recent
    assert dumped["current"] == {**current, "observation_gap": True}, "a restart only marks the gap"
