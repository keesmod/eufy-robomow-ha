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
    BridgeCommandOutcome,
    BridgeSettingOutcome,
    BridgeSettings,
    BridgeSettingsError,
    parse_command_outcome,
    parse_setting_outcome,
    parse_state_document,
    parse_work_parameter_outcome,
    refused_before_write,
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


def _outcome(**overrides: Any) -> dict[str, Any]:
    """A contract 1 command answer as bridge 0.5.0 serves it, without raw data points."""
    document: dict[str, Any] = {
        "contract": 1,
        "id": MOWER_ID,
        "command": "start",
        "result": "confirmed",
        "write": {"dp": "1", "code": "switch_go", "value": True},
        "sent_at": "2026-09-19T16:42:38.199Z",
        "stage": "reflected",
        "end": "reflected",
        "before_observed_at": "2026-09-19T16:42:38.198Z",
        "reply": {"observed_at": "2026-09-19T16:42:38.202Z", "return_code_zero": True, "rejected": False},
        "acknowledgement": {"observed_at": "2026-09-19T16:42:39.150Z", "sequence": 63859, "dp": "1"},
        "activity": {"observed_at": "2026-09-19T16:42:39.351Z", "sequence": 63860, "value": "mowing"},
        "payload": None,
        "reports": 3,
    }
    document.update(overrides)
    return document


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
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append((method, url, kwargs))
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
    assert [(method, url) for method, url, _ in session.requests] == [
        ("GET", "http://127.0.0.1:8090/v1/state"),
        ("GET", "http://127.0.0.1:8090/v1/mowers"),
        ("GET", f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/state"),
    ]
    for _, _, kwargs in session.requests:
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
    assert isinstance(session.requests[0][2]["ssl"], aiohttp.Fingerprint)
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


def test_client_posts_a_command_once_without_a_body_and_returns_the_outcome() -> None:
    session = _FakeSession([_FakeResponse(200, _json(_outcome()))])
    client, patcher = _client(session)
    with patcher:
        outcome = asyncio.run(client.async_send_command(MOWER_ID, "start"))
    assert outcome == BridgeCommandOutcome(
        result="confirmed",
        stage="reflected",
        end="reflected",
        activity="mowing",
        sent_at="2026-09-19T16:42:38.199Z",
        observed_at=datetime(2026, 9, 19, 16, 42, 39, 351000, tzinfo=UTC),
    )
    assert len(session.requests) == 1, "a command is sent exactly once"
    method, url, kwargs = session.requests[0]
    assert method == "POST"
    assert url == f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/commands/start"
    assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    assert kwargs["ssl"] is True
    assert "data" not in kwargs and "json" not in kwargs, "the bridge ignores request bodies"
    assert kwargs["timeout"].total == 75, "connect time plus the bridge's 60 second read-back bound"
    assert kwargs["timeout"].connect == 5


def test_client_pins_the_certificate_on_commands_too() -> None:
    session = _FakeSession([_FakeResponse(200, _json(_outcome(command="pause", activity={"observed_at": "2026-09-19T16:42:39.351Z", "sequence": 1, "value": "paused"})))])
    client, patcher = _client(
        session,
        _settings(base_url="https://bridge.example.test", certificate_fingerprint="ab" * 32),
    )
    with patcher:
        outcome = asyncio.run(client.async_send_command(MOWER_ID, "pause"))
    assert outcome.activity == "paused"
    assert isinstance(session.requests[0][2]["ssl"], aiohttp.Fingerprint)


@pytest.mark.parametrize(("mower_id", "kind", "code"), [
    (MOWER_ID, "dock", "command_unsupported"),
    (MOWER_ID, "return", "command_unsupported"),
    ("../commands", "start", "invalid_mower_id"),
    (MOWER_ID.upper(), "start", "invalid_mower_id"),
])
def test_client_refuses_unknown_classes_and_bad_ids_before_any_request(mower_id: str, kind: str, code: str) -> None:
    session = _FakeSession([])
    client, patcher = _client(session)
    with patcher, pytest.raises(BridgeClientError) as failure:
        asyncio.run(client.async_send_command(mower_id, kind))
    assert failure.value.code == code
    assert session.requests == []


@pytest.mark.parametrize(
    ("response", "code", "status"),
    [
        (_FakeResponse(403, _json({"error": "control_disabled"})), "control_disabled", 403),
        (_FakeResponse(404, _json({"error": "not_found"})), "not_found", 404),
        (_FakeResponse(409, _json({"error": "command_unsupported"})), "command_unsupported", 409),
        (_FakeResponse(409, _json({"error": "command_in_progress"})), "command_in_progress", 409),
        (_FakeResponse(409, _json({"error": "telemetry_stale"})), "telemetry_stale", 409),
        (_FakeResponse(409, _json({"error": "mower_command_map_saving"})), "mower_command_map_saving", 409),
        (_FakeResponse(503, _json({"error": "mower_local_disconnected"})), "mower_local_disconnected", 503),
        (_FakeResponse(503, _json({"error": "request_aborted"})), "request_aborted", 503),
        (_FakeResponse(401, _json({"error": "unauthorized"})), "unauthorized", 401),
        (_FakeResponse(500, b"<html>oops</html>"), "http_500", 500),
        (_FakeResponse(200, b"not json"), "invalid_response", 200),
        (_FakeResponse(200, b"{}", content_length=10_000_000), "response_too_large", 200),
        (_FakeResponse(200, aiohttp.ClientConnectorError(cast(Any, None), OSError("refused"))), "cannot_connect", None),
        (_FakeResponse(200, asyncio.TimeoutError()), "timeout", None),
        (_FakeResponse(200, aiohttp.ClientPayloadError("broken")), "transport_failed", None),
    ],
)
def test_client_reduces_command_failures_to_the_same_stable_codes(response: _FakeResponse, code: str, status: int | None) -> None:
    session = _FakeSession([response])
    client, patcher = _client(session)
    with patcher, pytest.raises(BridgeClientError) as failure:
        asyncio.run(client.async_send_command(MOWER_ID, "resume"))
    assert failure.value.code == code
    assert failure.value.status == status
    assert "oops" not in str(failure.value)
    assert len(session.requests) == 1, "a failed command is never retried"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"result": "failed", "stage": "sent", "end": "rejected", "activity": None}, ("failed", "sent", "rejected", None)),
        ({"result": "uncertain", "stage": "acknowledged", "end": "timed_out", "activity": None}, ("uncertain", "acknowledged", "timed_out", None)),
        ({"result": "uncertain", "stage": "acknowledged", "end": "report_limit", "activity": {"observed_at": "x", "sequence": 2, "value": "returning"}}, ("uncertain", "acknowledged", "report_limit", "returning")),
    ],
)
def test_command_outcome_keeps_failed_and_uncertain_explicit(overrides: dict[str, Any], expected: tuple[Any, ...]) -> None:
    outcome = parse_command_outcome(_outcome(**overrides), MOWER_ID, "start")
    assert (outcome.result, outcome.stage, outcome.end, outcome.activity) == expected
    assert outcome.sent_at == "2026-09-19T16:42:38.199Z"


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract": 2},
        {"id": OTHER_ID},
        {"command": "pause"},
        {"result": "maybe"},
        {"result": None},
        {"stage": None},
        {"end": 3},
        {"sent_at": None},
        {"activity": "mowing"},
        {"activity": {"observed_at": "x", "sequence": 1}},
        {"activity": {"value": 1}},
        {"result": "confirmed", "activity": None},
        {"payload": "map_saving"},
        {"payload": {"observed_at": "x", "sequence": 1}},
        {"payload": {"name": 5}},
    ],
)
def test_command_outcome_rejects_invalid_shapes(overrides: dict[str, Any]) -> None:
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_command_outcome(_outcome(**overrides), MOWER_ID, "start")
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_command_outcome(["not", "a", "document"], MOWER_ID, "start")


