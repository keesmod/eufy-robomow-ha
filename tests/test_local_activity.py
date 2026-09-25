"""The local backend's activity where the status reply alone cannot show it."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.eufy_robomow.commands import MowerCommand
from custom_components.eufy_robomow.const import (
    BACKEND_LOCAL,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    OPERATING_MODE_CONTROL,
)
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator
from custom_components.eufy_robomow.lawn_mower import EufyRobomowEntity
from custom_components.eufy_robomow.telemetry import (
    read_local_activity,
    robot_status,
    status_shape,
    task_ambiguous,
)

# The local status reply of 2026-09-24 09:43 UTC, the mower resting in the dock
# with its task flag set while the app showed it idle. Scalar data points only.
RESTING_IN_DOCK = {
    "1": True, "2": False, "8": 100, "26": 0, "47": True, "101": True, "109": 70,
    "110": 40, "114": 43, "118": 100, "125": 36292, "126": 978, "131": 90,
    "134": "Wifi", "139": 0, "146": 1, "170": 2, "181": 0,
}
DOCKED = {**RESTING_IN_DOCK, "1": False}
# Synthetic replies in the shapes the recorder showed around each dock arrival
# from 2026-09-14 to 2026-09-24: a task mows with DP 118 at 0, DP 1 turns false
# for the drive home, and at the arrival DP 1 is true again while DP 118 rises
# from 0 to 100 as the map is saved.
MOWING = {**RESTING_IN_DOCK, "118": 0}
DRIVING_HOME = {**RESTING_IN_DOCK, "1": False, "118": 0}
ARRIVING = {**RESTING_IN_DOCK, "118": 0}
SAVING_THE_MAP = {**RESTING_IN_DOCK, "118": 71}
PAUSED = {**RESTING_IN_DOCK, "2": True, "118": 0}
DEFAULT_PAYLOAD = "AA=="
T0 = datetime(2026, 9, 24, 9, 43, tzinfo=UTC)


def _b64(*values: int) -> str:
    return base64.b64encode(bytes(values)).decode()


MOWING_PAYLOAD = _b64(0x08, 0x02, 0x18, 0x01)
RETURNING_PAYLOAD = _b64(0x08, 0x01, 0x18, 0x01)
MAP_SAVING_PAYLOAD = _b64(0x10, 0x05, 0x18, 0x01)
FIELD_4_PAYLOAD = _b64(0x20, 0x02)


def test_robot_status_reads_the_confirmed_definitions_and_the_default_payload() -> None:
    assert robot_status(DEFAULT_PAYLOAD) == "idle"
    assert robot_status("") == "idle"
    assert robot_status(MOWING_PAYLOAD) == "mowing"
    assert robot_status(_b64(0x08, 0x02, 0x10, 0x09, 0x18, 0x01)) == "mowing", "field 2 takes no part"
    assert robot_status(_b64(0x08, 0x02, 0x18, 0x02)) == "paused"
    assert robot_status(RETURNING_PAYLOAD) == "returning"
    assert robot_status(_b64(0x08, 0x01, 0x10, 0x01, 0x18, 0x01)) == "returning"
    assert robot_status(MAP_SAVING_PAYLOAD) == "map_saving"


def test_robot_status_never_guesses() -> None:
    for value in (
        _b64(0x10, 0x05, 0x18, 0x02),  # not exactly the map-saving payload
        _b64(0x10, 0x05, 0x18, 0x01, 0x30, 0x01),  # map saving with another record
        _b64(0x30, 0x01),  # field 6
        FIELD_4_PAYLOAD,  # field 4, seen while docked idle on 2026-09-06 and 2026-09-24
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


def test_each_local_status_has_one_shape() -> None:
    assert status_shape({}) == "unknown"
    assert status_shape(DOCKED) == "no_task"
    assert status_shape(DRIVING_HOME) == "no_task"
    assert status_shape({"8": 90, "110": 40, "118": 0}) == "no_task", "the idle convention"
    assert status_shape(PAUSED) == "paused"
    assert status_shape({**PAUSED, "118": 100}) == "paused"
    assert status_shape(MOWING) == "task"
    assert status_shape({**MOWING, "118": 4}) == "task", "below the threshold"
    assert status_shape({"1": True}) == "task", "DP 118 omitted"
    assert status_shape(SAVING_THE_MAP) == "map_save"
    assert status_shape({**MOWING, "118": 99}) == "map_save"
    assert status_shape(RESTING_IN_DOCK) == "ambiguous"


def test_the_local_reading_keeps_each_shape_without_dp107_and_follows_it_with_one() -> None:
    cases = [
        # The drive home after a task runs with DP 1 false. Only returning shows it.
        (DRIVING_HOME, None, "docked"),
        (DRIVING_HOME, "returning", "returning"),
        (DRIVING_HOME, "mowing", "docked"),
        (DRIVING_HOME, "paused", "docked"),
        (DRIVING_HOME, "map_saving", "docked"),
        (DRIVING_HOME, "idle", "docked"),
        # A task shape: the first seconds of the map save at the arrival look alike.
        (MOWING, None, "mowing"),
        (MOWING, "mowing", "mowing"),
        (MOWING, "returning", "returning"),
        (MOWING, "map_saving", "docked"),
        (MOWING, "idle", "mowing"),
        (MOWING, "paused", "mowing"),
        # DP 118 between 5 and 99 is the map save, read as returning without DP 107.
        (SAVING_THE_MAP, None, "returning"),
        (SAVING_THE_MAP, "map_saving", "docked"),
        (SAVING_THE_MAP, "returning", "returning"),
        (SAVING_THE_MAP, "idle", "returning"),
        (SAVING_THE_MAP, "mowing", "returning"),
        # The ambiguous shape as in 0.13.1.
        (RESTING_IN_DOCK, None, "mowing"),
        (RESTING_IN_DOCK, "idle", "docked"),
        (RESTING_IN_DOCK, "map_saving", "docked"),
        (RESTING_IN_DOCK, "paused", "paused"),
        (RESTING_IN_DOCK, "returning", "returning"),
        (RESTING_IN_DOCK, "mowing", "mowing"),
        # DP 2 decides alone, and an unknown task flag stays unknown.
        (PAUSED, None, "paused"),
        (PAUSED, "returning", "paused"),
        ({}, "returning", None),
    ]
    for dps, status, expected in cases:
        assert read_local_activity(dps, status) == expected, (status_shape(dps), status)


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
    coordinator.local_shape = status_shape(dps)
    coordinator.shape_since = T0
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
    assert _local_entity(MOWING, "idle", fresh).activity == LawnMowerActivity.MOWING, "an unambiguous shape keeps the local reading"
    assert _local_entity(DOCKED, "mowing", fresh).activity == LawnMowerActivity.DOCKED
    assert _local_entity(RESTING_IN_DOCK, "idle", fresh).extra_state_attributes["robot_status"] == "idle"


def test_the_entity_shows_the_drive_home_and_the_map_save_from_a_fresh_payload() -> None:
    fresh = T0 + timedelta(seconds=2)
    assert _local_entity(DRIVING_HOME, "returning", fresh).activity == LawnMowerActivity.RETURNING
    assert _local_entity(DRIVING_HOME, "returning", T0 - timedelta(seconds=5)).activity == LawnMowerActivity.DOCKED, "a poll from before DP 1 turned false"
    assert _local_entity(DRIVING_HOME).activity == LawnMowerActivity.DOCKED
    assert _local_entity(SAVING_THE_MAP, "map_saving", fresh).activity == LawnMowerActivity.DOCKED
    assert _local_entity(SAVING_THE_MAP).activity == LawnMowerActivity.RETURNING, "the earlier reading without DP 107"
    assert _local_entity(ARRIVING, "map_saving", fresh).activity == LawnMowerActivity.DOCKED
    assert _local_entity({}, "returning", fresh).activity is None


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


Poll = Callable[[dict[str, Any], float], Awaitable[None]]


def _run(
    tmp_path: Path,
    answers: list[Any],
    scenario: Callable[[EufyMowerCoordinator, EufyRobomowEntity, _FakeCloud, Poll], Awaitable[None]],
    sun: str = "below_horizon",
) -> None:
    """Run one scenario against a local coordinator with a fake mower and cloud.

    ``poll(dps, at)`` answers the next local status query with ``dps`` while the
    monotonic clock reads ``at`` seconds. At night only the activity refreshes
    reach the cloud, so every cloud call in a scenario is one of them.
    """

    async def body(hass: HomeAssistant) -> None:
        hass.states.async_set("sun.sun", sun)
        cloud = _FakeCloud(answers)
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
        clock = {"now": 0.0}

        async def poll(dps: dict[str, Any], at: float) -> None:
            replies.append(dps)
            clock["now"] = at
            with patch("custom_components.eufy_robomow.coordinator.time.monotonic", side_effect=lambda: clock["now"]):
                await coordinator._async_update_data()

        await scenario(coordinator, entity, cloud, poll)

    async def wrapper() -> None:
        hass = HomeAssistant(str(tmp_path))
        frame.async_setup(hass)
        try:
            await body(hass)
        finally:
            await hass.async_stop()

    asyncio.run(wrapper())


def test_the_ambiguous_shape_refreshes_the_cloud_at_once_at_night_and_then_every_minute(tmp_path: Path) -> None:
    async def scenario(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
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
        assert coordinator.local_shape == "no_task"
        assert cloud.calls == 3, "the task that mowed has ended, so the drive home is asked for"
        assert entity.activity == LawnMowerActivity.DOCKED, "the default payload: no drive home"

        await poll(RESTING_IN_DOCK, 1091)
        assert cloud.calls == 4, "a new ambiguous shape asks again although the last poll was recent"
        assert entity.activity == LawnMowerActivity.MOWING, "a failed poll leaves the earlier reading"

        await poll(RESTING_IN_DOCK, 1160)
        assert cloud.calls == 4, "after a failure the backoff applies again"

    _run(tmp_path, [DEFAULT_PAYLOAD, MOWING_PAYLOAD, DEFAULT_PAYLOAD, RuntimeError("cloud down")], scenario)


def test_the_drive_home_after_a_task_and_the_map_save_at_the_arrival(tmp_path: Path) -> None:
    async def scenario(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(MOWING, 1000)
        assert cloud.calls == 0
        assert entity.activity == LawnMowerActivity.MOWING

        await poll(DRIVING_HOME, 1010)
        assert cloud.calls == 1, "DP 1 turned false after a task: DP 107 at once, also at night"
        assert entity.activity == LawnMowerActivity.RETURNING

        await poll(DRIVING_HOME, 1020)
        assert cloud.calls == 2, "at every local poll while the drive home is watched"
        assert entity.activity == LawnMowerActivity.RETURNING

        await poll(ARRIVING, 1030)
        assert cloud.calls == 3, "the dock arrival asks once"
        assert entity.activity == LawnMowerActivity.DOCKED, "the map save at the arrival"

        await poll(ARRIVING, 1040)
        assert cloud.calls == 3
        assert entity.activity == LawnMowerActivity.DOCKED

        await poll(SAVING_THE_MAP, 1050)
        assert cloud.calls == 4, "a map save asks once"
        assert entity.activity == LawnMowerActivity.DOCKED

        await poll(RESTING_IN_DOCK, 1060)
        assert cloud.calls == 5
        assert entity.activity == LawnMowerActivity.DOCKED, "the map-saving payload until DP 1 turns false"

        await poll(DOCKED, 1070)
        assert cloud.calls == 5, "after a map save no drive home is watched"
        assert entity.activity == LawnMowerActivity.DOCKED

        await poll(RESTING_IN_DOCK, 1370)
        assert cloud.calls == 6, "the rest in the dock five minutes later"
        assert entity.activity == LawnMowerActivity.DOCKED

        await poll(DOCKED, 1380)
        assert cloud.calls == 6, "after a rest in the dock no drive home is watched"

    _run(
        tmp_path,
        [RETURNING_PAYLOAD, RETURNING_PAYLOAD, MAP_SAVING_PAYLOAD, MAP_SAVING_PAYLOAD, MAP_SAVING_PAYLOAD, DEFAULT_PAYLOAD],
        scenario,
    )


def test_a_lagging_or_failing_cloud_keeps_the_last_reading_and_its_backoff(tmp_path: Path) -> None:
    async def scenario(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(PAUSED, 1000)
        assert entity.activity == LawnMowerActivity.PAUSED

        await poll(DRIVING_HOME, 1010)
        assert cloud.calls == 1
        assert entity.activity == LawnMowerActivity.DOCKED, "a cloud that still shows the task changes nothing"

        await poll(DRIVING_HOME, 1015)
        assert cloud.calls == 1, "a refresh between two polls does not ask again"

        await poll(DRIVING_HOME, 1020)
        assert cloud.calls == 2
        assert entity.activity == LawnMowerActivity.RETURNING

        await poll(DRIVING_HOME, 1030)
        assert cloud.calls == 3
        assert entity.activity == LawnMowerActivity.RETURNING, "a failed poll keeps the earlier reading"

        await poll(DRIVING_HOME, 1040)
        assert cloud.calls == 3, "after a failure the backoff applies"

        await poll(ARRIVING, 1050)
        assert cloud.calls == 3, "also for the arrival"
        assert entity.activity == LawnMowerActivity.MOWING, "the local reading of a new shape without DP 107"

    _run(tmp_path, [MOWING_PAYLOAD, RETURNING_PAYLOAD, RuntimeError("cloud down")], scenario)


def test_the_watch_ends_after_two_minutes_unless_the_mower_still_drives_home(tmp_path: Path) -> None:
    async def unread(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(MOWING, 1000)
        await poll(DRIVING_HOME, 1010)
        assert cloud.calls == 1
        assert entity.activity == LawnMowerActivity.DOCKED, "an unread payload keeps the local reading"
        await poll(DRIVING_HOME, 1130)
        assert cloud.calls == 2, "within two minutes of DP 1 turning false"
        await poll(DRIVING_HOME, 1140)
        await poll(DRIVING_HOME, 1400)
        assert cloud.calls == 2, "afterwards an unread payload ends the watch"
        assert entity.activity == LawnMowerActivity.DOCKED

    _run(tmp_path, [FIELD_4_PAYLOAD, FIELD_4_PAYLOAD], unread)

    async def returning(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(MOWING, 1000)
        await poll(DRIVING_HOME, 1010)
        await poll(DRIVING_HOME, 1130)
        assert cloud.calls == 2
        await poll(DRIVING_HOME, 1140)
        assert cloud.calls == 2, "after two minutes a returning reading is asked every minute"
        assert entity.activity == LawnMowerActivity.RETURNING
        await poll(DRIVING_HOME, 1190)
        assert cloud.calls == 3
        assert entity.activity == LawnMowerActivity.DOCKED, "the default payload ends the drive"
        await poll(DRIVING_HOME, 1400)
        assert cloud.calls == 3

    _run(tmp_path, [RETURNING_PAYLOAD, RETURNING_PAYLOAD, DEFAULT_PAYLOAD], returning)


def test_the_poll_that_confirms_a_dock_asks_for_the_drive_home(tmp_path: Path) -> None:
    async def scenario(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(MOWING, 1000)
        command = MowerCommand("dock", coordinator.local_generation)
        command.state, command.sent_monotonic = "pending", 1005.0
        coordinator.command = command

        await poll(MOWING, 1005.5)
        assert command.state == "pending"
        assert cloud.calls == 0, "a pending command keeps the cloud out"

        await poll(DRIVING_HOME, 1015)
        assert command.state == "confirmed" and command.evidence == "task_inactive"
        assert cloud.calls == 1, "the confirmation came first, then the cloud was asked"
        assert entity.activity == LawnMowerActivity.RETURNING

    _run(tmp_path, [RETURNING_PAYLOAD], scenario)


def test_a_start_while_resting_in_the_dock_is_confirmed_by_the_cloud(tmp_path: Path) -> None:
    async def scenario(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(RESTING_IN_DOCK, 1000)
        assert cloud.calls == 1
        assert entity.activity == LawnMowerActivity.DOCKED
        command = MowerCommand("start", coordinator.local_generation, before=dict(coordinator.local_dps))
        command.state, command.sent_monotonic = "pending", 1005.0
        coordinator.command = command

        await poll(RESTING_IN_DOCK, 1006)
        assert cloud.calls == 2, "the local status cannot show the start, DP 107 can"
        assert command.state == "pending", "the transitional first frame is not read"

        await poll(RESTING_IN_DOCK, 1010)
        assert cloud.calls == 2, "at most once per local poll"

        await poll(RESTING_IN_DOCK, 1016)
        assert cloud.calls == 3
        assert command.state == "confirmed" and command.evidence == "cloud_mowing_reported"
        assert entity.activity == LawnMowerActivity.MOWING

    _run(tmp_path, [DEFAULT_PAYLOAD, _b64(0x08, 0x02), MOWING_PAYLOAD], scenario)


def test_a_pending_start_the_local_status_can_show_never_asks_the_cloud(tmp_path: Path) -> None:
    async def scenario(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(DOCKED, 1000)
        command = MowerCommand("start", coordinator.local_generation, before=dict(coordinator.local_dps))
        command.state, command.sent_monotonic = "pending", 1005.0
        coordinator.command = command

        await poll(DOCKED, 1006)
        assert cloud.calls == 0
        assert command.state == "pending"

        await poll(RESTING_IN_DOCK, 1016)
        assert command.state == "confirmed" and command.evidence == "task_started"
        assert cloud.calls == 1, "confirmed locally, then the ambiguous shape asks as before"

    _run(tmp_path, [MOWING_PAYLOAD], scenario)


def test_the_drive_home_is_asked_for_in_daylight_beside_the_regular_poll(tmp_path: Path) -> None:
    async def scenario(coordinator: EufyMowerCoordinator, entity: EufyRobomowEntity, cloud: _FakeCloud, poll: Poll) -> None:
        await poll(MOWING, 1000)
        assert cloud.calls == 1, "the regular poll"
        assert entity.activity == LawnMowerActivity.MOWING

        await poll(DRIVING_HOME, 1010)
        assert cloud.calls == 2, "the drive home does not wait for the five-minute poll"
        assert entity.activity == LawnMowerActivity.RETURNING

    _run(tmp_path, [MOWING_PAYLOAD, RETURNING_PAYLOAD], scenario, sun="above_horizon")
