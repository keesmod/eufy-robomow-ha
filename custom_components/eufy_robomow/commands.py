"""Confirm commands using fresh local telemetry, never cached values.

The cloud's DP 107 confirms only a start or resume whose effect the local status
cannot show, see :meth:`MowerCommand.observe_cloud`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
from typing import Any

from homeassistant.util import dt as dt_util

from .const import DP_PAUSED, DP_PROGRESS, DP_TASK_ACTIVE, RETURNING_THRESHOLD
from .telemetry import task_active


def command_evidence(
    action: str, dps: dict[str, Any], before: dict[str, Any] | None = None
) -> str | None:
    """Describe exactly what a new device status proves.

    ``before`` is the local status the command was chosen from. DP 118 stays at
    100 after a map save, so a task started or resumed then never reports 0, as
    a start from the dock showed on 2026-09-25. There the flag the command
    changed is the evidence: DP 1 turning true for start and DP 2 turning false
    for resume. Without that change, for example a start while the mower rests
    in the dock with DP 1 already true, nothing is confirmed.
    """
    active, paused, progress = dps.get(DP_TASK_ACTIVE), dps.get(DP_PAUSED), dps.get(DP_PROGRESS)
    if action in ("start", "resume"):
        if active is True and paused is False and progress == 0:
            return "mowing_reported"
        if before is not None and active is True and paused is False:
            if action == "start" and task_active(before) is False:
                return "task_started"
            if action == "resume" and before.get(DP_PAUSED) is True:
                return "pause_cleared"
    elif action == "pause" and active is True and paused is True:
        return "pause_reported"
    elif action == "dock":
        if active is True and isinstance(progress, int) and RETURNING_THRESHOLD <= progress < 100:
            return "returning_reported"
        if active is False:
            # DP1=False proves an inactive task, not physical arrival at the dock.
            return "task_inactive"
    return None


@dataclass
class MowerCommand:
    """One bounded operation; a later safety command may supersede it."""

    action: str
    after_generation: int
    sent_monotonic: float = float("inf")
    state: str = "sending"
    evidence: str | None = None
    requested_at: str = field(default_factory=lambda: dt_util.utcnow().isoformat())
    finished_at: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # The local status the command was chosen from, see command_evidence.
    before: dict[str, Any] | None = field(default=None, repr=False)

    def finish(self, state: str, evidence: str | None = None) -> None:
        self.state, self.evidence = state, evidence
        self.finished_at = dt_util.utcnow().isoformat()
        self.event.set()

    def observe(self, generation: int, dps: dict[str, Any]) -> None:
        if self.state != "pending" or generation <= self.after_generation:
            return
        if evidence := command_evidence(self.action, dps, self.before):
            self.finish("confirmed", evidence)

    def observe_cloud(self, status: str | None) -> None:
        """Confirm a start or resume from DP 107 read after the write.

        The caller passes DP 107 from a cloud poll that began after the write, and
        only while the mower rests in the dock with its task flag set. Then DP 1
        is already true and DP 118 at 100, so the local status cannot show a
        start. On 2026-09-25 the cloud showed the default payload turn into the
        confirmed mowing payload within 4 seconds of such a start.
        """
        if self.state == "pending" and self.action in ("start", "resume") and status == "mowing":
            self.finish("confirmed", "cloud_mowing_reported")

    def as_dict(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in ("action", "state", "evidence", "requested_at", "finished_at")
        }
