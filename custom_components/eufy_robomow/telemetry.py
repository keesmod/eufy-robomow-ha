"""Interpret task flags in complete E15 local status responses."""

import base64
import binascii
from typing import Any

from .const import RETURNING_THRESHOLD

# DP 107 ``robot_status`` is the mower's mission status. The official app's product
# script for the T2880 (Anker eufy 6.1.00, T2880.js with SHA-256 0be33785e7c7…,
# read on the owner's Mac on 2026-09-25) decodes DP 107 as that message: field 1
# is the mission, 2 the sub-mission, 3 the state, 4 the power mode, 5 an error
# flag and 6 a saving-data flag, all varints, an absent field counting as zero.
# The owned E15 confirmed on hardware: missions 1, 2 and 17, sub-missions 1, 3,
# 5, 6 and 9, states 1 and 2, power mode 2 and the saving-data flag, each with
# the app's display at the time (library windows of 2026-09-16 to 2026-09-20
# and the window of 2026-09-25).
_MISSION_IDLE = 0
_MISSION_RECHARGE = 1
# The missions that mow: the whole lawn (2), mapping while mowing (4), a
# temporary task (5), remote-controlled mowing (7), the scheduled whole lawn (8),
# scheduled mapping while mowing (9), a selected zone (10), a scheduled zone
# (16), a drawn box (17, the app's Box), edge trimming (18) and scheduled edge
# trimming (22). Missions 2 and 17 are confirmed on the owned E15.
_MOWING_MISSIONS = frozenset({2, 4, 5, 7, 8, 9, 10, 16, 17, 18, 22})
_SUB_MISSION_IDLE = 0
_SUB_MISSION_SAVING_MAP = 5
_STATE_IDLE = 0
_STATE_RUNNING = 1
_STATE_PAUSED = 2
# Field 4: running, standby, or hibernate. Hibernate held in the cloud from 5 to
# 15 minutes after a rest in the dock ended until the next rest, while the app
# showed its idle controls.
_POWER_MODES = {0: "running", 1: "standby", 2: "hibernate"}
_ERROR_FIELD = 5
_MAX_ROBOT_STATUS_BYTES = 64


def task_ambiguous(dps: dict[str, Any]) -> bool:
    """DP 1 true, DP 2 false and DP 118 at 100, where mowing and resting look alike.

    DP 118 is map-save progress and stays at 100 after a map save, also while a
    later task mows. On 2026-09-24 the same shape also held for about fifteen
    minutes after each dock arrival and each evening while the mower rested in
    the dock, so a local status reply cannot tell the two apart. Only DP 107 can.
    """
    return status_shape(dps) == "ambiguous"


def status_shape(dps: dict[str, Any]) -> str:
    """Name the shape of one local status reply that the activity depends on.

    ``unknown`` without a task flag, ``no_task`` with DP 1 false, ``paused`` with
    DP 1 and DP 2 true. With DP 1 true and DP 2 false, DP 118 decides: between
    5 and 99 it is ``map_save``, at 100 ``ambiguous``, otherwise ``task``. DP 118
    is map-save progress: on the owned E15 it rises from 1 to 100 in about
    eighteen seconds at each dock arrival and after the app's Stop, while DP 1
    is true, and the drive home itself runs with DP 1 false.
    """
    active = task_active(dps)
    if active is None:
        return "unknown"
    if not active:
        return "no_task"
    if dps.get("2", False):
        return "paused"
    progress = dps.get("118")
    if progress == 100:
        return "ambiguous"
    if isinstance(progress, int) and RETURNING_THRESHOLD <= progress < 100:
        return "map_save"
    return "task"


