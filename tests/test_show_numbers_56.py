"""Queue 56 -- "show numbers" must either label something or say why not.

  * zero enumerated elements -> spoken + chip + FAILED dispatch, no empty overlay
  * geometry: pills land inside the target window on a mocked multi-monitor,
    mixed-DPI layout (physical UIA pixels converted per monitor)
  * the overlay window is created on the UI thread, never the dispatch thread
  * every invocation logs the target, the element count and the geometry
  * apps that expose no controls (Electron/Chromium chrome only, custom-drawn
    UIs) are named as such

Never imports dictation, never shows a window (the overlay class is faked
wherever show() would run; the one real widget is painted off-screen).
"""
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import plugins.commands.show_numbers as sn  # noqa: E402
from samsara.command_registry import DispatchState  # noqa: E402
from samsara.ui import numbers_overlay_qt as nov  # noqa: E402


# ---------------------------------------------------------------------------
# Mocked monitor layout: 4K@150% primary, 1080p@100% right of it, 4K@150%
# portrait-ish monitor above-right at negative y. Qt keeps native origins and
# scales each screen's size (see _map_physical_to_qt).
# ---------------------------------------------------------------------------

PRIMARY = ((0, 0, 3840, 2160), (0, 0, 2560, 1440), 1.5)
SECOND = ((3840, 0, 5760, 1080), (3840, 0, 5760, 1080), 1.0)
THIRD = ((5760, -1000, 9600, 1160), (5760, -1000, 8320, 440), 1.5)
MAPPINGS = [PRIMARY, SECOND, THIRD]


class _FakeScreen:
    def __init__(self, name, qt_rect, dpr):
        self._name, self._rect, self._dpr = name, qt_rect, dpr

    def name(self):
        return self._name

    def devicePixelRatio(self):
        return self._dpr

    def geometry(self):
        l, t, r, b = self._rect
        return SimpleNamespace(x=lambda: l, y=lambda: t, width=lambda: r - l, height=lambda: b - t)


MAPPINGS_WITH_SCREENS = [
    (p, q, d, _FakeScreen(n, q, d)) for (p, q, d), n in zip(MAPPINGS, ("PRIMARY", "SECOND", "THIRD"))
]


def _el(prect, name="b"):
    return {"control": SimpleNamespace(name=name), "prect": prect, "rect": prect,
            "name": name, "type": "ButtonControl"}


def _inside(rect, bounds):
    x, y, w, h = rect
    return bounds[0] <= x and bounds[1] <= y and x + w <= bounds[2] and y + h <= bounds[3]


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

