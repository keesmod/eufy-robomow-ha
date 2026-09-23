"""Regression tests for Eufy Robomow integration options."""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from custom_components.eufy_robomow import async_setup_entry
from custom_components.eufy_robomow.bridge_client import BridgeClient, BridgeClientError
from custom_components.eufy_robomow.config_flow import EufyRobomowOptionsFlow
from custom_components.eufy_robomow.const import (
    BACKEND_BRIDGE,
    BACKEND_LOCAL,
    CONF_BACKEND,
    CONF_BRIDGE_MOWER_ID,
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_MAP_CERTIFICATE_FINGERPRINT,
    CONF_MAP_SOURCE,
    CONF_MAP_SOURCE_URL,
    CONF_OPERATING_MODE,
    DOMAIN,
    MAP_SOURCE_BRIDGE,
    MAP_SOURCE_EXTERNAL,
    OPERATING_MODE_CONTROL,
    OPERATING_MODE_OBSERVE_ONLY,
)

TOKEN = "synthetic-bridge-token-0123456789abcdef"
MOWER_ID = "d" * 64
OTHER_ID = "e" * 64
STATE = {"protocol": 1, "bridge": "eufy-robomow-bridge", "version": "0.2.0"}


class _FakeCoordinator:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def async_config_entry_first_refresh(self) -> None:
        pass


def test_options_flow_saves_and_schedules_automatic_reload(tmp_path) -> None:
    async def run_test() -> None:
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Synthetic mower",
            data={
                CONF_HOST: "127.0.0.1",
                CONF_DEVICE_ID: "synthetic-device",
                CONF_LOCAL_KEY: "0123456789abcdef",
            },
            options={},
            source="user",
            unique_id="synthetic-device",
            discovery_keys=MappingProxyType({}),
            subentries_data=(),
        )
        hass.config_entries._entries[entry.entry_id] = entry

        async def async_forward_entry_setups(*args: object) -> None:
            pass

        with (
            patch(
                "custom_components.eufy_robomow.EufyMowerCoordinator",
                _FakeCoordinator,
            ),
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                async_forward_entry_setups,
            ),
        ):
            await async_setup_entry(hass, entry)

        assert not entry.update_listeners

        flow = EufyRobomowOptionsFlow()
        flow.hass = hass
        flow.handler = entry.entry_id
        result = await flow.async_step_init(
            {
                CONF_OPERATING_MODE: OPERATING_MODE_CONTROL,
                CONF_MAP_SOURCE_URL: "",
                CONF_MAP_CERTIFICATE_FINGERPRINT: "",
            }
        )

        with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
            await hass.config_entries.options.async_finish_flow(flow, result)

        assert entry.options[CONF_OPERATING_MODE] == OPERATING_MODE_CONTROL
        reload_entry.assert_called_once_with(entry.entry_id)
        await hass.async_stop()

    asyncio.run(run_test())


def _hass_with_entry(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Synthetic mower",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_DEVICE_ID: "synthetic-device",
            CONF_LOCAL_KEY: "0123456789abcdef",
        },
        options={},
        source="user",
        unique_id="synthetic-device",
        discovery_keys=MappingProxyType({}),
        subentries_data=(),
    )
    hass.config_entries._entries[entry.entry_id] = entry
    flow = EufyRobomowOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    return hass, entry, flow


def _bridge_input(**overrides):
    values = {
        CONF_OPERATING_MODE: OPERATING_MODE_OBSERVE_ONLY,
        CONF_BACKEND: BACKEND_BRIDGE,
        CONF_BRIDGE_URL: "http://127.0.0.1:8090/",
        CONF_BRIDGE_TOKEN: TOKEN,
        CONF_BRIDGE_MOWER_ID: "",
        CONF_MAP_SOURCE_URL: "",
        CONF_MAP_CERTIFICATE_FINGERPRINT: "",
    }
    values.update(overrides)
    return values