def read_local_activity(dps: dict[str, Any], status: str | None) -> str | None:
    """The local backend's activity from one status reply and DP 107.

    ``status`` is DP 107 as :func:`robot_status` read it from a cloud poll taken
    since the status reply took its current shape, or None. A local reply never
    carries DP 107. Returns ``mowing``, ``paused``, ``returning``, ``docked`` or
    None while the task flag is unknown.

    DP 1 turns false as soon as a task ends or a stop is sent, and the mower
    then drives home with DP 1 false, so only the confirmed ``returning`` payload
    shows the drive. The map-saving payload marks the map save at the dock
    arrival and after the app's Stop, which reads as docked, as the ambiguous
    shape has since 0.13.1. Without a fresh payload each shape keeps its earlier
    local reading: DP 118 between 5 and 99 reads as returning, the other task
    shapes as mowing. Nothing is inferred from age or absence.
    """
    shape = status_shape(dps)
    if shape == "unknown":
        return None
    if shape == "no_task":
        return "returning" if status == "returning" else "docked"
    if shape == "paused":
        return "paused"
    if status == "returning":
        return "returning"
    if status == "map_saving":
        return "docked"
    if shape == "ambiguous":
        # Resting in the dock with the task flag set reads the default payload.
        if status == "idle":
            return "docked"
        if status == "paused":
            return "paused"
        return "mowing"
    if shape == "map_save":
        return "returning"
    return "mowing"


def mission_status(value: Any) -> dict[int, int] | None:
    """The varint fields of one DP 107 payload as it arrives in the cloud DPS.

    The default payload, zero bytes or a single zero byte, has no fields. None
    when the value is not base64, too long, or not a flat record of distinct
    varint fields.
    """
    if not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) > _MAX_ROBOT_STATUS_BYTES:
        return None
    if raw in (b"", b"\x00"):
        return {}
    fields: dict[int, int] = {}
    offset = 0
    while offset < len(raw):
        tag, offset = _varint(raw, offset)
        if tag is None:
            return None
        number, wire = tag >> 3, tag & 7
        if number == 0 or wire != 0 or number in fields:
            return None
        field_value, offset = _varint(raw, offset)
        if field_value is None:
            return None
        fields[number] = field_value
    return fields


def robot_status(value: Any) -> str | None:
    """Read one DP 107 ``robot_status`` payload as an activity.

    ``mowing`` and ``paused`` for a mowing mission that runs or pauses,
    ``returning`` for the recharge mission while it runs, ``map_saving`` while
    the map is saved without a mission, and ``idle`` without a mission,
    sub-mission or state and without the error flag, whatever the power mode.
    That covers the default payload, which held while the mower rested in the
    dock with DP 1 true, hibernation (field 4 = 2) and the saving-data flag
    after a map save. Anything else, such as the first frame of a start with
    no state yet, returns None and is never guessed.
    """
    fields = mission_status(value)
    if fields is None:
        return None
    mission = fields.get(1, _MISSION_IDLE)
    sub_mission = fields.get(2, _SUB_MISSION_IDLE)
    state = fields.get(3, _STATE_IDLE)
    if mission == _MISSION_IDLE:
        if sub_mission == _SUB_MISSION_SAVING_MAP and state == _STATE_RUNNING:
            return "map_saving"
        if sub_mission == _SUB_MISSION_IDLE and state == _STATE_IDLE and not fields.get(_ERROR_FIELD):
            return "idle"
        return None
    if mission == _MISSION_RECHARGE and state == _STATE_RUNNING:
        return "returning"
    if mission in _MOWING_MISSIONS:
        if state == _STATE_RUNNING:
            return "mowing"
        if state == _STATE_PAUSED:
            return "paused"
    return None


def robot_power_mode(value: Any) -> str | None:
    """Field 4 of one DP 107 payload: ``running``, ``standby`` or ``hibernate``."""
    fields = mission_status(value)
    if fields is None:
        return None
    return _POWER_MODES.get(fields.get(4, 0))


def _varint(raw: bytes, offset: int) -> tuple[int | None, int]:
    """One protobuf varint of at most ten bytes, or None when it is truncated or too long."""
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(raw):
            return None, offset
        byte = raw[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
    return None, offset


def task_active(dps: dict[str, Any]) -> bool | None:
    """An idle E15 omits DP1/DP2 until the first command after a connection reset.

    Only apply this observed idle convention to a complete status shape. Empty
    responses and partial notifications remain unknown and never end a session.
    Explicit flags always take precedence. This is not evidence of dock arrival.
    """
    value = dps.get("1")
    if type(value) is bool:
        return value
    if "1" not in dps and "2" not in dps and {"8", "110", "118"} <= dps.keys():
        if type(dps["8"]) is int and type(dps["110"]) is int and dps["118"] == 0:
            return False
    return None
