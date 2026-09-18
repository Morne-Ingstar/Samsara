"""Qt-free monitor placement and recovery math for the live surface."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Iterable, Mapping, Optional

MARK_SIZE_DIP = 44.0
SNAP_DISTANCE_DIP = 24.0
INSET_DIP = 8.0
ATTACHMENT_DIP = 4.0


class Edge(str, Enum):
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"
    FREE = "free"


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, other: "Rect") -> bool:
        return (other.x >= self.x and other.y >= self.y and other.right <= self.right
                and other.bottom <= self.bottom)


@dataclass(frozen=True)
class Screen:
    """Plain screen input. Coordinates are DIPs and may have negative origin."""

    id: str
    work_area: Rect
    primary: bool = False


@dataclass(frozen=True)
class Placement:
    edge: Edge = Edge.BOTTOM
    t: float = 0.5
    free_cx: float = 0.5
    free_cy: float = 0.5


@dataclass(frozen=True)
class PlacedForm:
    screen_id: str
    edge: Edge
    mark: Rect
    bounds: Rect


@dataclass(frozen=True)
class ResolvedPlacement:
    screen: Screen
    placement: Placement
    used_fallback: bool


def monitor_identity(name: str, manufacturer: str = "", model: str = "", serial: str = "") -> str:
    """Stable identity; a deterministic name fallback when EDID is absent."""
    clean_name = str(name or "screen")
    details = tuple(str(v).strip() for v in (manufacturer, model, serial))
    if any(details):
        return "|".join((clean_name, *details))
    return clean_name


def normalize_placement(raw: object, *, default: Placement = Placement()) -> Placement:
    """Parse persisted placement defensively, retaining only bounded values."""
    if isinstance(raw, Placement):
        return Placement(raw.edge, _unit(raw.t), _unit(raw.free_cx), _unit(raw.free_cy))
    if not isinstance(raw, Mapping):
        return default
    try:
        edge = Edge(raw.get("edge", default.edge))
    except (TypeError, ValueError):
        edge = default.edge
    return Placement(edge, _unit(raw.get("t", default.t)), _unit(raw.get("free_cx", default.free_cx)),
                     _unit(raw.get("free_cy", default.free_cy)))


def placement_record(placement: Placement) -> dict[str, float | str]:
    """The bounded persisted representation (no Qt screen object)."""
    item = normalize_placement(placement)
    return {"edge": item.edge.value, "t": item.t, "free_cx": item.free_cx, "free_cy": item.free_cy}


def snap_edge(center: tuple[float, float], work_area: Rect, *, previous: Optional[Edge] = None,
              snap_distance: float = SNAP_DISTANCE_DIP) -> Optional[Edge]:
    """Return the dock edge when a mark centre lies in a snap zone.

    Corner ties retain the prior edge. Without one, the smallest distance wins;
    exact ties prefer top/bottom over left/right, then top over bottom.
    """
    x, y = center
    distances = {
        Edge.TOP: abs(y - work_area.y), Edge.BOTTOM: abs(work_area.bottom - y),
        Edge.LEFT: abs(x - work_area.x), Edge.RIGHT: abs(work_area.right - x),
    }
    eligible = [edge for edge, distance in distances.items() if distance <= snap_distance]
    if not eligible:
        return None
    lowest = min(distances[edge] for edge in eligible)
    tied = [edge for edge in eligible if distances[edge] == lowest]
    if previous in tied:
        return previous
    for edge in (Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT):
        if edge in tied:
            return edge
    return None


def edge_mark_rect(placement: Placement, work_area: Rect, *, mark_size: float = MARK_SIZE_DIP,
                   inset: float = INSET_DIP) -> Rect:
    """Mark rectangle for an edge dock; ``t`` spans usable mark centres."""
    item = normalize_placement(placement)
    t = item.t
    if item.edge in (Edge.TOP, Edge.BOTTOM):
        x = _along(work_area.x, work_area.width, mark_size, inset, t)
        y = work_area.y + inset if item.edge is Edge.TOP else work_area.bottom - inset - mark_size
    elif item.edge in (Edge.LEFT, Edge.RIGHT):
        x = work_area.x + inset if item.edge is Edge.LEFT else work_area.right - inset - mark_size
        y = _along(work_area.y, work_area.height, mark_size, inset, t)
    else:
        x, y = _free_mark_origin(item, work_area, mark_size, inset)
    return Rect(x, y, mark_size, mark_size)


def place_form(placement: Placement, screen: Screen, *, card_width: float = 0.0,
               card_height: float = 0.0, mark_size: float = MARK_SIZE_DIP,
               inset: float = INSET_DIP, free_growth: Optional[Edge] = None) -> PlacedForm:
    """Place and clamp the complete visible surface, never only its mark.

    ``card_width``/``card_height`` exclude the mark. A zero card gives M;
    callers pass S/L/R card dimensions. Free placement stores mark centre and
    chooses a growth edge once per expansion (or accepts the caller's fixed
    choice for that expansion).
    """
    item = normalize_placement(placement)
    mark = edge_mark_rect(item, screen.work_area, mark_size=mark_size, inset=inset)
    edge = item.edge
    if edge is Edge.FREE:
        edge = free_growth if free_growth in (Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT) else free_growth_edge(
            mark.center, screen.work_area)
    if card_width <= 0 or card_height <= 0:
        bounds = _clamp(Rect(mark.x, mark.y, mark.width, mark.height), screen.work_area)
        return PlacedForm(screen.id, item.edge, bounds, bounds)
    if edge is Edge.TOP:
        bounds = Rect(mark.center[0] - card_width / 2, mark.y, card_width, mark_size + ATTACHMENT_DIP + card_height)
        mark_offset = (bounds.width / 2 - mark_size / 2, 0.0)
    elif edge is Edge.BOTTOM:
        bounds = Rect(mark.center[0] - card_width / 2, mark.y - card_height - ATTACHMENT_DIP,
                      card_width, card_height + ATTACHMENT_DIP + mark_size)
        mark_offset = (bounds.width / 2 - mark_size / 2, bounds.height - mark_size)
    elif edge is Edge.LEFT:
        bounds = Rect(mark.x, mark.center[1] - card_height / 2, mark_size + ATTACHMENT_DIP + card_width, card_height)
        mark_offset = (0.0, bounds.height / 2 - mark_size / 2)
    else:  # right
        bounds = Rect(mark.x - card_width - ATTACHMENT_DIP, mark.center[1] - card_height / 2,
                      card_width + ATTACHMENT_DIP + mark_size, card_height)
        mark_offset = (bounds.width - mark_size, bounds.height / 2 - mark_size / 2)
    bounds = _clamp(bounds, screen.work_area)
    placed_mark = Rect(bounds.x + mark_offset[0], bounds.y + mark_offset[1], mark_size, mark_size)
    return PlacedForm(screen.id, item.edge, placed_mark, bounds)


def free_growth_edge(center: tuple[float, float], work_area: Rect) -> Edge:
    """Side with the most available room; deterministic bottom/top/right/left ties."""
    x, y = center
    room = {
        Edge.TOP: y - work_area.y, Edge.BOTTOM: work_area.bottom - y,
        Edge.LEFT: x - work_area.x, Edge.RIGHT: work_area.right - x,
    }
    maximum = max(room.values())
    for edge in (Edge.BOTTOM, Edge.TOP, Edge.RIGHT, Edge.LEFT):
        if room[edge] == maximum:
            return edge
    return Edge.BOTTOM


def placement_for_screens(records: Mapping[str, object], screens: Iterable[Screen], *,
                          preferred_monitor: Optional[str], foreground_monitor: Optional[str] = None,
                          default: Placement = Placement()) -> ResolvedPlacement:
    """Resolve preferred screen, falling back foreground then primary.

    The caller owns ``records`` and never removes missing-screen entries; that
    preserves an unplugged monitor's placement for a later replug.
    """
    available = list(screens)
    if not available:
        raise ValueError("at least one screen is required")
    by_id = {screen.id: screen for screen in available}
    selected = by_id.get(preferred_monitor or "")
    fallback = selected is None
    if selected is None:
        selected = by_id.get(foreground_monitor or "")
    if selected is None:
        selected = next((screen for screen in available if screen.primary), available[0])
    return ResolvedPlacement(selected, normalize_placement(records.get(selected.id), default=default), fallback)


def normalized_t_for_mark(mark: Rect, edge: Edge, work_area: Rect, *, inset: float = INSET_DIP) -> float:
    """Persist a dock mark by normalized usable-edge position."""
    if edge in (Edge.TOP, Edge.BOTTOM):
        start, span, centre = work_area.x + inset, work_area.width - mark.width - 2 * inset, mark.x
    else:
        start, span, centre = work_area.y + inset, work_area.height - mark.height - 2 * inset, mark.y
    return 0.5 if span <= 0 else _unit((centre - start) / span)


def _unit(value: object) -> float:
    try:
        number = float(value)
        return min(1.0, max(0.0, number)) if isfinite(number) else 0.5
    except (TypeError, ValueError):
        return 0.5


def _along(start: float, extent: float, mark_size: float, inset: float, t: float) -> float:
    usable = max(0.0, extent - mark_size - 2 * inset)
    return start + inset + usable * t


def _free_mark_origin(placement: Placement, area: Rect, size: float, inset: float) -> tuple[float, float]:
    usable_width = max(0.0, area.width - size - 2 * inset)
    usable_height = max(0.0, area.height - size - 2 * inset)
    return area.x + inset + usable_width * placement.free_cx, area.y + inset + usable_height * placement.free_cy


def _clamp(rect: Rect, area: Rect) -> Rect:
    width, height = min(rect.width, area.width), min(rect.height, area.height)
    x = min(max(rect.x, area.x), area.right - width)
    y = min(max(rect.y, area.y), area.bottom - height)
    return Rect(x, y, width, height)
