"""Authenticated client for the dedicated mower bridge (bridge/ in this repository).

The bridge serves contract 1 of ``GET /v1/state``, ``GET /v1/mowers``,
``GET /v1/mowers/{id}/state`` and, in its ``control`` mode, one opt-in
``POST /v1/mowers/{id}/commands/{start|pause|resume|stop}``. This client validates the
connection settings, bounds every request and reduces failures to stable codes
without upstream detail. It never logs the token or a document, and it never
retries: a command request is sent exactly once.
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
# A command answers after the bridge's connect time plus its read-back bound, at
# most 60 seconds, so its request may take far longer than a state query.
_COMMAND_TIMEOUT = aiohttp.ClientTimeout(total=75, connect=5)
# The command classes bridge 0.6.0 routes. ``stop`` writes DP 1 false, which on
# the owned E15 ends the task and returns the mower to the dock by itself.
# ``return`` is refused by the bridge with ``command_unsupported`` because the
# owned E15 firmware ignored its DP 3 write from paused and from the stopped task.
COMMAND_CLASSES = frozenset({"start", "pause", "resume", "stop"})
_COMMAND_RESULTS = frozenset({"confirmed", "failed", "uncertain"})
# Codes the bridge, the library or this client raise before any frame is written.
# Every other failure of a command request leaves the write uncertain.
_REFUSED_BEFORE_WRITE = frozenset(
    {
        "control_disabled",
        "not_found",
        "invalid_mower_id",
        "unknown_mower",
        "command_unsupported",
        "command_in_progress",
        "telemetry_stale",
        "mower_host_unconfigured",
        "bridge_not_running",
        "authentication_required",
        "mower_binding_unavailable",
        "mower_request_failed",
        "mower_local_unreachable",
        "mower_local_authentication_failed",
        "mower_local_key_invalid",
        "mower_local_busy",
        "unauthorized",
        "cannot_connect",
        "certificate_mismatch",
        "certificate_invalid",
    }
)
_NETWORK_KINDS = {
    "wifi": "Wifi",
    "cellular": "Cellular",
    "ethernet": "Ethernet",
    "none": "None",
}
# The library's four states of one typed telemetry field. Only ``reported`` carries a value.
_FIELD_STATES = frozenset({"reported", "missing", "invalid", "unconfirmed"})


class BridgeSettingsError(ValueError):
    """Raised when bridge connection settings are unsafe or incomplete."""


class BridgeClientError(Exception):
    """A bridge request failed with a stable code and, when known, an HTTP status."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def refused_before_write(code: str) -> bool:
    """Whether a failed command request is known to have written nothing.

    The bridge checks mode, class, id, ownership, discovery, host and telemetry
    age before it touches the library, and the library's ``mower_command_*``
    refusals happen before any frame. A timeout, a lost transport or an invalid
    answer says nothing about the write, so those are not listed here.
    """
    return code in _REFUSED_BEFORE_WRITE or code.startswith("mower_command_")


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
    # The library's state of the typed status field: ``reported``, ``missing``,
    # ``invalid`` or ``unconfirmed``. Missing and invalid stay distinct from each
    # other and from an activity, and nothing is derived from age or absence.
    status: str
    # The reported activity, only while ``status`` is ``reported``.
    activity: str | None
    dps: dict[str, Any]
    # The latest confirmed activity of the command running on the bridge and when
    # the library received that report, for example ``returning`` shortly after a
    # dock. None without a running command or without progress yet.
    command_activity: tuple[str, datetime] | None = None


def _field(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict) or value.get("state") not in _FIELD_STATES:
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
        status=status["state"],
        activity=activity,
        dps=dps,
        command_activity=_command_activity(document.get("command")),
    )


def _command_activity(command: Any) -> tuple[str, datetime] | None:
    """The running command's latest activity from bridge 0.9.0 or later, or None.

    The field is optional progress. An older bridge omits it and a shape this
    integration does not know is ignored instead of failing the poll.
    """
    if not isinstance(command, dict):
        return None
    activity = command.get("activity")
    if not isinstance(activity, dict) or not isinstance(activity.get("value"), str):
        return None
    observed_at = _report_time(activity)
    if observed_at is None:
        return None
    return activity["value"], observed_at