def test_options_flow_validates_the_bridge_and_resolves_the_sole_mower(tmp_path) -> None:
    async def run_test() -> None:
        hass, entry, flow = _hass_with_entry(tmp_path)
        with (
            patch.object(BridgeClient, "async_state", return_value=STATE),
            patch.object(
                BridgeClient,
                "async_mowers",
                return_value={"contract": 1, "mowers": [{"id": MOWER_ID}]},
            ),
        ):
            result = await flow.async_step_init(_bridge_input())
        assert result["type"] == "create_entry"
        assert result["data"][CONF_BACKEND] == BACKEND_BRIDGE
        assert result["data"][CONF_BRIDGE_URL] == "http://127.0.0.1:8090"
        assert result["data"][CONF_BRIDGE_MOWER_ID] == MOWER_ID
        assert result["data"][CONF_BRIDGE_TOKEN] == TOKEN
        await hass.async_stop()

    asyncio.run(run_test())


def test_options_flow_reports_bridge_failures_without_saving(tmp_path) -> None:
    async def run_test() -> None:
        hass, entry, flow = _hass_with_entry(tmp_path)
        cases = [
            (BridgeClientError("unauthorized", 401), None, "", "bridge_unauthorized"),
            (BridgeClientError("cannot_connect"), None, "", "bridge_cannot_connect"),
            (STATE, {"contract": 1, "mowers": [{"id": MOWER_ID}, {"id": OTHER_ID}]}, "", "bridge_mower_selection_required"),
            (STATE, {"contract": 1, "mowers": [{"id": MOWER_ID}]}, OTHER_ID, "bridge_unknown_mower"),
            (STATE, {"contract": 1, "mowers": []}, "", "bridge_no_mowers"),
            (STATE, BridgeClientError("authentication_required", 503), "", "bridge_unavailable"),
        ]
        for state, mowers, mower_id, expected in cases:
            state_mock = patch.object(
                BridgeClient,
                "async_state",
                side_effect=state if isinstance(state, Exception) else None,
                return_value=None if isinstance(state, Exception) else state,
            )
            mowers_mock = patch.object(
                BridgeClient,
                "async_mowers",
                side_effect=mowers if isinstance(mowers, Exception) else None,
                return_value=None if isinstance(mowers, Exception) else mowers,
            )
            with state_mock, mowers_mock:
                result = await flow.async_step_init(_bridge_input(**{CONF_BRIDGE_MOWER_ID: mower_id}))
            assert result["type"] == "form", expected
            assert result["errors"] == {"base": expected}
        with patch.object(BridgeClient, "async_state", return_value=STATE):
            result = await flow.async_step_init(_bridge_input(**{CONF_BRIDGE_URL: "ftp://bridge"}))
        assert result["errors"] == {"base": "invalid_bridge"}
        assert entry.options == {}
        await hass.async_stop()

    asyncio.run(run_test())


def test_options_flow_keeps_an_explicit_mower_id_while_discovery_is_unavailable(tmp_path) -> None:
    async def run_test() -> None:
        hass, entry, flow = _hass_with_entry(tmp_path)
        with (
            patch.object(BridgeClient, "async_state", return_value=STATE),
            patch.object(
                BridgeClient,
                "async_mowers",
                side_effect=BridgeClientError("authentication_required", 503),
            ),
        ):
            result = await flow.async_step_init(_bridge_input(**{CONF_BRIDGE_MOWER_ID: MOWER_ID.upper()}))
        assert result["type"] == "create_entry"
        assert result["data"][CONF_BRIDGE_MOWER_ID] == MOWER_ID
        await hass.async_stop()

    asyncio.run(run_test())