STOP_PAYLOAD = {"observed_at": "2026-09-20T10:58:18.236Z", "sequence": 47724, "name": "map_saving"}


def test_client_posts_a_stop_and_returns_the_map_saving_payload() -> None:
    answer = _outcome(
        command="stop",
        write={"dp": "1", "code": "switch_go", "value": False},
        activity=None,
        payload=STOP_PAYLOAD,
    )
    session = _FakeSession([_FakeResponse(200, _json(answer))])
    client, patcher = _client(session)
    with patcher:
        outcome = asyncio.run(client.async_send_command(MOWER_ID, "stop"))
    assert outcome.result == "confirmed"
    assert outcome.activity is None, "a stop reflects through the payload, not an activity"
    assert outcome.payload == "map_saving"
    method, url, _ = session.requests[0]
    assert (method, url.endswith(f"/v1/mowers/{MOWER_ID}/commands/stop")) == ("POST", True)
    assert len(session.requests) == 1


def test_command_outcome_keeps_a_stop_without_its_payload_uncertain() -> None:
    answer = _outcome(command="stop", result="uncertain", stage="acknowledged", end="timed_out", activity=None)
    outcome = parse_command_outcome(answer, MOWER_ID, "stop")
    assert (outcome.result, outcome.activity, outcome.payload) == ("uncertain", None, None)
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_command_outcome(_outcome(command="stop", activity=None), MOWER_ID, "stop")