@dataclass(frozen=True, slots=True)
class BridgeCommandOutcome:
    """The parts of one bridge command answer that the integration consumes."""

    # ``confirmed``, ``failed`` or ``uncertain``, the library's outcome unchanged.
    result: str
    # The furthest stage evidenced by fresh reports: sent, acknowledged or reflected.
    stage: str
    # Why the command ended, for example ``reflected``, ``rejected`` or ``timed_out``.
    end: str
    # The activity a fresh report reflected, only when one did.
    activity: str | None
    sent_at: str
    # The name of the DP 107 payload that reflected a ``stop``, ``map_saving``.
    # On the owned E15 it marks the dock arrival, about 30 seconds after the
    # write, because a stop ends the task and the mower returns by itself.
    payload: str | None = None
    # When the library received the report that reflected the command: the
    # activity report or, for a stop, the map-saving payload.
    observed_at: datetime | None = None


def parse_command_outcome(document: Any, mower_id: str, kind: str) -> BridgeCommandOutcome:
    """Validate a contract 1 command answer for exactly the command that was sent."""
    if not isinstance(document, dict):
        raise BridgeClientError("invalid_document")
    if (
        document.get("contract") != STATE_CONTRACT
        or document.get("id") != mower_id
        or document.get("command") != kind
    ):
        raise BridgeClientError("invalid_document")
    result = document.get("result")
    stage = document.get("stage")
    end = document.get("end")
    sent_at = document.get("sent_at")
    if result not in _COMMAND_RESULTS:
        raise BridgeClientError("invalid_document")
    if not (isinstance(stage, str) and isinstance(end, str) and isinstance(sent_at, str)):
        raise BridgeClientError("invalid_document")
    activity_field = document.get("activity")
    activity: str | None = None
    reflected_at: datetime | None = None
    if activity_field is not None:
        if not isinstance(activity_field, dict) or not isinstance(activity_field.get("value"), str):
            raise BridgeClientError("invalid_document")
        activity = activity_field["value"]
        reflected_at = _report_time(activity_field)
    payload_field = document.get("payload")
    payload: str | None = None
    if payload_field is not None:
        if not isinstance(payload_field, dict) or not isinstance(payload_field.get("name"), str):
            raise BridgeClientError("invalid_document")
        payload = payload_field["name"]
        reflected_at = _report_time(payload_field)
    if result == "confirmed" and activity is None and payload is None:
        # Confirmed means a fresh report reflected the expected activity or,
        # for a stop, the map-saving payload.
        raise BridgeClientError("invalid_document")
    return BridgeCommandOutcome(
        result=result,
        stage=stage,
        end=end,
        activity=activity,
        sent_at=sent_at,
        payload=payload,
        observed_at=reflected_at,
    )


def _report_time(field: dict[str, Any]) -> datetime | None:
    """The library's receipt time of one reflecting report, or None when unreadable.

    The time only dates the reflected activity. An unreadable one leaves the
    outcome itself intact and records no activity.
    """
    raw = field.get("observed_at")
    observed_at = dt_util.parse_datetime(raw) if isinstance(raw, str) else None
    if observed_at is None or observed_at.tzinfo is None:
        return None
    return observed_at


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
    """Bridge access over Home Assistant's shared aiohttp session, one request per call."""

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

    async def async_send_command(self, mower_id: str, kind: str) -> BridgeCommandOutcome:
        """Send one opt-in command exactly once and return the library's outcome.

        The request carries no body. A ``BridgeClientError`` whose code passes
        :func:`refused_before_write` wrote nothing, any other failure leaves the
        write uncertain and the caller must never repeat it blindly.
        """
        if not _MOWER_ID_PATTERN.fullmatch(mower_id):
            raise BridgeClientError("invalid_mower_id")
        if kind not in COMMAND_CLASSES:
            raise BridgeClientError("command_unsupported")
        document = await self._async_request(
            "POST", f"/v1/mowers/{mower_id}/commands/{kind}", _COMMAND_TIMEOUT
        )
        return parse_command_outcome(document, mower_id, kind)

    async def _async_get(self, path: str) -> dict[str, Any]:
        return await self._async_request("GET", path, _REQUEST_TIMEOUT)

    async def _async_request(
        self, method: str, path: str, timeout: aiohttp.ClientTimeout
    ) -> dict[str, Any]:
        session = async_get_clientsession(self._hass)
        ssl: bool | aiohttp.Fingerprint = True
        if self._settings.certificate_fingerprint is not None:
            ssl = aiohttp.Fingerprint(self._settings.certificate_fingerprint)
        headers = {
            "Authorization": f"Bearer {self._settings.token}",
            "Accept": "application/json",
        }
        try:
            async with session.request(
                method,
                f"{self._settings.base_url}{path}",
                headers=headers,
                ssl=ssl,
                timeout=timeout,
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
