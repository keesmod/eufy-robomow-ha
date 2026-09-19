"""Authenticated client for the dedicated mower bridge (bridge/ in this repository).

The bridge serves contract 1 of ``GET /v1/state``, ``GET /v1/mowers`` and
``GET /v1/mowers/{id}/state``. This client validates the connection settings,
bounds every request and reduces failures to stable codes without upstream
detail. It never logs the token or a document.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import DP_BATTERY, DP_NETWORK, DP_SIGNAL

BRIDGE_NAME = "eufy-robomow-bridge"
BRIDGE_PROTOCOL = 1
STATE_CONTRACT = 1
_MOWER_ID_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_TOKEN_PATTERN = re.compile(r"^[\x21-\x7e]{32,256}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_MAX_BODY_SIZE = 256 * 1024
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)
_NETWORK_KINDS = {
    "wifi": "Wifi",
    "cellular": "Cellular",
    "ethernet": "Ethernet",
    "none": "None",
}


class BridgeSettingsError(ValueError):
    """Raised when bridge connection settings are unsafe or incomplete."""


class BridgeClientError(Exception):
    """A bridge request failed with a stable code and, when known, an HTTP status."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    """Validated bridge connection settings."""

    base_url: str
    token: str
    mower_id: str | None
    certificate_fingerprint: bytes | None

    @classmethod
    def from_values(
        cls,
        *,
        base_url: str,
        token: str,
        mower_id: str,
        certificate_fingerprint: str,
    ) -> BridgeSettings:
        """Validate raw option values."""
        normalized_url = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise BridgeSettingsError(
                "Bridge URL must be an http or https base URL without credentials, "
                "query or fragment"
            )
        normalized_token = token.strip()
        if not _TOKEN_PATTERN.fullmatch(normalized_token):
            raise BridgeSettingsError(
                "Bridge token must contain 32 to 256 printable characters without spaces"
            )
        normalized_id = mower_id.strip().lower()
        if normalized_id and not _MOWER_ID_PATTERN.fullmatch(normalized_id):
            raise BridgeSettingsError("Bridge mower id must be 64 hexadecimal characters")
        normalized_fingerprint = (
            certificate_fingerprint.lower().replace(":", "").replace(" ", "")
        )
        if normalized_fingerprint:
            if parsed.scheme != "https":
                raise BridgeSettingsError(
                    "Bridge certificate fingerprint requires an https bridge URL"
                )
            if not _SHA256_PATTERN.fullmatch(normalized_fingerprint):
                raise BridgeSettingsError(
                    "Bridge certificate fingerprint must be 64 hexadecimal characters"
                )
        return cls(
            base_url=normalized_url,
            token=normalized_token,
            mower_id=normalized_id or None,
            certificate_fingerprint=(
                bytes.fromhex(normalized_fingerprint) if normalized_fingerprint else None
            ),
        )


@dataclass(frozen=True, slots=True)
class BridgeTelemetry:
    """The parts of one bridge state document that the integration consumes."""

    observed_at: datetime
    age_ms: int | None
    stale: bool
    error: str | None
    activity: str | None
    dps: dict[str, Any]


def _field(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("state"), str):
        raise BridgeClientError("invalid_document")
    return value


def _reported_value(field: dict[str, Any]) -> dict[str, Any] | None:
    if field["state"] != "reported":
        return None
    value = field.get("value")
    if not isinstance(value, dict):
        raise BridgeClientError("invalid_document")
    return value


def parse_state_document(document: Any, mower_id: str) -> BridgeTelemetry:
    """Validate a contract 1 state document and map its typed fields onto DPS keys.

    Only the library's typed, reported values are mapped. Raw data points are not
    part of the contract and nothing is derived from absence or age here.
    """
    if not isinstance(document, dict):
        raise BridgeClientError("invalid_document")
    if document.get("contract") != STATE_CONTRACT or document.get("id") != mower_id:
        raise BridgeClientError("invalid_document")
    if document.get("source") != "local-tuya-3.5":
        raise BridgeClientError("invalid_document")
    observed_raw = document.get("observed_at")
    observed_at = dt_util.parse_datetime(observed_raw) if isinstance(observed_raw, str) else None
    if observed_at is None or observed_at.tzinfo is None:
        raise BridgeClientError("invalid_document")
    stale = document.get("stale")
    error = document.get("error")
    age_ms = document.get("age_ms")
    if not isinstance(stale, bool) or not (error is None or isinstance(error, str)):
        raise BridgeClientError("invalid_document")
    if not (age_ms is None or (isinstance(age_ms, int) and not isinstance(age_ms, bool))):
        raise BridgeClientError("invalid_document")

    dps: dict[str, Any] = {}
    battery = _reported_value(_field(document, "battery"))
    if battery is not None:
        percent = battery.get("percent")
        if not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100:
            raise BridgeClientError("invalid_document")
        dps[DP_BATTERY] = percent
    network = _reported_value(_field(document, "network"))
    if network is not None:
        kind = network.get("kind")
        if kind is not None:
            if not isinstance(kind, str):
                raise BridgeClientError("invalid_document")
            dps[DP_NETWORK] = _NETWORK_KINDS.get(kind, kind.capitalize())
        signal = network.get("signalPercent")
        if signal is not None:
            if not isinstance(signal, int) or isinstance(signal, bool) or not 0 <= signal <= 100:
                raise BridgeClientError("invalid_document")
            # The same raw device percentage the local backend reads from DP 109.
            dps[DP_SIGNAL] = signal
    status = _field(document, "status")
    activity: str | None = None
    if status["state"] == "reported":
        if not isinstance(status.get("value"), str):
            raise BridgeClientError("invalid_document")
        activity = status["value"]
    _field(document, "progress")
    return BridgeTelemetry(
        observed_at=observed_at,
        age_ms=age_ms,
        stale=stale,
        error=error,
        activity=activity,
        dps=dps,
    )


def resolve_mower_id(discovery: Any, configured: str | None) -> str:
    """Pick the mower this entry owns from a contract 1 discovery document."""
    if not isinstance(discovery, dict) or not isinstance(discovery.get("mowers"), list):
        raise BridgeClientError("invalid_document")
    ids: list[str] = []
    for mower in discovery["mowers"]:
        if not isinstance(mower, dict) or not isinstance(mower.get("id"), str):
            raise BridgeClientError("invalid_document")
        ids.append(mower["id"])
    if configured:
        if configured not in ids:
            raise BridgeClientError("unknown_mower")
        return configured
    if not ids:
        raise BridgeClientError("no_mowers")
    if len(ids) > 1:
        raise BridgeClientError("mower_selection_required")
    return ids[0]


class BridgeClient:
    """Read-only bridge access over Home Assistant's shared aiohttp session."""

    def __init__(self, hass: HomeAssistant, settings: BridgeSettings) -> None:
        self._hass = hass
        self._settings = settings

    @property
    def settings(self) -> BridgeSettings:
        return self._settings

    async def async_state(self) -> dict[str, Any]:
        """The bridge state document. Also proves the token."""
        document = await self._async_get("/v1/state")
        if document.get("protocol") != BRIDGE_PROTOCOL or document.get("bridge") != BRIDGE_NAME:
            raise BridgeClientError("invalid_document")
        return document

    async def async_mowers(self) -> dict[str, Any]:
        """The contract 1 discovery document."""
        return await self._async_get("/v1/mowers")

    async def async_mower_state(self, mower_id: str) -> dict[str, Any]:
        """One contract 1 state document for a discovered mower."""
        if not _MOWER_ID_PATTERN.fullmatch(mower_id):
            raise BridgeClientError("invalid_mower_id")
        return await self._async_get(f"/v1/mowers/{mower_id}/state")

    async def _async_get(self, path: str) -> dict[str, Any]:
        session = async_get_clientsession(self._hass)
        ssl: bool | aiohttp.Fingerprint = True
        if self._settings.certificate_fingerprint is not None:
            ssl = aiohttp.Fingerprint(self._settings.certificate_fingerprint)
        headers = {
            "Authorization": f"Bearer {self._settings.token}",
            "Accept": "application/json",
        }
        try:
            async with session.get(
                f"{self._settings.base_url}{path}",
                headers=headers,
                ssl=ssl,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if (
                    response.content_length is not None
                    and response.content_length > _MAX_BODY_SIZE
                ):
                    raise BridgeClientError("response_too_large", response.status)
                encoded = await response.content.read(_MAX_BODY_SIZE + 1)
                if len(encoded) > _MAX_BODY_SIZE:
                    raise BridgeClientError("response_too_large", response.status)
                status = response.status
        except aiohttp.ServerFingerprintMismatch as exc:
            raise BridgeClientError("certificate_mismatch") from exc
        except aiohttp.ClientConnectorCertificateError as exc:
            raise BridgeClientError("certificate_invalid") from exc
        except aiohttp.ClientConnectorError as exc:
            raise BridgeClientError("cannot_connect") from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise BridgeClientError("timeout") from exc
        except aiohttp.ClientError as exc:
            raise BridgeClientError("transport_failed") from exc

        try:
            payload: Any = json.loads(encoded)
        except ValueError:
            payload = None
        if status == 200:
            if not isinstance(payload, dict):
                raise BridgeClientError("invalid_response", status)
            return payload
        if status == 401:
            raise BridgeClientError("unauthorized", status)
        code = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(code, str) and _ERROR_CODE_PATTERN.fullmatch(code):
            raise BridgeClientError(code, status)
        raise BridgeClientError(f"http_{status}", status)