class TestPlanGeometry:
    def test_window_on_150_percent_primary_converts_and_stays_inside(self):
        window = (300, 300, 2100, 1500)                       # physical
        elements = [
            _el((300, 300, 360, 345)),      # top-left corner of the window: pill must clamp inside
            _el((3000, 1500, 3090, 1545)),  # outside window (x) but on primary
            _el((1200, 900, 1290, 945)),    # middle
            _el((2040, 1450, 2098, 1498)),  # bottom-right corner
            _el((4000, 500, 4060, 540)),    # physically on the second monitor
        ]
        plan = sn.plan_overlay(elements, window, MAPPINGS)

        assert plan["screen_index"] == 0
        assert plan["window_rect"] == (200, 200, 1400, 1000)       # 150% conversion
        assert plan["bounds"] == (200, 200, 1400, 1000)
        assert len(plan["kept"]) == 3
        assert plan["dropped_other_screen"] == 1 and plan["dropped_outside_window"] == 1
        assert plan["kept"][1]["rect"] == (800, 600, 860, 630)      # logical, not physical
        for sx, sy, pw, ph, _t in plan["labels"]:
            assert _inside(nov.pill_rect(sx, sy, pw, ph, plan["bounds"]), plan["bounds"])
        assert plan["clamped"] >= 1

    def test_window_on_100_percent_secondary_keeps_native_origin(self):
        window = (4000, 100, 5500, 1000)
        elements = [_el((4010, 110, 4100, 150)), _el((5400, 900, 5490, 990))]
        plan = sn.plan_overlay(elements, window, MAPPINGS)
        assert plan["screen_index"] == 1
        assert plan["window_rect"] == window
        for sx, sy, pw, ph, _t in plan["labels"]:
            pill = nov.pill_rect(sx, sy, pw, ph, plan["bounds"])
            assert _inside(pill, plan["bounds"]) and _inside(pill, plan["window_rect"])

    def test_maximised_window_border_is_bounded_by_its_screen(self):
        window = (5752, -1008, 9608, 1168)          # maximised on THIRD: -8 px border
        plan = sn.plan_overlay([_el((5760, -1000, 5820, -960))], window, MAPPINGS)
        assert plan["screen_index"] == 2
        assert plan["bounds"] == (5760, -1000, 8320, 440)
        (sx, sy, pw, ph, _t), = plan["labels"]
        assert _inside(nov.pill_rect(sx, sy, pw, ph, plan["bounds"]), (5760, -1000, 8320, 440))

    def test_window_straddling_monitors_uses_the_screen_holding_its_centre(self):
        window = (3500, 100, 5000, 900)             # centre x=4250 -> SECOND
        plan = sn.plan_overlay([_el((3600, 200, 3660, 240)), _el((4100, 200, 4160, 240))], window, MAPPINGS)
        assert plan["screen_index"] == 1
        assert len(plan["kept"]) == 1 and plan["dropped_other_screen"] == 1

    def test_identity_when_monitors_cannot_be_paired(self):
        plan = sn.plan_overlay([_el((10, 10, 60, 40))], (0, 0, 800, 600), [])
        assert plan["kept"][0]["rect"] == (10, 10, 60, 40)
        assert plan["bounds"] == (0, 0, 800, 600)

    def test_pill_rect_clamps_all_four_edges(self):
        bounds = (100, 100, 300, 300)
        assert nov.pill_rect(100, 100, 28, 22, bounds) == (100, 100, 28, 22)
        assert nov.pill_rect(400, 400, 28, 22, bounds) == (272, 278, 28, 22)
        assert nov.pill_rect(200, 200, 28, 22, bounds) == (200 - 28 - 4, 200 - 22 - 4, 28, 22)


def test_monitor_count_mismatch_is_logged_not_silent(monkeypatch, caplog):
    monkeypatch.setattr(nov, "_win32_monitor_rects", lambda: [(0, 0, 10, 10), (10, 0, 20, 10)])
    monkeypatch.setattr(nov.QApplication, "screens", staticmethod(lambda: [_FakeScreen("A", (0, 0, 10, 10), 1.0)]))
    monkeypatch.setattr(nov, "_last_mapping_warning", None)
    with caplog.at_level(logging.WARNING):
        assert nov.current_monitor_mappings() == []
    assert any("cannot map" in r.getMessage() for r in caplog.records)


