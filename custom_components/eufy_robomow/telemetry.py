"""Interpret task flags in complete E15 local status responses."""

import base64
import binascii
from typing import Any

# DP 107 ``robot_status`` wire definitions confirmed by the eufy-mega-client library
# in owner-operated windows on the owned E15 (library 0.15.0, 2026-09-16 and
# 2026-09-19): every listed field is a varint with exactly this value, an absent
# field counting as zero. Other fields, such as field 2, do not take part.
_ROBOT_STATUS_ACTIVITIES = (
    ({1: 2, 3: 1}, "mowing"),
    ({1: 2, 3: 2}, "paused"),
    ({1: 1, 3: 1}, "returning"),
)
_MAX_ROBOT_STATUS_BYTES = 64


def task_ambiguous(dps: dict[str, Any]) -> bool:
    """DP 1 true, DP 2 false and DP 118 at 100, where mowing and resting look alike.

    DP 118 is map-save progress and stays at 100 after a map save, also while a
    later task mows. On 2026-09-24 the same shape also held for about fifteen
    minutes after each dock arrival and each evening while the mower rested in
    the dock, so a local status reply cannot tell the two apart. Only DP 107 can.
    """
    return task_active(dps) is True and dps.get("2", False) is False and dps.get("118") == 100


def robot_status(value: Any) -> str | None:
    """Read one DP 107 ``robot_status`` payload as it arrives in the cloud DPS.

    Returns ``mowing``, ``paused`` or ``returning`` for the confirmed wire
    definitions and ``idle`` for the default payload, zero bytes or a single
    zero byte, as the library parses it. On 2026-09-24 the default payload held
    in the cloud while the task flag DP 1 was true and the app showed the mower
    charging or idle in the dock. Everything else, including the map-saving
    payload and field 6, returns None and is never guessed.
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
        return "idle"
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
    for match, activity in _ROBOT_STATUS_ACTIVITIES:
        if all(fields.get(number, 0) == expected for number, expected in match.items()):
            return activity
    return None


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
