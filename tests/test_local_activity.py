"""The local backend's activity where the status reply alone is ambiguous."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.eufy_robomow.const import (
    BACKEND_LOCAL,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    OPERATING_MODE_CONTROL,
)
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.lawn_mower import EufyRobomowEntity
from custom_components.eufy_robomow.telemetry import robot_status, task_ambiguous

# The local status reply of 2026-09-24 09:43 UTC, the mower resting in the dock
# with its task flag set while the app showed it idle. Scalar data points only.
RESTING_IN_DOCK = {
    "1": True, "2": False, "8": 100, "26": 0, "47": True, "101": True, "109": 70,
    "110": 40, "114": 43, "118": 100, "125": 36292, "126": 978, "131": 90,
    "134": "Wifi", "139": 0, "146": 1, "170": 2, "181": 0,
}
DOCKED = {**RESTING_IN_DOCK, "1": False}
DEFAULT_PAYLOAD = "AA=="
T0 = datetime(2026, 9, 24, 9, 43, tzinfo=UTC)


def _b64(*values: int) -> str:
    return base64.b64encode(bytes(values)).decode()


MOWING_PAYLOAD = _b64(0x08, 0x02, 0x18, 0x01)


def test_robot_status_reads_the_confirmed_definitions_and_the_default_payload() -> None:
    assert robot_status(DEFAULT_PAYLOAD) == "idle"
    assert robot_status("") == "idle"
    assert robot_status(MOWING_PAYLOAD) == "mowing"
    assert robot_status(_b64(0x08, 0x02, 0x10, 0x09, 0x18, 0x01)) == "mowing", "field 2 takes no part"
    assert robot_status(_b64(0x08, 0x02, 0x18, 0x02)) == "paused"
    assert robot_status(_b64(0x08, 0x01, 0x18, 0x01)) == "returning"
    assert robot_status(_b64(0x08, 0x01, 0x10, 0x01, 0x18, 0x01)) == "returning"
    assert robot_status(_b64(0x10, 0x05, 0x18, 0x01)) == "map_saving"


def test_robot_status_never_guesses() -> None:
    for value in (
        _b64(0x10, 0x05, 0x18, 0x02),  # not exactly the map-saving payload
        _b64(0x10, 0x05, 0x18, 0x01, 0x30, 0x01),  # map saving with another record
        _b64(0x30, 0x01),  # field 6
        _b64(0x20, 0x02),  # field 4, seen once while docked idle on 2026-09-24
        _b64(0x08, 0x02),  # field 3 absent counts as zero, no definition matches
        _b64(0x08, 0x02, 0x08, 0x02, 0x18, 0x01),  # a repeated field
        _b64(0x0A, 0x01, 0x00),  # a length-delimited record
        _b64(0x08),  # truncated
        _b64(0x00, 0x00),  # field number zero
        "not base64!",
        "A" * 200,
        None,
        7,
    ):
        assert robot_status(value) is None, value


def test_the_ambiguous_shape_is_a_set_task_flag_not_paused_after_a_map_save() -> None:
    assert task_ambiguous(RESTING_IN_DOCK) is True
    assert task_ambiguous({"1": True, "118": 100}) is True, "an omitted DP 2 is not paused"
    assert task_ambiguous({"1": True, "2": False, "118": 0}) is False
    assert task_ambiguous({"1": True, "2": True, "118": 100}) is False
    assert task_ambiguous(DOCKED) is False
    assert task_ambiguous({}) is False


def _entry() -> SimpleNamespace:
    return SimpleNamespace(
        data={CONF_HOST: "192.0.2.1", CONF_DEVICE_ID: "synthetic-device", CONF_LOCAL_KEY: "not-a-real-local-key"},
        options={},
        entry_id="test-entry",
    )


def _local_entity(dps: dict[str, Any], status: str | None = None, polled_at: datetime | None = None) -> EufyRobomowEntity:
    coordinator = object.__new__(EufyMowerCoordinator)
    coordinator.backend = BACKEND_LOCAL
    coordinator.operating_mode = OPERATING_MODE_CONTROL
    coordinator.local_dps = dps
    coordinator.command = None
    coordinator.last_local_update = T0
    coordinator.ambiguous_since = T0 if task_ambiguous(dps) else None
    coordinator.cloud_robot_status = status
    coordinator.cloud_polled_at = polled_at
    return EufyRobomowEntity(coordinator, cast(Any, _entry()))


def test_a_fresh_default_payload_makes_the_ambiguous_shape_docked() -> None:
    fresh = T0 + timedelta(seconds=2)
    assert _local_entity(RESTING_IN_DOCK, "idle", fresh).activity == LawnMowerActivity.DOCKED
    assert _local_entity(RESTING_IN_DOCK, "map_saving", fresh).activity == LawnMowerActivity.DOCKED, "the map save at the arrival"
    assert _local_entity(RESTING_IN_DOCK, "mowing", fresh).activity == LawnMowerActivity.MOWING
    assert _local_entity(RESTING_IN_DOCK, "paused", fresh).activity == LawnMowerActivity.PAUSED
    assert _local_entity(RESTING_IN_DOCK, "returning", fresh).activity == LawnMowerActivity.RETURNING
    assert _local_entity(RESTING_IN_DOCK, None, fresh).activity == LawnMowerActivity.MOWING, "an unread payload keeps the earlier reading"
    assert _local_entity(RESTING_IN_DOCK).activity == LawnMowerActivity.MOWING, "no cloud poll keeps the earlier reading"
    older = T0 - timedelta(seconds=30)
    assert _local_entity(RESTING_IN_DOCK, "idle", older).activity == LawnMowerActivity.MOWING, "a poll from before the shape never decides it"
    mowing_shape = {**RESTING_IN_DOCK, "118": 0}
    assert _local_entity(mowing_shape, "idle", fresh).activity == LawnMowerActivity.MOWING, "an unambiguous shape keeps the local reading"
    assert _local_entity(DOCKED, "mowing", fresh).activity == LawnMowerActivity.DOCKED
    assert _local_entity(RESTING_IN_DOCK, "idle", fresh).extra_state_attributes["robot_status"] == "idle"


class _FakeCloud:
    """Stands in for EufyCloudClient. Each answer is a DP 107 value or an exception."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.calls = 0

    def get_all_dps(self) -> tuple[dict[str, Any], dict[str, Any]]:
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        settings = {"edge_mm": 150, "path_mm": 80, "travel_speed": "normal", "blade_speed": "normal", "pad_direction": 0}
        return {"1": True, "107": answer}, settings