def test_client_validates_the_command_answer_against_the_sent_command() -> None:
    session = _FakeSession([_FakeResponse(200, _json(_outcome(command="start")))])
    client, patcher = _client(session)
    with patcher, pytest.raises(BridgeClientError, match="invalid_document"):
        asyncio.run(client.async_send_command(MOWER_ID, "resume"))


def test_refusals_before_any_write_are_distinguished_from_uncertain_failures() -> None:
    for code in (
        "control_disabled", "not_found", "invalid_mower_id", "unknown_mower", "command_unsupported",
        "command_in_progress", "telemetry_stale", "mower_host_unconfigured", "bridge_not_running",
        "authentication_required", "mower_request_failed", "mower_local_unreachable",
        "mower_local_authentication_failed", "mower_local_key_invalid", "mower_local_busy",
        "unauthorized", "cannot_connect", "certificate_mismatch", "certificate_invalid",
        "mower_command_undeclared", "mower_command_evidence_missing", "mower_command_map_saving",
        "mower_command_already_set", "settings_disabled", "invalid_setting_value",
        "mower_settings_disabled", "mower_setting_invalid", "mower_setting_read_only",
        "mower_setting_undeclared", "mower_setting_evidence_missing", "mower_setting_map_saving",
        "mower_setting_already_set",
    ):
        assert refused_before_write(code) is True, code
    for code in (
        "timeout", "transport_failed", "mower_local_disconnected", "request_aborted",
        "internal_error", "invalid_response", "invalid_document", "response_too_large", "http_500",
    ):
        assert refused_before_write(code) is False, code


# ── Settings, bridge 0.10.0 ────────────────────────────────────────────────────


def _reported_setting(value: Any, writable: bool, **bounds: Any) -> dict[str, Any]:
    return {"state": "reported", "value": value, "writable": writable, **bounds}


SETTINGS_BLOCK: dict[str, Any] = {
    "mow_height": _reported_setting(40, True, min=25, max=75, step=1, unit="mm"),
    "volume": _reported_setting(20, True, min=0, max=100, step=1, unit="%"),
    "smart_no_go_zones": _reported_setting(True, True),
    "sparse_lawn_optimization": _reported_setting(False, True),
    "rain_auto_return": _reported_setting(True, False),
    "child_lock": _reported_setting(True, False),
    "bird_view_capture": _reported_setting(False, False),
}


def _setting_outcome(**overrides: Any) -> dict[str, Any]:
    """A contract 1 setting answer as bridge 0.10.0 serves it, without raw data points."""
    document: dict[str, Any] = {
        "contract": 1,
        "id": MOWER_ID,
        "setting": "mow_height",
        "result": "confirmed",
        "write": {"dp": "110", "code": "mow_height", "value": 45},
        "previous": 40,
        "sent_at": "2026-09-25T09:00:00.100Z",
        "stage": "reflected",
        "end": "reflected",
        "before_observed_at": "2026-09-25T09:00:00.050Z",
        "reply": {"observed_at": "2026-09-25T09:00:00.120Z", "return_code_zero": True, "rejected": False},
        "reflection": {"observed_at": "2026-09-25T09:00:00.600Z", "sequence": 71, "value": 45},
        "other": None,
        "reports": 1,
    }
    document.update(overrides)
    return document


def test_state_document_maps_reported_settings_onto_their_data_points() -> None:
    telemetry = parse_state_document(_document(settings=SETTINGS_BLOCK), MOWER_ID)
    assert telemetry.dps == {
        "8": 85, "134": "Wifi", "109": 70,
        "110": 40, "26": 20, "132": True, "141": False, "101": True, "47": True, "133": False,
    }
    assert telemetry.settings_writable == {
        "110": True, "26": True, "132": True, "141": True, "101": False, "47": False, "133": False,
    }
    assert parse_state_document(_document(), MOWER_ID).settings_writable == {}, "an older bridge has no settings"


