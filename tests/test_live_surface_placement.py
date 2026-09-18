"""Qt-free placement geometry cases required by 220 package A."""
from samsara.live_surface.placement import (
    Edge, Placement, Rect, Screen, edge_mark_rect, monitor_identity, normalize_placement,
    place_form, placement_for_screens, snap_edge,
)


SCREEN = Screen("one", Rect(0, 0, 1000, 700), primary=True)


def test_all_four_edges_grow_inward_and_clamp_whole_form():
    for edge in (Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT):
        placed = place_form(Placement(edge, t=0.0), SCREEN, card_width=500, card_height=240)
        assert SCREEN.work_area.contains(placed.bounds)
        assert placed.bounds.contains(placed.mark)
        if edge is Edge.TOP: assert placed.mark.y == placed.bounds.y
        if edge is Edge.BOTTOM: assert placed.mark.bottom == placed.bounds.bottom
        if edge is Edge.LEFT: assert placed.mark.x == placed.bounds.x
        if edge is Edge.RIGHT: assert placed.mark.right == placed.bounds.right


def test_corner_ties_preserve_previous_else_top_bottom_win():
    assert snap_edge((0, 0), SCREEN.work_area, previous=Edge.LEFT) is Edge.LEFT
    assert snap_edge((0, 0), SCREEN.work_area) is Edge.TOP
    assert snap_edge((500, 350), SCREEN.work_area) is None


def test_free_float_uses_mark_center_on_negative_origin_mixed_dpi_geometry():
    screen = Screen("left", Rect(-1500, -100, 1500, 900))
    placement = Placement(Edge.FREE, free_cx=0.25, free_cy=0.75)
    mark = edge_mark_rect(placement, screen.work_area)
    placed = place_form(placement, screen, card_width=500, card_height=360)
    assert screen.work_area.contains(placed.bounds)
    assert mark.center[0] < -1000 and mark.center[1] > 500


def test_unplug_fallback_preserves_missing_monitor_record_for_replug():
    records = {"gone": {"edge": "left", "t": 0.2}, "primary": {"edge": "right", "t": 0.8}}
    primary = Screen("primary", Rect(0, 0, 800, 600), primary=True)
    resolved = placement_for_screens(records, [primary], preferred_monitor="gone", foreground_monitor=None)
    assert resolved.used_fallback and resolved.screen.id == "primary"
    assert records["gone"]["edge"] == "left"
    replugged = Screen("gone", Rect(-1200, 0, 1200, 800))
    restored = placement_for_screens(records, [primary, replugged], preferred_monitor="gone")
    assert restored.screen.id == "gone" and restored.placement.edge is Edge.LEFT


def test_malformed_records_and_identity_fallback_are_deterministic():
    malformed = normalize_placement({"edge": "bad", "t": "nan", "free_cx": -4, "free_cy": 9})
    assert malformed == Placement(Edge.BOTTOM, t=.5, free_cx=0.0, free_cy=1.0)
    assert monitor_identity("DISPLAY1") == "DISPLAY1"
    assert monitor_identity("DISPLAY1", "ACME", "Wide", "42") == "DISPLAY1|ACME|Wide|42"