def test_the_ambiguous_shape_refreshes_the_cloud_at_once_at_night_and_then_every_minute(tmp_path: Path) -> None:
    async def scenario(hass: HomeAssistant) -> None:
        hass.states.async_set("sun.sun", "below_horizon")
        cloud = _FakeCloud([DEFAULT_PAYLOAD, MOWING_PAYLOAD, RuntimeError("cloud down")])
        with patch("custom_components.eufy_robomow.coordinator.tinytuya.Device"):
            coordinator = EufyMowerCoordinator(
                hass,
                host="192.0.2.1",
                device_id="synthetic-device",
                local_key="not-a-real-local-key",
                cloud_client=cloud,
                operating_mode=OPERATING_MODE_CONTROL,
            )
        entity = EufyRobomowEntity(coordinator, cast(Any, _entry()))
        replies: list[dict[str, Any]] = []
        coordinator._device_call = lambda method, *args: {"dps": dict(replies.pop(0))}  # type: ignore[method-assign]
        clock = {"now": 1000.0}

        async def poll(dps: dict[str, Any], at: float) -> None:
            replies.append(dps)
            clock["now"] = at
            with patch("custom_components.eufy_robomow.coordinator.time.monotonic", side_effect=lambda: clock["now"]):
                await coordinator._async_update_data()

        await poll(DOCKED, 1000)
        assert cloud.calls == 0, "at night a docked mower needs no cloud poll"
        assert entity.activity == LawnMowerActivity.DOCKED

        await poll(RESTING_IN_DOCK, 1010)
        assert cloud.calls == 1, "the ambiguous shape asks the cloud at once, also at night"
        assert entity.activity == LawnMowerActivity.DOCKED
        assert entity.extra_state_attributes["robot_status"] == "idle"

        await poll(RESTING_IN_DOCK, 1030)
        assert cloud.calls == 1, "within a minute the fresh answer stands"
        assert entity.activity == LawnMowerActivity.DOCKED

        await poll(RESTING_IN_DOCK, 1071)
        assert cloud.calls == 2, "while the shape lasts the cloud is asked every minute"
        assert entity.activity == LawnMowerActivity.MOWING, "a later task that mows with DP 118 still at 100"

        await poll(DOCKED, 1081)
        assert coordinator.ambiguous_since is None
        assert cloud.calls == 2

        await poll(RESTING_IN_DOCK, 1091)
        assert cloud.calls == 3, "a new ambiguous shape asks again although the last poll was recent"
        assert entity.activity == LawnMowerActivity.MOWING, "a failed poll leaves the earlier reading"

        await poll(RESTING_IN_DOCK, 1160)
        assert cloud.calls == 3, "after a failure the backoff applies again"

    async def wrapper() -> None:
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        try:
            await scenario(hass)
        finally:
            await hass.async_stop()

    asyncio.run(wrapper())