def test_overlay_paints_pills_inside_bounds_offscreen(qapp):
    """A real NumbersOverlayWindow, never shown: grab() renders it off-screen."""
    screen = qapp.primaryScreen()
    g = screen.geometry()
    bounds = (g.x() + 200, g.y() + 200, g.x() + 600, g.y() + 500)
    labels = [[g.x() + 205, g.y() + 205, 28, 22, "1"],        # clamps to the bounds' top-left
              [g.x() + 400, g.y() + 400, 28, 22, "2"]]
    win = nov.NumbersOverlayWindow(labels, screen)
    try:
        win.set_bounds(bounds)
        img = win.grab().toImage()
        assert not win.isVisible()
        for sx, sy, pw, ph, _t in labels:
            x, y, w, h = nov.pill_rect(sx, sy, pw, ph, win._bounds)
            assert _inside((x, y, w, h), bounds)
            assert img.pixelColor(int(x - g.x() + w // 2), int(y - g.y() + 2)).alpha() > 100
        assert img.pixelColor(int(bounds[0] - g.x() - 30), int(bounds[1] - g.y() - 30)).alpha() == 0
    finally:
        win.deleteLater()


# ---------------------------------------------------------------------------
# handler: honest outcome, UI thread, trace logs
# ---------------------------------------------------------------------------

class _FakeRuntime:
    """qt_runtime double: posted callbacks are queued, then run on a thread
    named like the real UI thread."""

    def __init__(self):
        self.posted = []

    def ensure_started(self):
        pass

    def post(self, cb):
        self.posted.append(cb)

    def run_on_ui_thread(self):
        errors = []

        def _run():
            try:
                for cb in list(self.posted):
                    cb()
            except BaseException as exc:          # surface in the test thread
                errors.append(exc)

        t = threading.Thread(target=_run, name="samsara-qt")
        t.start()
        t.join(10)
        if errors:
            raise errors[0]


@pytest.fixture
def harness(monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(sn, "qt_runtime", runtime)
    monkeypatch.setattr(sn, "_is_brave_foreground", lambda hwnd: False)
    monkeypatch.setattr(sn, "QTimer", MagicMock())
    monkeypatch.setattr(sn, "_overlay_window", None)
    monkeypatch.setattr(sn, "_overlay_screen_name", "")
    monkeypatch.setattr(sn, "_enum_cache", None)
    monkeypatch.setattr(sn, "current_monitor_mappings", lambda: MAPPINGS_WITH_SCREENS)

    timers = []

    def _timer(name, seconds, fn, daemon=True):
        timers.append(name)
        if name == "show_numbers.report_chip":
            fn()
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(sn.thread_registry, "timer", _timer)

    constructed = []

    class _FakeWindow:
        def __init__(self, labels, screen):
            constructed.append(threading.current_thread().name)
            self.labels, self.screen, self.bounds, self.shown = labels, screen, None, False

        def set_bounds(self, b):
            self.bounds = b

        def update_labels(self, labels, caption="", bounds=None):
            self.labels, self.bounds = labels, bounds

        def show(self):
            self.shown = True

        def raise_(self):
            pass

        def hide(self):
            self.shown = False

        def isVisible(self):
            return self.shown

        def isHidden(self):
            return not self.shown

        def winId(self):
            return 0

    monkeypatch.setattr(sn, "NumbersOverlayWindow", _FakeWindow)
    monkeypatch.setattr(sn, "_ensure_dpi_thread_context", lambda: None)

    fg = {"hwnd": 0x1234, "exe": "explorer.exe", "cls": "CabinetWClass", "kind": "native",
          "rect": (4000, 100, 5500, 1000), "topmost": False}
    monkeypatch.setattr(sn, "_foreground_info", lambda hwnd: dict(fg))

    spoken, chips = [], []
    app = SimpleNamespace(hints=SimpleNamespace(increment=lambda *a: None),
                          _show_outcome_chip=lambda label, kind: chips.append((label, kind)))
    monkeypatch.setattr(sn, "_speak", lambda app_, text: spoken.append(text))

    def enumerate_as(elements, nodes):
        def _enum(*a, **k):
            sn._last_enum_stats = {"source": "uia", "count": len(elements), "nodes": nodes,
                                   "capped": False, "ms": 1}
            return list(elements)
        monkeypatch.setattr(sn, "_cached_enumerate", _enum)
        monkeypatch.setattr(sn, "_enumerate_foreground_clickables", _enum)

    with sn._state_lock:
        sn._elements.clear()
    yield SimpleNamespace(runtime=runtime, app=app, spoken=spoken, chips=chips, fg=fg,
                          constructed=constructed, timers=timers, enumerate_as=enumerate_as)
    with sn._state_lock:
        sn._elements.clear()


def test_zero_elements_is_reported_not_an_empty_overlay(harness, caplog):
    harness.enumerate_as([], nodes=163)
    with caplog.at_level(logging.INFO):
        result = sn.handle_show_numbers(harness.app, "")
    assert result is DispatchState.FAILED
    assert harness.runtime.posted == [], "no overlay may be posted for nothing"
    assert harness.spoken == [sn.NOTHING_SPOKEN]
    assert harness.chips and harness.chips[-1][1] == "error" and "nothing clickable" in harness.chips[-1][0]
    assert any("nothing to label" in r.getMessage() for r in caplog.records)
    assert not sn.is_overlay_active()


def test_zero_elements_in_an_electron_app_names_the_cause(harness):
    harness.fg.update(exe="obsidian.exe", cls="Chrome_WidgetWin_1", kind="chromium")
    harness.enumerate_as([], nodes=44)
    assert sn.handle_show_numbers(harness.app, "") is DispatchState.FAILED
    assert harness.spoken == [sn.OPAQUE_SPOKEN]
    assert "hides its controls" in harness.chips[-1][0]


def test_all_spoken_messages_survive_the_command_mode_char_limit():
    assert len(sn.NOTHING_SPOKEN) <= 50 and len(sn.OPAQUE_SPOKEN) <= 50
    assert len(f"Only {sn.OPAQUE_MAX_ELEMENTS} found. This app hides its controls.") <= 50


def test_overlay_is_created_on_the_ui_thread_and_logged(harness, caplog):
    elements = [_el((4010, 110, 4100, 150)), _el((4300, 500, 4380, 540)), _el((100, 100, 160, 140))]
    harness.enumerate_as(elements, nodes=163)
    with caplog.at_level(logging.INFO):
        dispatch_thread = threading.Thread(
            target=lambda: harness.__dict__.setdefault("result", sn.handle_show_numbers(harness.app, "")),
            name="command-dispatch")
        dispatch_thread.start()
        dispatch_thread.join(10)
        assert harness.result is True
        assert harness.constructed == [], "the window must not be built on the dispatch thread"
        assert len(harness.runtime.posted) == 1
        assert not sn.is_overlay_active()          # numbering is decided on the UI thread
        harness.runtime.run_on_ui_thread()

    assert harness.constructed == ["samsara-qt"]
    window = sn._overlay_window
    assert window.shown and window.screen.name() == "SECOND"
    assert len(window.labels) == 2                 # the element on PRIMARY is dropped
    for sx, sy, pw, ph, _t in window.labels:
        assert _inside(nov.pill_rect(sx, sy, pw, ph, window.bounds), (4000, 100, 5500, 1000))
    with sn._state_lock:
        assert len(sn._elements) == 2

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("[SHOW_NUMBERS] invoked: foreground exe=explorer.exe") for m in messages)
    assert any("[SHOW_NUMBERS] enumerated 3 clickable elements" in m for m in messages)
    plan_line = next(m for m in messages if m.startswith("[SHOW_NUMBERS] plan "))
    assert "screen=SECOND" in plan_line and "kept=2" in plan_line and "bounds=(4000, 100, 5500, 1000)" in plan_line
    assert any(m.startswith("[SHOW_NUMBERS] overlay window hwnd=") and "labels=2" in m for m in messages)
    assert harness.spoken == [] and harness.chips == []


def test_every_element_on_another_screen_reports_instead_of_drawing(harness):
    harness.enumerate_as([_el((100, 100, 160, 140))], nodes=163)   # on PRIMARY; window on SECOND
    assert sn.handle_show_numbers(harness.app, "") is True
    harness.runtime.run_on_ui_thread()
    assert harness.constructed == []
    assert harness.spoken == [sn.NOTHING_SPOKEN]
    assert harness.chips[-1][1] == "error"
    assert not sn.is_overlay_active()


def test_few_elements_in_an_opaque_app_draws_them_and_warns(harness):
    harness.fg.update(exe="claude.exe", cls="Chrome_WidgetWin_1", kind="chromium")
    harness.enumerate_as([_el((4010, 110, 4100, 150)), _el((5400, 110, 5490, 150))], nodes=44)
    assert sn.handle_show_numbers(harness.app, "") is True
    harness.runtime.run_on_ui_thread()
    assert harness.constructed == ["samsara-qt"]
    assert harness.spoken == ["Only 2 found. This app hides its controls."]
    assert harness.chips[-1] == ("only 2: app hides its controls", "warning")


def test_refresh_with_nothing_reports_too(harness):
    harness.enumerate_as([], nodes=5)
    assert sn.handle_refresh_numbers(harness.app, "") is DispatchState.FAILED
    assert harness.chips and harness.chips[-1][1] == "error"


@pytest.mark.parametrize("kind, count, nodes, expected", [
    ("chromium", 4, 44, True),       # Claude desktop, measured
    ("native", 1, 7, True),          # Warp, measured
    ("native", 99, 163, False),      # Explorer, measured
    ("chromium", 99, 1280, False),   # Brave, measured
    ("native", 3, 60, False),        # a small real dialog
])
def test_opaque_classification_matches_measurements(kind, count, nodes, expected):
    assert sn._looks_opaque(kind, count, nodes) is expected