def test_options_flow_local_backend_never_contacts_a_bridge(tmp_path) -> None:
    async def run_test() -> None:
        hass, entry, flow = _hass_with_entry(tmp_path)
        with patch.object(BridgeClient, "async_state") as state:
            result = await flow.async_step_init(
                {
                    CONF_OPERATING_MODE: OPERATING_MODE_CONTROL,
                    CONF_BACKEND: BACKEND_LOCAL,
                    CONF_BRIDGE_URL: "not even a url",
                    CONF_MAP_SOURCE_URL: "",
                    CONF_MAP_CERTIFICATE_FINGERPRINT: "",
                }
            )
        assert result["type"] == "create_entry"
        state.assert_not_called()
        await hass.async_stop()

    asyncio.run(run_test())


def test_options_flow_selects_the_bridge_map_only_with_the_bridge_backend_and_its_map_route(
    tmp_path,
) -> None:
    async def run_test() -> None:
        hass, entry, flow = _hass_with_entry(tmp_path)
        recovery = {CONF_MAP_SOURCE_URL: "https://map.example.test"}
        with patch.object(BridgeClient, "async_state") as state:
            local = await flow.async_step_init(
                {
                    CONF_OPERATING_MODE: OPERATING_MODE_OBSERVE_ONLY,
                    CONF_BACKEND: BACKEND_LOCAL,
                    CONF_MAP_SOURCE: MAP_SOURCE_BRIDGE,
                    CONF_MAP_SOURCE_URL: "",
                    CONF_MAP_CERTIFICATE_FINGERPRINT: "",
                }
            )
        assert local["type"] == "form"
        assert local["errors"] == {"base": "map_source_needs_bridge"}
        state.assert_not_called()

        mowers = {"contract": 1, "mowers": [{"id": MOWER_ID}]}
        without_maps = {**STATE, "routes": {"discovery": True, "state": True, "control": False, "maps": False}}
        with (
            patch.object(BridgeClient, "async_state", return_value=without_maps),
            patch.object(BridgeClient, "async_mowers", return_value=mowers),
        ):
            refused = await flow.async_step_init(
                _bridge_input(**{CONF_MAP_SOURCE: MAP_SOURCE_BRIDGE}, **recovery)
            )
        assert refused["type"] == "form"
        assert refused["errors"] == {"base": "bridge_maps_unavailable"}

        with_maps = {**STATE, "routes": {"discovery": True, "state": True, "control": False, "maps": True}}
        with (
            patch.object(BridgeClient, "async_state", return_value=with_maps),
            patch.object(BridgeClient, "async_mowers", return_value=mowers),
        ):
            result = await flow.async_step_init(
                _bridge_input(**{CONF_MAP_SOURCE: MAP_SOURCE_BRIDGE}, **recovery)
            )
        assert result["type"] == "create_entry"
        assert result["data"][CONF_MAP_SOURCE] == MAP_SOURCE_BRIDGE
        assert result["data"][CONF_BRIDGE_MOWER_ID] == MOWER_ID
        assert result["data"][CONF_MAP_SOURCE_URL] == "https://map.example.test", (
            "the external URL is kept for manual recovery"
        )

        with (
            patch.object(BridgeClient, "async_state", return_value=without_maps),
            patch.object(BridgeClient, "async_mowers", return_value=mowers),
        ):
            external = await flow.async_step_init(
                _bridge_input(**{CONF_MAP_SOURCE: MAP_SOURCE_EXTERNAL}, **recovery)
            )
        assert external["type"] == "create_entry", "the external map needs no bridge map route"
        assert external["data"][CONF_MAP_SOURCE] == MAP_SOURCE_EXTERNAL
        await hass.async_stop()

    asyncio.run(run_test())


def test_options_form_offers_the_map_source_with_the_external_default(tmp_path) -> None:
    async def run_test() -> None:
        hass, entry, flow = _hass_with_entry(tmp_path)
        form = await flow.async_step_init()
        schema = form["data_schema"].schema
        field = next(key for key in schema if key == CONF_MAP_SOURCE)
        assert field.default() == MAP_SOURCE_EXTERNAL
        assert schema[field].container == [MAP_SOURCE_EXTERNAL, MAP_SOURCE_BRIDGE]
        await hass.async_stop()

    asyncio.run(run_test())
