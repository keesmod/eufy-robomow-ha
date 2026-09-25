"""Physical command acknowledgements must come from post-write local samples."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_robomow.commands import MowerCommand, command_evidence
from custom_components.eufy_robomow.coordinator import EufyMowerCoordinator


@pytest.mark.parametrize(
    "action,dps,evidence",
    [
        ("start", {"1": True, "2": False, "118": 0}, "mowing_reported"),
        ("start", {"1": True, "2": False, "118": 50}, None),
        ("resume", {"1": True, "2": True, "118": 0}, None),
        ("start", {"1": True}, None),
        ("pause", {"1": True, "2": True}, "pause_reported"),
        ("pause", {"1": False, "2": True}, None),
        ("dock", {"1": False}, "task_inactive"),
        ("dock", {"1": True, "118": 30}, "returning_reported"),
    ],
)
def test_command_evidence_is_precise(action, dps, evidence):
    assert command_evidence(action, dps) == evidence


# DP 118 stays at 100 after a map save. On 2026-09-25 a start from the dock right
# after an arrival reported DP 1 true with DP 118 still at 100.
SAVED = {"1": False, "2": False, "118": 100}
RESTING = {"1": True, "2": False, "118": 100}


@pytest.mark.parametrize(
    "action,before,dps,evidence",
    [
        ("start", SAVED, RESTING, "task_started"),
        ("start", {"8": 90, "110": 40, "118": 0}, {"1": True, "2": False, "118": 0}, "mowing_reported"),
        ("start", RESTING, RESTING, None),
        ("start", SAVED, {"1": True, "2": True, "118": 100}, None),
        ("start", {}, RESTING, None),
        ("resume", {**RESTING, "2": True}, RESTING, "pause_cleared"),
        ("resume", RESTING, RESTING, None),
        ("resume", {**RESTING, "2": True}, {"1": False, "2": False}, None),
    ],
)
def test_the_changed_flag_confirms_start_and_resume_after_a_map_save(action, before, dps, evidence):
    assert command_evidence(action, dps, before) == evidence
    op = MowerCommand(action, 4, before=before)
    op.state = "pending"
    op.observe(5, dps)
    assert op.evidence == evidence


def test_cached_and_prewrite_values_cannot_confirm():
    op = MowerCommand("pause", 4)
    op.observe(5, {"1": True, "2": True})
    assert op.state == "sending"
    op.state = "pending"
    op.observe(4, {"1": True, "2": True})
    assert op.state == "pending"
    op.observe(5, {})
    assert op.state == "pending"
    op.observe(5, {"1": True, "2": True})
    assert op.state == "confirmed"


def coordinator():
    c = object.__new__(EufyMowerCoordinator)
    c.operating_mode = "control"
    c.local_generation = 1
    c.command = None
    c.local_dps = {"1": True, "2": False, "118": 0}
    c.async_update_listeners = Mock()
    c.hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value={}))
    c.async_request_refresh = AsyncMock()
    return c


def test_timeout_is_visible_and_never_resends():
    async def run():
        c = coordinator()
        with pytest.raises(HomeAssistantError, match="did not confirm"):
            await c.async_send_mower_command("start", timeout=0.01)
        assert c.command.state == "timeout"
        c.hass.async_add_executor_job.assert_awaited_once()

    asyncio.run(run())


def test_refresh_confirms_command():
    async def run():
        c = coordinator()

        async def refresh():
            c.command.observe(2, {"1": True, "2": True})

        c.async_request_refresh = refresh
        await c.async_send_mower_command("pause", timeout=0.1)
        assert c.command.evidence == "pause_reported"

    asyncio.run(run())


def test_observe_only_never_reaches_transport():
    async def run():
        c = coordinator()
        c.operating_mode = "observe_only"
        with pytest.raises(HomeAssistantError, match="observe-only"):
            await c.async_send_mower_command("start")
        c.hass.async_add_executor_job.assert_not_awaited()

    asyncio.run(run())


def test_pause_can_supersede_pending_start():
    async def run():
        c = coordinator()
        task = asyncio.create_task(c.async_send_mower_command("start", timeout=1))
        await asyncio.sleep(0)
        old = c.command

        async def refresh():
            c.command.observe(3, {"1": True, "2": True})

        c.async_request_refresh = refresh
        await c.async_send_mower_command("pause")
        with pytest.raises(HomeAssistantError, match="superseded"):
            await task
        assert old.state == "superseded"
        assert c.command.state == "confirmed"

    asyncio.run(run())


def test_pause_without_a_running_task_is_refused_before_any_write():
    async def run():
        c = coordinator()
        c.local_dps = {"1": False, "2": False, "118": 0}
        with pytest.raises(HomeAssistantError, match="runs no task"):
            await c.async_send_mower_command("pause")
        c.hass.async_add_executor_job.assert_not_awaited()
        assert c.command is None
        c.local_dps = {}
        c.async_request_refresh = AsyncMock()
        with pytest.raises(HomeAssistantError, match="did not confirm"):
            await c.async_send_mower_command("pause", timeout=0.01)
        c.hass.async_add_executor_job.assert_awaited_once()

    asyncio.run(run())


def test_a_start_from_the_dock_records_the_status_it_was_chosen_from():
    async def run():
        c = coordinator()
        c.local_dps = SAVED

        async def refresh():
            c.command.observe(2, RESTING)

        c.async_request_refresh = refresh
        await c.async_send_mower_command("start", timeout=0.1)
        assert c.command.state == "confirmed"
        assert c.command.evidence == "task_started"

    asyncio.run(run())


def test_the_cloud_confirms_only_a_pending_start_or_resume_with_the_mowing_payload():
    for action, status, state in [
        ("start", "mowing", "confirmed"),
        ("resume", "mowing", "confirmed"),
        ("start", None, "pending"),
        ("start", "idle", "pending"),
        ("pause", "paused", "pending"),
        ("dock", "returning", "pending"),
    ]:
        op = MowerCommand(action, 4)
        op.state = "pending"
        op.observe_cloud(status)
        assert op.state == state, (action, status)
    op = MowerCommand("start", 4)
    op.observe_cloud("mowing")
    assert op.state == "sending", "never before the write"


def test_a_confirmation_stands_when_the_refresh_outlasts_the_bound():
    async def run():
        c = coordinator()

        async def refresh():
            c.command.observe(2, {"1": True, "2": False, "118": 0})
            await asyncio.sleep(1)

        c.async_request_refresh = refresh
        await c.async_send_mower_command("start", timeout=0.05)
        assert c.command.state == "confirmed"
        assert c.command.evidence == "mowing_reported"

    asyncio.run(run())