def test_state_document_skips_settings_it_cannot_read_without_failing_the_poll() -> None:
    odd = {
        "mow_height": {"state": "invalid"},
        "volume": {"state": "missing"},
        "smart_no_go_zones": _reported_setting(1, True),
        "sparse_lawn_optimization": _reported_setting("false", True),
        "rain_auto_return": "reported",
        "child_lock": _reported_setting(True, "yes"),
    }
    telemetry = parse_state_document(_document(settings=odd), MOWER_ID)
    assert telemetry.dps == {"8": 85, "134": "Wifi", "109": 70, "47": True}
    assert telemetry.settings_writable == {"47": False}, "only a boolean true is writable"
    for block in (None, [], "settings", {"mow_height": _reported_setting(True, True)}):
        assert parse_state_document(_document(settings=block), MOWER_ID).dps == {"8": 85, "134": "Wifi", "109": 70}


def test_client_posts_a_setting_once_with_the_value_in_the_query() -> None:
    switch = _setting_outcome(
        setting="smart_no_go_zones",
        write={"dp": "132", "code": "enable_smart_forbid_zone", "value": False},
        previous=True,
        reflection={"observed_at": "2026-09-25T09:00:00.600Z", "sequence": 72, "value": False},
    )
    session = _FakeSession([_FakeResponse(200, _json(_setting_outcome())), _FakeResponse(200, _json(switch))])
    client, patcher = _client(session)
    with patcher:
        outcome = asyncio.run(client.async_set_setting(MOWER_ID, "mow_height", 45))
        toggled = asyncio.run(client.async_set_setting(MOWER_ID, "smart_no_go_zones", False))
    assert outcome == BridgeSettingOutcome(
        result="confirmed",
        stage="reflected",
        end="reflected",
        previous=40,
        observed_at=datetime(2026, 9, 25, 9, 0, 0, 600000, tzinfo=UTC),
    )
    assert toggled.previous is True
    assert [(method, url) for method, url, _ in session.requests] == [
        ("POST", f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/settings/mow_height?value=45"),
        ("POST", f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/settings/smart_no_go_zones?value=false"),
    ], "each setting is sent exactly once"
    for _, _, kwargs in session.requests:
        assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
        assert "data" not in kwargs and "json" not in kwargs, "the bridge ignores request bodies"
        assert kwargs["timeout"].total == 75


@pytest.mark.parametrize(
    ("mower_id", "key", "value", "code"),
    [
        (MOWER_ID, "rain_auto_return", False, "mower_setting_read_only"),
        (MOWER_ID, "child_lock", False, "mower_setting_read_only"),
        (MOWER_ID, "bird_view_capture", True, "mower_setting_read_only"),
        (MOWER_ID, "cut_height", 45, "not_found"),
        (MOWER_ID, "mow_height", True, "invalid_setting_value"),
        (MOWER_ID, "mow_height", "45", "invalid_setting_value"),
        (MOWER_ID, "volume", 30.5, "invalid_setting_value"),
        (MOWER_ID, "smart_no_go_zones", 1, "invalid_setting_value"),
        ("not-a-mower-id", "mow_height", 45, "invalid_mower_id"),
    ],
)
def test_client_refuses_read_only_and_malformed_settings_before_any_request(
    mower_id: str, key: str, value: Any, code: str
) -> None:
    session = _FakeSession([])
    client, patcher = _client(session)
    with patcher, pytest.raises(BridgeClientError, match=code):
        asyncio.run(client.async_set_setting(mower_id, key, value))
    assert session.requests == []
    assert refused_before_write(code) or code == "not_found"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"result": "failed", "stage": "sent", "end": "rejected", "reflection": None}, ("failed", "rejected")),
        ({"result": "uncertain", "stage": "sent", "end": "timed_out", "reflection": None}, ("uncertain", "timed_out")),
    ],
)
def test_setting_outcome_keeps_failed_and_uncertain_explicit(overrides: dict[str, Any], expected: tuple[str, str]) -> None:
    outcome = parse_setting_outcome(_setting_outcome(**overrides), MOWER_ID, "mow_height", 45)
    assert (outcome.result, outcome.end) == expected
    assert outcome.observed_at is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract": 2},
        {"id": OTHER_ID},
        {"setting": "volume"},
        {"result": "done"},
        {"stage": None},
        {"write": {"dp": "110", "code": "mow_height", "value": 50}},
        {"write": None},
        {"previous": "40"},
        {"previous": True},
        {"reflection": None},
        {"reflection": {"observed_at": "2026-09-25T09:00:00.600Z", "sequence": 71, "value": 50}},
    ],
)
def test_setting_outcome_rejects_invalid_shapes(overrides: dict[str, Any]) -> None:
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_setting_outcome(_setting_outcome(**overrides), MOWER_ID, "mow_height", 45)


