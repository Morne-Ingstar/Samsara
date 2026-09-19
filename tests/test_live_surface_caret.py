"""Bounded caret lookup and temporary placement avoidance (220-H)."""
from __future__ import annotations

import threading

from samsara.live_surface.caret import CaretBounds, CaretLocator
from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.placement import Edge, Placement, Rect, Screen, avoid_caret, place_form


def test_unavailable_provider_returns_none_without_queueing_workers():
    calls = []
    locator = CaretLocator(uia=lambda: calls.append(1) or None)
    try:
        assert locator.request() is None
        assert locator.request() is None
        # One outstanding request is allowed; no repeated worker queue.
        assert len(calls) <= 1
    finally:
        locator.close()


def test_stuck_provider_never_starts_an_unbounded_queue():
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set(); release.wait(.2); return None
    locator = CaretLocator(uia=slow)
    try:
        locator.request(); assert entered.wait(.1)
        for _ in range(20): assert locator.request() is None
        assert locator._future is not None
    finally:
        release.set(); locator.close()


def test_corner_avoidance_is_temporary_and_keeps_saved_edge():
    screen = Screen("left", Rect(-1500, -100, 1500, 900), primary=True)
    saved = Placement(Edge.TOP, .0)
    placed = place_form(saved, screen, card_width=500, card_height=360)
    avoided = avoid_caret(placed, Rect(-1490, -90, 20, 24), screen,
                          card_width=500, card_height=360)
    assert screen.work_area.contains(avoided.bounds)
    assert saved.edge is Edge.TOP  # avoidance never mutates preference


def test_cached_caret_is_reused_for_half_a_second():
    now = [0.0]; calls = []
    locator = CaretLocator(uia=lambda: calls.append(1) or CaretBounds(Rect(1, 2, 3, 4)),
                          clock=lambda: now[0])
    try:
        assert locator.request() is None
        # Finish the tiny worker then read its cache.
        locator._future.result(.2)
        assert locator.request().rect == Rect(1, 2, 3, 4)
        now[0] = .4
        assert locator.request().rect == Rect(1, 2, 3, 4)
        assert len(calls) == 1
    finally:
        locator.close()


def test_controller_applies_injected_caret_avoidance_without_saving_it():
    class App:
        config = {"ui": {"live_surface": {"placement": {
            "preferred_monitor": "screen", "monitors": {"screen": {"edge": "top", "t": 0}},
        }}}}
    class Widget:
        def height(self): return 360
        def apply_placement(self, placed): self.placed = placed
    class Locator:
        def request(self): return CaretBounds(Rect(8, 8, 30, 24))

    controller = LiveSurfaceController(App(), caret_locator=Locator())
    controller.widget = Widget()
    controller._screens = lambda: [Screen("screen", Rect(0, 0, 800, 600), primary=True)]
    controller._place_widget()
    assert controller.widget.placed.edge in (Edge.TOP, Edge.BOTTOM)
    # The persisted preference remains top even if temporary avoidance moved it.
    assert App.config["ui"]["live_surface"]["placement"]["monitors"]["screen"]["edge"] == "top"
