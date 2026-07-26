"""Tests for the confirmed E15 map decoder."""

from __future__ import annotations

import pytest

from custom_components.eufy_robomow.map import (
    MapDecodeError,
    Point,
    parse_map_snapshot,
)

from .map_fixtures import (
    clean_path_payload,
    integer,
    map_payload,
    message,
    point,
    varint,
)


def test_parse_map_snapshot_builds_confirmed_geometry() -> None:
    snapshot = parse_map_snapshot(
        map_payload(),
        clean_path_payload(),
        point(11, 12, x_field=2, y_field=3),
    )

    assert snapshot.map_id == 539
    assert snapshot.boundary == (
        Point(-20, 0),
        Point(100, 0),
        Point(100, 80),
        Point(0, 80),
    )
    assert len(snapshot.base_areas) == 1
    assert len(snapshot.no_go_areas) == 1
    assert len(snapshot.pathways) == 1
    assert tuple(map(len, snapshot.cleaned_paths)) == (2, 2, 1)
    assert snapshot.mower_position == Point(9, 10)


def test_parse_map_snapshot_falls_back_to_navigation_position() -> None:
    snapshot = parse_map_snapshot(
        map_payload(include_embedded_position=False),
        clean_path_payload(),
        point(11, 12, x_field=2, y_field=3),
    )

    assert snapshot.mower_position == Point(11, 12)


def test_parse_map_snapshot_rejects_missing_boundary() -> None:
    payload = message(2, message(1, integer(16, 1)))

    with pytest.raises(MapDecodeError, match="boundary"):
        parse_map_snapshot(payload, b"", b"")


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"\x00", "field number 0"),
        (b"\x08", "Truncated"),
        (b"\x12\x04abc", "exceeds payload"),
        (varint((1 << 3) | 5) + b"\x00\x00\x00\x00", "wire type 5"),
        (b"\x08" + (b"\x80" * 10), "exceeds 10 bytes"),
    ],
)
def test_parse_map_snapshot_rejects_malformed_protobuf(
    payload: bytes,
    error: str,
) -> None:
    with pytest.raises(MapDecodeError, match=error):
        parse_map_snapshot(payload, b"", b"")


def test_parse_map_snapshot_rejects_coordinate_outside_sint32() -> None:
    oversized_coordinate = integer(1, 0x1_0000_0000)
    boundary = b"".join(message(1, oversized_coordinate) for _ in range(3))
    record = integer(16, 1) + message(10, message(3, boundary))

    with pytest.raises(MapDecodeError, match="sint32"):
        parse_map_snapshot(message(2, message(1, record)), b"", b"")