def test_setting_outcome_never_takes_true_for_one() -> None:
    document = _setting_outcome(
        setting="sparse_lawn_optimization",
        write={"dp": "141", "code": "sparse_lawn_optimization", "value": 1},
        previous=False,
        reflection={"observed_at": "2026-09-25T09:00:00.600Z", "sequence": 71, "value": 1},
    )
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_setting_outcome(document, MOWER_ID, "sparse_lawn_optimization", True)


# ── Work parameters, bridge 0.11.0 ─────────────────────────────────────────────


def _speed(value: Any, writable: Any, options: list[str]) -> dict[str, Any]:
    return {"value": value, "writable": writable, "options": options}


MOW_SPEEDS = ["low", "medium", "adaptive_high"]
BLADE_SPEEDS = ["low", "medium", "high"]
WORK_PARAMETERS: dict[str, Any] = {
    "state": "reported",
    "source": "cloud",
    "observed_at": "2026-09-25T09:00:00.000Z",
    "error": None,
    "mow_speed": _speed("medium", True, MOW_SPEEDS),
    "blade_speed": _speed("high", True, BLADE_SPEEDS),
    "edge_distance": 120,
    "mow_spacing": 70,
    "direction": {"mode": "single", "single_angle": 30, "current_angle": 30},
}


def _work_parameter_outcome(**overrides: Any) -> dict[str, Any]:
    """A contract 1 work parameter answer as bridge 0.11.0 serves it, without raw data."""
    document: dict[str, Any] = {
        "contract": 1,
        "id": MOWER_ID,
        "setting": "blade_speed",
        "result": "confirmed",
        "write": {"dp": "155", "code": "reserved_raw_155", "field": 6, "value": "high"},
        "previous": "medium",
        "cloud_observed_at": "2026-09-25T08:59:00.000Z",
        "sent_at": "2026-09-25T09:00:00.100Z",
        "stage": "reflected",
        "end": "reflected",
        "before_observed_at": "2026-09-25T09:00:00.050Z",
        "reply": {"observed_at": "2026-09-25T09:00:00.120Z", "return_code_zero": True, "rejected": False},
        "reflection": {"observed_at": "2026-09-25T09:00:00.300Z", "sequence": 81, "value": "high"},
        "other": None,
        "reports": 1,
    }
    document.update(overrides)
    return document


def test_state_document_maps_the_work_parameters_onto_the_cloud_keys() -> None:
    telemetry = parse_state_document(_document(work_parameters=WORK_PARAMETERS), MOWER_ID)
    assert telemetry.dps == {
        "8": 85, "134": "Wifi", "109": 70,
        "cloud_travel_speed": "normal",
        "cloud_blade_speed": "fast",
        "cloud_edge_mm": 120,
        "cloud_path_mm": 70,
        "cloud_pad_direction": 30,
    }, "the speeds use the integration's own options and the rest the device's integers"
    assert telemetry.settings_writable == {"cloud_travel_speed": True, "cloud_blade_speed": True}
    low = {**WORK_PARAMETERS, "mow_speed": _speed("low", True, MOW_SPEEDS), "blade_speed": _speed("low", True, BLADE_SPEEDS)}
    assert parse_state_document(_document(work_parameters=low), MOWER_ID).dps["cloud_travel_speed"] == "slow"
    fast = {**WORK_PARAMETERS, "mow_speed": _speed("adaptive_high", True, MOW_SPEEDS)}
    assert parse_state_document(_document(work_parameters=fast), MOWER_ID).dps["cloud_travel_speed"] == "fast"


