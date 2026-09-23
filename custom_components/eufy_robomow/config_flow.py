"""Config flow for Eufy Robomow.

Two-step setup:
  Step 1 (user)   — Eufy email + password → authenticates and discovers devices.
  Step 2 (device) — Pick a device from the list + enter its local IP address.

The device_id and local_key are retrieved automatically from the cloud;
the user never has to run external tools.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .bridge_client import (
    BridgeClient,
    BridgeClientError,
    BridgeSettings,
    BridgeSettingsError,
    resolve_mower_id,
)
from .const import (
    DOMAIN,
    BACKEND_BRIDGE,
    BACKENDS,
    CONF_BACKEND,
    CONF_BRIDGE_CERTIFICATE_FINGERPRINT,
    CONF_BRIDGE_MOWER_ID,
    CONF_BRIDGE_TOKEN,
    CONF_BRIDGE_URL,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_EUFY_EMAIL,
    CONF_EUFY_PASSWORD,
    CONF_MAP_CERTIFICATE_FINGERPRINT,
    CONF_MAP_SOURCE,
    CONF_MAP_SOURCE_URL,
    CONF_OPERATING_MODE,
    DEFAULT_BACKEND,
    DEFAULT_MAP_SOURCE,
    DEFAULT_OPERATING_MODE,
    MAP_SOURCE_BRIDGE,
    MAP_SOURCES,
    OPERATING_MODES,
)
from .map_source import MapSourceError, MapSourceSettings

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EUFY_EMAIL): str,
        vol.Required(CONF_EUFY_PASSWORD): str,
    }
)


def _discover_devices(email: str, password: str) -> list[dict]:
    """Log in and return all discoverable devices. Raises on failure."""
    from .cloud import EufyCloudClient
    client = EufyCloudClient(email=email, password=password, device_id="")
    devices = client.list_all_devices()
    if not devices:
        raise NoDevicesFound("No devices with a local key found in this account")
    return devices


def _test_local_connection(host: str, device_id: str, local_key: str) -> None:
    """Try to connect to the device via Tuya local protocol. Raises CannotConnect."""
    import tinytuya
    from .const import TUYA_VERSION
    d = tinytuya.Device(device_id, host, local_key, version=TUYA_VERSION)
    d.set_socketTimeout(5)
    result = d.status()
    if "Error" in result:
        raise CannotConnect(result["Error"])
    if "dps" not in result:
        raise CannotConnect("No DPS in response")


def _device_label(device: dict) -> str:
    """Human-readable label for a device entry in the picker dropdown."""
    name   = device.get("name") or device.get("productName") or "Unknown device"
    dev_id = device["devId"]
    return f"{name}  [{dev_id[:8]}…]"


class EufyRobomowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the two-step setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._email:      str        = ""
        self._password:   str        = ""
        self._discovered: list[dict] = []  # Tuya device dicts

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> EufyRobomowOptionsFlow:
        """Create the options flow."""
        return EufyRobomowOptionsFlow()

    # ── Step 1: credentials ───────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect Eufy credentials and discover devices."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email    = user_input[CONF_EUFY_EMAIL].strip()
            password = user_input[CONF_EUFY_PASSWORD].strip()

            try:
                devices = await self.hass.async_add_executor_job(
                    _discover_devices, email, password
                )
            except NoDevicesFound:
                errors["base"] = "no_devices"
            except ValueError:
                # Explicit login failure (wrong credentials or server rejection)
                _LOGGER.exception("Eufy login rejected")
                errors["base"] = "invalid_auth"
            except Exception:  # noqa: BLE001
                # Transient errors: network issues, timeouts, 5xx responses
                _LOGGER.exception("Unexpected error during device discovery")
                errors["base"] = "unknown"

            if not errors:
                self._email      = email
                self._password   = password
                self._discovered = devices
                return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    # ── Step 2: device selection ──────────────────────────────────────────────

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick a device and enter its local IP."""
        errors: dict[str, str] = {}

        # Build the dropdown options {devId: "Name [id…]"}
        options = {d["devId"]: _device_label(d) for d in self._discovered}

        # Auto-select when there is only one device.
        # Note: the cloud API's "ip" field reports the IP the device last connected
        # FROM, which is often the router's public IP or a 4G address — not the
        # device's LAN IP.  We intentionally leave CONF_HOST blank so the user
        # enters the correct internal address.
        auto_id = self._discovered[0]["devId"] if len(self._discovered) == 1 else None

        step_schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): vol.In(options),
                vol.Required(CONF_HOST): str,
            }
        )

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            host      = user_input[CONF_HOST].strip()

            # Retrieve the local key for the selected device
            device    = next(d for d in self._discovered if d["devId"] == device_id)
            local_key = device["localKey"]

            # Verify local connectivity
            try:
                await self.hass.async_add_executor_job(
                    _test_local_connection, host, device_id, local_key
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during local connection test")
                errors["base"] = "unknown"

            if not errors:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()

                device_name = device.get("name") or device.get("productName") or "Eufy E15"
                return self.async_create_entry(
                    title=f"{device_name} ({host})",
                    data={
                        CONF_HOST:          host,
                        CONF_DEVICE_ID:     device_id,
                        CONF_LOCAL_KEY:     local_key,
                        CONF_EUFY_EMAIL:    self._email,
                        CONF_EUFY_PASSWORD: self._password,
                        CONF_OPERATING_MODE: DEFAULT_OPERATING_MODE,
                    },
                )

        # Pre-populate the form with smart defaults (device ID only; host is manual)
        suggested: dict[str, Any] = {}
        if auto_id:
            suggested[CONF_DEVICE_ID] = auto_id

        return self.async_show_form(
            step_id="device",
            data_schema=self.add_suggested_values_to_schema(step_schema, suggested),
            errors=errors,
            description_placeholders={"device_count": str(len(self._discovered))},
        )


# Bridge client failure codes mapped onto options-flow error keys. Every other
# code, including bridge-side 503 codes, becomes a generic unavailable error.
_BRIDGE_FLOW_ERRORS = {
    "unauthorized": "bridge_unauthorized",
    "cannot_connect": "bridge_cannot_connect",
    "timeout": "bridge_cannot_connect",
    "transport_failed": "bridge_cannot_connect",
    "certificate_mismatch": "bridge_cannot_connect",
    "certificate_invalid": "bridge_cannot_connect",
    "mower_selection_required": "bridge_mower_selection_required",
    "unknown_mower": "bridge_unknown_mower",
    "no_mowers": "bridge_no_mowers",
}


async def _async_validate_bridge(
    hass: HomeAssistant, settings: BridgeSettings
) -> tuple[dict[str, Any], str]:
    """Prove the token against the bridge and resolve the mower this entry owns.

    Returns the bridge state document and the mower id. Discovery may
    legitimately be unavailable while the bridge is not connected to the cloud
    yet. An explicitly configured id is then kept as given. Without an id,
    discovery has to succeed so the entry never guesses a mower.
    """
    client = BridgeClient(hass, settings)
    state = await client.async_state()
    try:
        discovery = await client.async_mowers()
    except BridgeClientError:
        if settings.mower_id is not None:
            return state, settings.mower_id
        raise
    return state, resolve_mower_id(discovery, settings.mower_id)


def _bridge_serves_maps(state: dict[str, Any]) -> bool:
    """Whether the bridge reports its read-only map route, ``routes.maps``."""
    routes = state.get("routes")
    return isinstance(routes, dict) and routes.get("maps") is True


class EufyRobomowOptionsFlow(OptionsFlowWithReload):
    """Manage operating mode, the mower backend and the optional read-only map source.

    The map source is ``external``, the map source URL and the manual recovery
    path, or ``bridge``, the bridge's map route. The bridge source needs the
    bridge backend and a bridge that reports ``routes.maps``. Choosing it keeps
    the external URL, so switching back is one option change.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure integration behavior."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                MapSourceSettings.from_values(
                    base_url=user_input.get(CONF_MAP_SOURCE_URL, ""),
                    certificate_fingerprint=user_input.get(
                        CONF_MAP_CERTIFICATE_FINGERPRINT,
                        "",
                    ),
                    local_key=self.config_entry.data[CONF_LOCAL_KEY],
                )
            except MapSourceError:
                errors["base"] = "invalid_map_source"
            backend = user_input.get(CONF_BACKEND, DEFAULT_BACKEND)
            bridge_map = user_input.get(CONF_MAP_SOURCE, DEFAULT_MAP_SOURCE) == MAP_SOURCE_BRIDGE
            if not errors and bridge_map and backend != BACKEND_BRIDGE:
                errors["base"] = "map_source_needs_bridge"
            if not errors and backend == BACKEND_BRIDGE:
                try:
                    settings = BridgeSettings.from_values(
                        base_url=user_input.get(CONF_BRIDGE_URL, ""),
                        token=user_input.get(CONF_BRIDGE_TOKEN, ""),
                        mower_id=user_input.get(CONF_BRIDGE_MOWER_ID, ""),
                        certificate_fingerprint=user_input.get(
                            CONF_BRIDGE_CERTIFICATE_FINGERPRINT, ""
                        ),
                    )
                    state, mower_id = await _async_validate_bridge(self.hass, settings)
                except BridgeSettingsError:
                    errors["base"] = "invalid_bridge"
                except BridgeClientError as exc:
                    errors["base"] = _BRIDGE_FLOW_ERRORS.get(exc.code, "bridge_unavailable")
                else:
                    if bridge_map and not _bridge_serves_maps(state):
                        errors["base"] = "bridge_maps_unavailable"
                    user_input = {
                        **user_input,
                        CONF_BRIDGE_URL: settings.base_url,
                        CONF_BRIDGE_TOKEN: settings.token,
                        CONF_BRIDGE_MOWER_ID: mower_id,
                    }
            if not errors:
                return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        current_mode = options.get(
            CONF_OPERATING_MODE,
            self.config_entry.data.get(CONF_OPERATING_MODE, DEFAULT_OPERATING_MODE),
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_OPERATING_MODE): vol.In(OPERATING_MODES),
                vol.Required(CONF_BACKEND, default=DEFAULT_BACKEND): vol.In(BACKENDS),
                vol.Optional(CONF_BRIDGE_URL): str,
                vol.Optional(CONF_BRIDGE_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_BRIDGE_MOWER_ID): str,
                vol.Optional(CONF_BRIDGE_CERTIFICATE_FINGERPRINT): str,
                vol.Required(CONF_MAP_SOURCE, default=DEFAULT_MAP_SOURCE): vol.In(MAP_SOURCES),
                vol.Optional(CONF_MAP_SOURCE_URL): str,
                vol.Optional(CONF_MAP_CERTIFICATE_FINGERPRINT): str,
            }
        )
        suggested = {
            CONF_OPERATING_MODE: current_mode,
            CONF_BACKEND: options.get(CONF_BACKEND, DEFAULT_BACKEND),
            CONF_BRIDGE_URL: options.get(CONF_BRIDGE_URL, ""),
            CONF_BRIDGE_TOKEN: options.get(CONF_BRIDGE_TOKEN, ""),
            CONF_BRIDGE_MOWER_ID: options.get(CONF_BRIDGE_MOWER_ID, ""),
            CONF_BRIDGE_CERTIFICATE_FINGERPRINT: options.get(
                CONF_BRIDGE_CERTIFICATE_FINGERPRINT, ""
            ),
            CONF_MAP_SOURCE: options.get(CONF_MAP_SOURCE, DEFAULT_MAP_SOURCE),
            CONF_MAP_SOURCE_URL: options.get(CONF_MAP_SOURCE_URL, ""),
            CONF_MAP_CERTIFICATE_FINGERPRINT: options.get(
                CONF_MAP_CERTIFICATE_FINGERPRINT,
                "",
            ),
        }
        if user_input is not None:
            suggested.update(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )


# ── Custom exceptions ──────────────────────────────────────────────────────────

class CannotConnect(HomeAssistantError):
    """Cannot connect to the device via local Tuya protocol."""


class NoDevicesFound(HomeAssistantError):
    """No Tuya-compatible devices with local keys found in this account."""
