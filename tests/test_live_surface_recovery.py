"""Synthetic monitor and visible-form recovery coverage for 220-H."""
from __future__ import annotations

from samsara.live_surface.placement import Edge, Placement, Rect, Screen, placement_for_screens, place_form


def test_all_edges_and_free_float_clamp_the_complete_visible_form():
    screen = Screen("primary", Rect(0, 0, 320, 240), primary=True)
    for edge in (*tuple(Edge),):
        form = place_form(Placement(edge, .5, .7, .2), screen,
                          card_width=500, card_height=360)
        assert screen.work_area.contains(form.bounds), edge


def test_unplug_then_replug_defers_to_fallback_without_losing_record():
    records = {"gone": {"edge": "left", "t": .2}, "now": {"edge": "bottom", "t": .5}}
    now = Screen("now", Rect(-1000, 0, 1000, 700), primary=True)
    fallback = placement_for_screens(records, [now], preferred_monitor="gone")
    assert fallback.used_fallback and records["gone"]["edge"] == "left"
    restored = placement_for_screens(records, [now, Screen("gone", Rect(0, 0, 800, 600))], preferred_monitor="gone")
    assert restored.screen.id == "gone" and restored.placement.edge is Edge.LEFT


def test_mixed_dpi_negative_origin_is_not_global_scale_divided():
    screen = Screen("negative", Rect(-1920, -120, 1280, 900), primary=True)
    form = place_form(Placement(Edge.RIGHT, .75), screen, card_width=500, card_height=360)
    assert screen.work_area.contains(form.bounds)
    assert form.mark.center[0] < -640