def test_state_document_skips_work_parameters_it_cannot_read_without_failing_the_poll() -> None:
    odd = {
        **WORK_PARAMETERS,
        "mow_speed": _speed("auto", False, MOW_SPEEDS),
        "blade_speed": _speed(["high"], "yes", BLADE_SPEEDS),
        "edge_distance": "120",
        "mow_spacing": True,
        "direction": {"mode": "single", "single_angle": None},
    }
    telemetry = parse_state_document(_document(work_parameters=odd), MOWER_ID)
    assert telemetry.dps == {"8": 85, "134": "Wifi", "109": 70}, "an unnamed speed and bad shapes map to nothing"
    assert telemetry.settings_writable == {}
    for block in (None, [], "work", {**WORK_PARAMETERS, "state": "missing"}, {**WORK_PARAMETERS, "state": "unavailable"}):
        parsed = parse_state_document(_document(work_parameters=block), MOWER_ID)
        assert parsed.dps == {"8": 85, "134": "Wifi", "109": 70}
        assert parsed.settings_writable == {}
    not_writable = {**WORK_PARAMETERS, "blade_speed": _speed("high", "true", BLADE_SPEEDS)}
    assert parse_state_document(_document(work_parameters=not_writable), MOWER_ID).settings_writable["cloud_blade_speed"] is False


def test_client_posts_a_work_parameter_once_with_the_library_value_in_the_query() -> None:
    session = _FakeSession([_FakeResponse(200, _json(_work_parameter_outcome()))])
    client, patcher = _client(session)
    with patcher:
        outcome = asyncio.run(client.async_set_work_parameter(MOWER_ID, "blade_speed", "high"))
    assert outcome == BridgeSettingOutcome(
        result="confirmed",
        stage="reflected",
        end="reflected",
        previous="medium",
        observed_at=datetime(2026, 9, 25, 9, 0, 0, 300000, tzinfo=UTC),
    )
    assert [(method, url) for method, url, _ in session.requests] == [
        ("POST", f"http://127.0.0.1:8090/v1/mowers/{MOWER_ID}/settings/blade_speed?value=high"),
    ]
    _, _, kwargs = session.requests[0]
    assert "data" not in kwargs and "json" not in kwargs
    assert kwargs["timeout"].total == 75


@pytest.mark.parametrize(
    ("mower_id", "key", "value", "code"),
    [
        (MOWER_ID, "edge_distance", "100", "not_found"),
        (MOWER_ID, "travel_speed", "fast", "not_found"),
        (MOWER_ID, "mow_speed", "fast", "invalid_setting_value"),
        (MOWER_ID, "mow_speed", "auto", "invalid_setting_value"),
        (MOWER_ID, "blade_speed", "adaptive_high", "invalid_setting_value"),
        ("not-a-mower-id", "blade_speed", "high", "invalid_mower_id"),
    ],
)
def test_client_refuses_unknown_work_parameters_and_values_before_any_request(
    mower_id: str, key: str, value: str, code: str
) -> None:
    session = _FakeSession([])
    client, patcher = _client(session)
    with patcher, pytest.raises(BridgeClientError, match=code):
        asyncio.run(client.async_set_work_parameter(mower_id, key, value))
    assert session.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract": 2},
        {"id": OTHER_ID},
        {"setting": "mow_speed"},
        {"result": "done"},
        {"end": None},
        {"write": {"dp": "155", "code": "reserved_raw_155", "field": 6, "value": "medium"}},
        {"write": None},
        {"previous": "fast"},
        {"previous": 1},
        {"reflection": None},
        {"reflection": {"observed_at": "2026-09-25T09:00:00.300Z", "sequence": 81, "value": "medium"}},
    ],
)
def test_work_parameter_outcome_rejects_invalid_shapes(overrides: dict[str, Any]) -> None:
    with pytest.raises(BridgeClientError, match="invalid_document"):
        parse_work_parameter_outcome(_work_parameter_outcome(**overrides), MOWER_ID, "blade_speed", "high")


def test_work_parameter_outcome_keeps_failed_and_uncertain_explicit() -> None:
    for result, end in (("failed", "rejected"), ("uncertain", "timed_out")):
        outcome = parse_work_parameter_outcome(
            _work_parameter_outcome(result=result, stage="sent", end=end, reflection=None),
            MOWER_ID,
            "blade_speed",
            "high",
        )
        assert (outcome.result, outcome.end, outcome.observed_at) == (result, end, None)
