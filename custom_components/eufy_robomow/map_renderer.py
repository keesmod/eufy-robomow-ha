"""Deterministic, script-free SVG rendering for Eufy E15 maps."""

from __future__ import annotations

from dataclasses import dataclass

from .map import MapSnapshot, Point

MAP_CONTENT_TYPE = "image/svg+xml"
_WIDTH = 720
_HEIGHT = 900
_MARGIN = 48

# The mower coordinates describe the navigation anchor, not the icon centre.
_MOWER_ICON_OFFSET_X = 20
_MOWER_ICON_OFFSET_Y = 68


@dataclass(frozen=True, slots=True)
class _Projection:
    minimum_x: int
    minimum_y: int
    scale: float
    offset_x: float
    offset_y: float

    def point(self, value: Point) -> tuple[float, float]:
        return (
            self.offset_x + ((value.x - self.minimum_x) * self.scale),
            self.offset_y + ((value.y - self.minimum_y) * self.scale),
        )


def render_map_svg(
    snapshot: MapSnapshot,
    *,
    include_cleaned_paths: bool = False,
) -> bytes:
    """Render a normalized map snapshot as an iOS-like SVG."""
    pathways = tuple(
        pathway
        for pathway in snapshot.pathways
        if any(not _point_in_polygon(point, snapshot.boundary) for point in pathway)
    )
    cleaned_paths = snapshot.cleaned_paths if include_cleaned_paths else ()
    projection = _projection(
        _visible_points(
            snapshot,
            pathways=pathways,
            cleaned_paths=cleaned_paths,
        )
    )
    elements = [
        f'<polygon class="boundary" points="{_render_points(snapshot.boundary, projection)}" />'
    ]

    elements.extend(
        f'<polygon class="base-area" points="{_render_points(area, projection)}" />'
        for area in snapshot.base_areas
    )
    elements.extend(
        f'<polygon class="no-go-area" points="{_render_points(area, projection)}" />'
        for area in snapshot.no_go_areas
    )
    for pathway in pathways:
        rendered = _render_points(pathway, projection)
        elements.append(f'<polyline class="pathway" points="{rendered}" />')
        elements.append(f'<polyline class="pathway-center" points="{rendered}" />')
    elements.extend(
        f'<polyline class="cleaned-path" points="{_render_points(path, projection)}" />'
        for path in cleaned_paths
    )

    if snapshot.mower_position is not None:
        mower_x, mower_y = projection.point(snapshot.mower_position)
        elements.append(
            _render_mower(
                mower_x + _MOWER_ICON_OFFSET_X,
                mower_y + _MOWER_ICON_OFFSET_Y,
            )
        )

    body = "\n    ".join(elements)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}">
  <title>Eufy mower map {snapshot.map_id}</title>
  <rect class="background" width="{_WIDTH}" height="{_HEIGHT}" />
  <style>
    .background {{ fill: #020505; }}
    .boundary {{ fill: #153f16; stroke: #49a6ff; stroke-width: 5; stroke-linejoin: round; }}
    .base-area {{ fill: #347f91; fill-opacity: .9; stroke: #55aaff; stroke-width: 4; stroke-dasharray: 12 8; stroke-linejoin: round; }}
    .no-go-area {{ fill: #050607; stroke: #626a74; stroke-width: 2; stroke-linejoin: round; }}
    .pathway {{ fill: none; stroke: #ffbd00; stroke-width: 10; stroke-linecap: round; stroke-linejoin: round; }}
    .pathway-center {{ fill: none; stroke: #fff7cf; stroke-width: 1.5; stroke-dasharray: 8 8; stroke-linecap: round; }}
    .cleaned-path {{ fill: none; stroke: #d2f85b; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }}
    .mower-shadow {{ fill: #000; opacity: .6; }}
    .mower-wheel {{ fill: #25292d; stroke: #666d73; stroke-width: 1; }}
    .mower-body {{ fill: #4a4f54; stroke: #d6d9dc; stroke-width: 1.5; }}
    .mower-panel {{ fill: #24282b; stroke: #787f84; stroke-width: 1; }}
    .mower-accent {{ fill: #e34c56; }}
    .mower-lightning {{ fill: #39f27d; }}
  </style>
  <g>
    {body}
  </g>
</svg>
"""
    return svg.encode()


def _render_points(
    points: tuple[Point, ...],
    projection: _Projection,
) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in map(projection.point, points))


def _render_mower(x: float, y: float) -> str:
    return f"""<g class="mower-marker" transform="translate({x:.2f} {y:.2f})">
      <ellipse class="mower-shadow" cx="0" cy="3" rx="17" ry="25" />
      <rect class="mower-wheel" x="-17" y="-14" width="5" height="29" rx="2" />
      <rect class="mower-wheel" x="12" y="-14" width="5" height="29" rx="2" />
      <path class="mower-body" d="M -11 -20 Q 0 -25 11 -20 L 13 15 Q 0 23 -13 15 Z" />
      <rect class="mower-panel" x="-7" y="-14" width="14" height="22" rx="4" />
      <rect class="mower-accent" x="-2" y="-19" width="4" height="8" rx="2" />
      <path class="mower-lightning" d="M 2 7 L -5 16 L 0 16 L -3 23 L 7 12 L 2 12 Z" />
    </g>"""


def _point_in_polygon(point: Point, polygon: tuple[Point, ...]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        crosses_y = (current.y > point.y) != (previous.y > point.y)
        if crosses_y:
            crossing_x = (
                ((previous.x - current.x) * (point.y - current.y))
                / (previous.y - current.y)
            ) + current.x
            if point.x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _visible_points(
    snapshot: MapSnapshot,
    *,
    pathways: tuple[tuple[Point, ...], ...],
    cleaned_paths: tuple[tuple[Point, ...], ...],
) -> tuple[Point, ...]:
    geometry = (
        snapshot.boundary,
        *snapshot.base_areas,
        *snapshot.no_go_areas,
        *pathways,
        *cleaned_paths,
    )
    points = tuple(point for item in geometry for point in item)
    if snapshot.mower_position is not None:
        points += (snapshot.mower_position,)
    return points


def _projection(points: tuple[Point, ...]) -> _Projection:
    minimum_x = min(point.x for point in points)
    maximum_x = max(point.x for point in points)
    minimum_y = min(point.y for point in points)
    maximum_y = max(point.y for point in points)
    width = max(maximum_x - minimum_x, 1)
    height = max(maximum_y - minimum_y, 1)
    available_width = _WIDTH - (2 * _MARGIN)
    available_height = _HEIGHT - (2 * _MARGIN)
    scale = min(available_width / width, available_height / height)
    rendered_width = width * scale
    rendered_height = height * scale
    return _Projection(
        minimum_x=minimum_x,
        minimum_y=minimum_y,
        scale=scale,
        offset_x=_MARGIN + ((available_width - rendered_width) / 2),
        offset_y=_MARGIN + ((available_height - rendered_height) / 2),
    )
