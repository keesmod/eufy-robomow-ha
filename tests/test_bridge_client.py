"""Tests for the authenticated mower bridge client. No network, no hardware."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any, cast
from unittest.mock import patch

import aiohttp
import pytest

from homeassistant.core import HomeAssistant

from custom_components.eufy_robomow.bridge_client import (
    BridgeClient,
    BridgeClientError,
    BridgeSettings,
    BridgeSettingsError,
    parse_state_document,
    resolve_mower_id,
)

TOKEN = "synthetic-bridge-token-0123456789abcdef"
MOWER_ID = "a" * 64
OTHER_ID = "b" * 64
OBSERVED_AT = "2026-09-19T10:00:01.250Z"
# Library 0.15.0 lists DP 107 once per confirmed status definition.
STATUS_DP = ["107", "107", "107"]


def _status(activity: str) -> dict[str, Any]:
    return {
        "state": "reported",
        "value": activity,
        "dp": STATUS_DP,
        "source": "local-tuya-3.5",
        "observedAt": OBSERVED_AT,
    }


def _settings(**overrides: str) -> BridgeSettings:
    values = {
        "base_url": "http://127.0.0.1:8090",
        "token": TOKEN,
        "mower_id": "",
        "certificate_fingerprint": "",
    }
    values.update(overrides)
    return BridgeSettings.from_values(**values)


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "contract": 1,
        "id": MOWER_ID,
        "source": "local-tuya-3.5",
        "observed_at": OBSERVED_AT,
        "age_ms": 12,
        "stale": False,
        "error": None,
        "status": {"state": "missing", "dp": STATUS_DP},
        "battery": {
            "state": "reported",
            "value": {"percent": 85},
            "dp": ["8"],
            "source": "local-tuya-3.5",
            "observedAt": OBSERVED_AT,
        },
        "progress": {"state": "unconfirmed"},
        "network": {
            "state": "reported",
            "value": {"kind": "wifi", "signalPercent": 70},
            "dp": ["134", "109"],
            "source": "local-tuya-3.5",
            "observedAt": OBSERVED_AT,
        },
    }
    document.update(overrides)
    return document


def test_settings_accept_http_and_https_and_normalize_values() -> None:
    settings = _settings(base_url="https://bridge.example.test:8443/", mower_id=MOWER_ID.upper())
    assert settings.base_url == "https://bridge.example.test:8443"
    assert settings.mower_id == MOWER_ID
    assert settings.certificate_fingerprint is None
    pinned = _settings(base_url="https://bridge.example.test", certificate_fingerprint="AB:" * 31 + "AB")
    assert pinned.certificate_fingerprint == bytes.fromhex("ab" * 32)
    assert _settings().mower_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "ftp://bridge.example.test"},
        {"base_url": "http://user:secret@bridge.example.test"},
        {"base_url": "http://bridge.example.test/?token=x"},
        {"base_url": "http://bridge.example.test/#fragment"},
        {"base_url": ""},
        {"token": "short"},
        {"token": f"{TOKEN} with space"},
        {"token": ""},
        {"mower_id": "not-a-mower-id"},
        {"certificate_fingerprint": "ab" * 32},
        {"base_url": "https://bridge.example.test", "certificate_fingerprint": "zz" * 32},
    ],
)
def test_settings_reject_unsafe_values(overrides: dict[str, str]) -> None:
    with pytest.raises(BridgeSettingsError):
        _settings(**overrides)


def test_resolve_mower_id_needs_exactly_one_or_an_explicit_id() -> None:
    one = {"contract": 1, "mowers": [{"id": MOWER_ID}]}
    two = {"contract": 1, "mowers": [{"id": MOWER_ID}, {"id": OTHER_ID}]}
    assert resolve_mower_id(one, None) == MOWER_ID
    assert resolve_mower_id(two, OTHER_ID) == OTHER_ID
    with pytest.raises(BridgeClientError, match="mower_selection_required"):
        resolve_mower_id(two, None)
    with pytest.raises(BridgeClientError, match="unknown_mower"):
        resolve_mower_id(one, OTHER_ID)
    with pytest.raises(BridgeClientError, match="no_mowers"):
        resolve_mower_id({"contract": 1, "mowers": []}, None)
    with pytest.raises(BridgeClientError, match="invalid_document"):
        resolve_mower_id({"contract": 1, "mowers": [{"name": "x"}]}, None)
    with pytest.raises(BridgeClientError, match="invalid_document"):
        resolve_mower_id(["not", "a", "document"], None)


def test_state_document_maps_only_reported_typed_fields() -> None:
    telemetry = parse_state_document(_document(), MOWER_ID)
    assert telemetry.observed_at == datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC)
    assert telemetry.age_ms == 12
    assert telemetry.stale is False
    assert telemetry.error is None
    assert telemetry.status == "missing"
    assert telemetry.activity is None, "a missing status is never turned into an activity"
    assert telemetry.dps == {"8": 85, "134": "Wifi", "109": 70}

    reported = parse_state_document(
        _document(
            status=_status("mowing"),
            battery={"state": "missing", "dp": ["8"]},
            network={"state": "reported", "value": {"kind": "cellular"}, "dp": ["134"], "source": "local-tuya-3.5", "observedAt": OBSERVED_AT},
        ),
        MOWER_ID,
    )
    assert reported.status == "reported"
    assert reported.activity == "mowing"
    assert reported.dps == {"134": "Cellular"}

    stale = parse_state_document(_document(stale=True, error="mower_local_unreachable", age_ms=30_000), MOWER_ID)
    assert stale.stale is True
    assert stale.error == "mower_local_unreachable"
    assert stale.age_ms == 30_000


@pytest.mark.parametrize("activity", ["mowing", "paused", "returning"])
def test_state_document_reports_each_confirmed_activity(activity: str) -> None:
    telemetry = parse_state_document(_document(status=_status(activity)), MOWER_ID)
    assert telemetry.status == "reported"
    assert telemetry.activity == activity
    assert telemetry.observed_at == datetime(2026, 9, 19, 10, 0, 1, 250000, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"state": "missing", "dp": STATUS_DP}, "missing"),
        ({"state": "invalid", "dp": STATUS_DP}, "invalid"),
        ({"state": "unconfirmed", "level": "observed"}, "unconfirmed"),
        ({"state": "unconfirmed"}, "unconfirmed"),
    ],
)
def test_state_document_keeps_missing_and_invalid_status_explicit(status: dict[str, Any], expected: str) -> None:
    telemetry = parse_state_document(_document(status=status), MOWER_ID)
    assert telemetry.status == expected
    assert telemetry.activity is None, "only a reported status carries an activity"
    assert telemetry.dps == {"8": 85, "134": "Wifi", "109": 70}, "the other fields are unaffected"


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract": 2},
        {"id": OTHER_ID},
        {"source": "cloud"},
        {"observed_at": "yesterday"},
        {"observed_at": "2026-09-19T10:00:01"},
        {"stale": "no"},
        {"error": 5},
        {"age_ms": 1.5},
        {"battery": {"state": "reported", "value": {"percent": 150}}},
        {"battery": {"state": "reported", "value": "85"}},
        {"network": {"state": "reported", "value": {"signalPercent": -1}}},
        {"status": {"state": "reported", "value": {"activity": "mowing"}}},
        {"status": {"state": "reported", "dp": STATUS_DP}},
        {"status": {"state": "stale", "dp": STATUS_DP}},
        {"status": {"dp": STATUS_DP}},
        {"progress": "unconfirmed"},
    ],
)
def test_state_document_rejects_invalid_shapes(overrides: dict[str, Any]) -> None:
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_state_document(_document(**overrides), MOWER_ID)
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_state_document("not a document", MOWER_ID)


class _FakeResponse:
    def __init__(self, status: int, body: bytes | Exception, content_length: int | None = None) -> None:
        self.status = status
        self._body = body
        self.content_length = len(body) if isinstance(body, bytes) and content_length is None else content_length
        self.content = self

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self._body, Exception):
            raise self._body
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def read(self, size: int) -> bytes:
        assert isinstance(self._body, bytes)
        return self._body[:size]


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append((url, kwargs))
        return self._responses.pop(0)


def _client(session: _FakeSession, settings: BridgeSettings | None = None) -> tuple[BridgeClient, Any]:
    client = BridgeClient(cast(HomeAssistant, object()), settings or _settings())
    patcher = patch(
        "custom_components.eufy_robomow.bridge_client.async_get_clientsession",
        return_value=session,
    )
    return client, patcher


def _json(value: Any) -> bytes:
    return json.dumps(value).encode()


def test_client_sends_the_bearer_token_and_returns_documents() -> None:
    state = {"protocol": 1, "bridge": "eufy-robomow-bridge", "version": "0.2.0"}
    session = _FakeSession(
        [
            _FakeResponse(200, _json(state)),
            _FakeResponse(200, _json({"contract": 1, "mowers": []})),
            _FakeResponse(200, _json(_document())),
        ]
    )
    client, patcher = _client(session)
    with patcher:
        assert asyncio.run(client.async_state()) == state
        assert asyncio.run(client.async_mowers()) == {"contract": 1, "mowers": []}
        assert asyncio.run(client.async_mower_state(MOWER_ID))["id"] == MOWER_ID
    urls = [url for url, _ in session.requests]
    assert urls == [
        "http://127.0.0.1:8090/v1/state",
        "http://127.0.0.1:8090/v1/mowers",
        f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/state",
    ]
    for _, kwargs in session.requests:
        assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
        assert kwargs["ssl"] is True
        assert kwargs["timeout"].total == 15


def test_client_pins_the_certificate_when_configured_and_refuses_bad_ids() -> None:
    session = _FakeSession([_FakeResponse(200, _json({"contract": 1, "mowers": []}))])
    client, patcher = _client(
        session,
        _settings(base_url="https://bridge.example.test", certificate_fingerprint="ab" * 32),
    )
    with patcher:
        asyncio.run(client.async_mowers())
        with pytest.raises(BridgeClientError, match="invalid_mower_id"):
            asyncio.run(client.async_mower_state("../state"))
    assert isinstance(session.requests[0][1]["ssl"], aiohttp.Fingerprint)
    assert len(session.requests) == 1, "an invalid id never becomes a request"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_FakeResponse(401, _json({"error": "unauthorized"})), "unauthorized"),
        (_FakeResponse(503, _json({"error": "authentication_required"})), "authentication_required"),
        (_FakeResponse(404, _json({"error": "unknown_mower"})), "unknown_mower"),
        (_FakeResponse(500, b"<html>oops</html>"), "http_500"),
        (_FakeResponse(503, _json({"error": "Bad Code!"})), "http_503"),
        (_FakeResponse(200, b"not json"), "invalid_response"),
        (_FakeResponse(200, _json(["list"])), "invalid_response"),
        (_FakeResponse(200, b"{}", content_length=10_000_000), "response_too_large"),
        (_FakeResponse(200, b"x" * (256 * 1024 + 1), content_length=None), "response_too_large"),
        (_FakeResponse(200, aiohttp.ClientConnectorError(cast(Any, None), OSError("refused"))), "cannot_connect"),
        (_FakeResponse(200, asyncio.TimeoutError()), "timeout"),
        (_FakeResponse(200, aiohttp.ClientPayloadError("broken")), "transport_failed"),
    ],
)
def test_client_reduces_failures_to_stable_codes(response: _FakeResponse, code: str) -> None:
    client, patcher = _client(_FakeSession([response]))
    with patcher, pytest.raises(BridgeClientError) as failure:
        asyncio.run(client.async_mowers())
    assert failure.value.code == code
    assert "oops" not in str(failure.value)


def test_client_state_requires_the_bridge_identity() -> None:
    client, patcher = _client(_FakeSession([_FakeResponse(200, _json({"protocol": 1, "bridge": "other"}))]))
    with patcher, pytest.raises(BridgeClientError, match="invalid_document"):
        asyncio.run(client.async_state())
