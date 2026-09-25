"""Tests for deterministic, script-free E15 map rendering."""

from __future__ import annotations

import re

from custom_components.eufy_robomow.map import MapSnapshot, Point
from custom_components.eufy_robomow.map_renderer import (
    MAP_CONTENT_TYPE,
    render_map_svg,
)


def _snapshot() -> MapSnapshot:
    return MapSnapshot(
        map_id=539,
        boundary=(
            Point(0, 0),
            Point(100, 0),
            Point(100, 100),
            Point(0, 100),
        ),
        base_areas=(
            (
                Point(40, 90),
                Point(70, 90),
                Point(70, 110),
                Point(40, 110),
            ),
        ),
        no_go_areas=(
            (
                Point(20, 20),
                Point(30, 20),
                Point(30, 30),
                Point(20, 30),
            ),
        ),
        pathways=(
            (Point(-20, 50), Point(10, 50)),
            (Point(40, 40), Point(60, 60)),
        ),
        cleaned_paths=((Point(10, 10), Point(20, 10)),),
        mower_position=Point(45, 50),
        tracking_position=Point(20, 10),
        forbidden_zones=(
            (
                Point(60, 15),
                Point(80, 20),
                Point(75, 40),
                Point(55, 35),
            ),
        ),
    )


def test_render_map_svg_is_deterministic_and_script_free() -> None:
    rendered = render_map_svg(_snapshot())

    assert rendered == render_map_svg(_snapshot())
    assert MAP_CONTENT_TYPE == "image/svg+xml"
    assert rendered.startswith(b'<svg xmlns="http://www.w3.org/2000/svg"')
    assert b"<title>Eufy mower map 539</title>" in rendered
    assert b"<script" not in rendered.lower()
    assert b'class="boundary"' in rendered
    assert b'class="base-area"' in rendered
    assert rendered.count(b'class="obstacle"') == 1
    assert rendered.count(b'class="forbidden-zone"') == 1
    assert rendered.count(b'class="pathway"') == 1
    assert b'class="mower-marker"' in rendered


def test_render_map_svg_draws_only_pathways_that_leave_the_lawn() -> None:
    snapshot = _snapshot()
    rendered = render_map_svg(snapshot).decode()

    # The snapshot keeps both pathways. The renderer draws the one that leaves
    # the lawn in full, including its end inside the lawn, and never the one
    # that stays inside.
    assert len(snapshot.pathways) == 2
    drawn = re.findall(r'<polyline class="pathway" points="([^"]+)"', rendered)
    assert len(drawn) == 1
    assert rendered.count('class="pathway-center"') == 1
    boundary = re.search(r'<polygon class="boundary" points="([^"]+)"', rendered)
    assert boundary is not None
    lawn_left = min(float(pair.split(",")[0]) for pair in boundary.group(1).split())
    drawn_x = [float(pair.split(",")[0]) for pair in drawn[0].split()]
    assert len(drawn_x) == len(snapshot.pathways[0])
    assert drawn_x[0] < lawn_left < drawn_x[-1]


def test_render_map_svg_draws_pathways_outside_the_lawn() -> None:
    snapshot = MapSnapshot(
        map_id=539,
        boundary=(Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)),
        base_areas=(),
        no_go_areas=(),
        pathways=(
            (Point(120, 10), Point(150, 10), Point(150, 40)),
            (Point(10, 10), Point(20, 20), Point(30, 10)),
            (Point(50, 50), Point(150, 50)),
        ),
        cleaned_paths=(),
        mower_position=None,
    )

    rendered = render_map_svg(snapshot).decode()
    drawn = re.findall(r'<polyline class="pathway" points="([^"]+)"', rendered)

    # Outside the lawn and across its edge are drawn, inside it is not.
    assert [len(points.split()) for points in drawn] == [3, 2]
    assert re.findall(r'<polyline class="pathway-center" points="([^"]+)"', rendered) == drawn


def test_render_map_svg_draws_forbidden_zones_as_red_dashed_zones() -> None:
    rendered = render_map_svg(_snapshot()).decode()

    style = re.search(r"\.forbidden-zone \{([^}]*)\}", rendered)
    assert style is not None
    assert "stroke: #ff3b30" in style.group(1)
    assert "stroke-dasharray" in style.group(1)
    obstacle_style = re.search(r"\.obstacle \{([^}]*)\}", rendered)
    assert obstacle_style is not None
    assert "#ff3b30" not in obstacle_style.group(1)
    # Above the lawn, the areas and the obstacles, below the pathways and markers.
    zone = rendered.index('<polygon class="forbidden-zone"')
    assert rendered.index('<polygon class="obstacle"') < zone
    assert zone < rendered.index('<polyline class="pathway"')
    assert zone < rendered.index('class="mower-marker"')


def test_render_map_svg_keeps_forbidden_zones_inside_canvas() -> None:
    snapshot = MapSnapshot(
        map_id=539,
        boundary=(Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)),
        base_areas=(),
        no_go_areas=(),
        pathways=(),
        cleaned_paths=(),
        mower_position=None,
        forbidden_zones=(
            (Point(90, 90), Point(300, 90), Point(300, 300), Point(90, 300)),
        ),
    )

    rendered = render_map_svg(snapshot).decode()
    points = re.search(r'class="forbidden-zone" points="([^"]+)"', rendered)

    assert points is not None
    coordinates = [tuple(map(float, pair.split(","))) for pair in points.group(1).split()]
    assert len(coordinates) == 4
    assert all(48 <= x <= 672 and 48 <= y <= 852 for x, y in coordinates)


def test_render_map_svg_hides_historical_cleaned_path_by_default() -> None:
    assert b'class="cleaned-path"' not in render_map_svg(_snapshot())


def test_render_map_svg_can_show_current_cleaned_path() -> None:
    rendered = render_map_svg(
        _snapshot(),
        include_cleaned_paths=True,
    )

    assert b'class="cleaned-area" clip-path="url(#mowing-boundary-clip)"' in rendered
    assert b'class="cleaned-path"' in rendered
    assert b'class="charging-station"' in rendered
    assert b"scale(.75)" in rendered
    assert b'class="mower-lightning"' not in rendered


def test_render_map_svg_keeps_offset_markers_inside_canvas() -> None:
    snapshot = MapSnapshot(
        map_id=539,
        boundary=(Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)),
        base_areas=(),
        no_go_areas=(),
        pathways=(),
        cleaned_paths=((Point(0, 0), Point(100, 100)),),
        mower_position=Point(1000, 1000),
        tracking_position=Point(0, 0),
    )

    rendered = render_map_svg(snapshot, include_cleaned_paths=True)
    positions = re.findall(
        rb'class="(?:charging-station|mower-marker)" '
        rb'transform="translate\(([\d.]+) ([\d.]+)\)',
        rendered,
    )

    assert len(positions) == 2
    assert all(20 <= float(x) <= 700 for x, _ in positions)
    assert all(28 <= float(y) <= 872 for _, y in positions)
