"""Queue 70 -- "click <text>": on-device OCR click-by-text.

  * OCR words map to the right physical screen rectangles, and through queue
    56's per-monitor mapping to the right overlay positions on a mocked
    mixed-DPI, three-monitor layout
  * a real window with known text on every connected monitor is read back at
    the right place (Windows only; shows a small window briefly)
  * two identical strings -> numbered labels, never a click; saying the
    number then clicks the chosen one
  * no match -> spoken and on-chip "nothing on screen matches that", FAILED,
    nothing clicked, no overlay
  * a homophone of an on-screen word matches it; near matches are labelled,
    not clicked
  * key names ("press enter") never read the screen; label numbers keep the
    numbered path
  * a stale match (window moved / lost focus) is reported, not clicked, and
    the cursor is placed inside a per-monitor-v2 thread context

Never imports dictation. OCR, capture and the overlay are faked except in the
one real-window test.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import plugins.commands.show_numbers as sn  # noqa: E402
from samsara import screen_ocr as so  # noqa: E402
from samsara.command_registry import DispatchState  # noqa: E402

PRIMARY = ((0, 0, 3840, 2160), (0, 0, 2560, 1440), 1.5)
SECOND = ((3840, 0, 5760, 1080), (3840, 0, 5760, 1080), 1.0)
THIRD = ((5760, -1000, 9600, 1160), (5760, -1000, 8320, 440), 1.5)
MAPPINGS = [PRIMARY, SECOND, THIRD]
HWND = 0x1234


def _line(*words):
    """words: (text, left, top, right, bottom) in physical pixels."""
    return so.Line(tuple(so.Word(t, (l, tp, r, b)) for t, l, tp, r, b in words))


def _snapshot(lines, window=(100, 100, 1500, 1100)):
    return so.Snapshot(HWND, window, window, tuple(lines), 1, {"total": 90.0})


class _App:
    def __init__(self):
        self.spoken, self.chips = [], []
        self.audio_coordinator = SimpleNamespace(speak=lambda text, **k: self.spoken.append(text))

    def _show_outcome_chip(self, label, kind):
        self.chips.append((label, kind))


@pytest.fixture
def rig(monkeypatch):
    """handle_click with OCR, overlay, chips and clicks faked."""
    app = _App()
    state = SimpleNamespace(snapshot=None, drawn=None, clicked=[], reads=0, reaches=lambda x, y: True)

    def read_foreground(hwnd=None, scale=so.OCR_SCALE):
        state.reads += 1
        return state.snapshot

    def draw(app_, elements, caption="", fg_info=None):
        state.drawn = (list(elements), caption, fg_info)
        with sn._state_lock:
            sn._elements[:] = [e["control"] for e in elements]

    monkeypatch.setattr(so, "read_foreground", read_foreground)
    monkeypatch.setattr(so, "foreground_hwnd", lambda: HWND)
    monkeypatch.setattr(so, "point_reaches_window", lambda hwnd, x, y: state.reaches(x, y))
    monkeypatch.setattr(sn, "_foreground_info", lambda hwnd: {"hwnd": hwnd, "exe": "obsidian.exe",
                                                               "cls": "Chrome_WidgetWin_1", "kind": "chromium",
                                                               "rect": (92, 92, 1508, 1108)})
    monkeypatch.setattr(sn, "_draw_overlay", draw)
    monkeypatch.setattr(sn, "_destroy_overlay", lambda app_=None: None)
    monkeypatch.setattr(sn, "_click_with_validation",
                        lambda element, modifier, keys=frozenset(): state.clicked.append((element, modifier, keys)) or True)
    # Chips fire on a timer in the app; run them inline here.
    monkeypatch.setattr(sn, "_report_chip", lambda app_, label, kind, delay_s=0: app_._show_outcome_chip(label, kind))
    with sn._state_lock:
        sn._elements.clear()
    yield app, state
    with sn._state_lock:
        sn._elements.clear()


# ---------------------------------------------------------------------------
# coordinates
# ---------------------------------------------------------------------------

class TestCoordinates:
    def test_ocr_image_coordinates_map_to_physical_screen_rectangles(self):
        # A window captured at physical (5900, -800) on the 150% monitor, OCR'd at 2x.
        raw = [[("Export", 40, 60, 120, 28), ("PDF", 180, 60, 60, 28)],
               [("Settings", 1000, 400, 160, 30)]]
        lines = so.lines_to_screen(raw, (5900, -800), scale=2)
        assert [w.rect for w in lines[0].words] == [(5920, -770, 5980, -756), (5990, -770, 6020, -756)]
        assert lines[1].words[0].rect == (6400, -600, 6480, -585)

    @pytest.mark.parametrize("mapping,window,word,expected_logical", [
        # 150% primary: physical (1200, 900) -> logical (800, 600)
        (PRIMARY, (300, 300, 2100, 1500), (1200, 900, 1320, 930), (800, 600, 880, 620)),
        # 100% secondary keeps its native origin
        (SECOND, (4000, 100, 5000, 900), (4100, 300, 4200, 320), (4100, 300, 4200, 320)),
        # 150% monitor at negative y: relative to its own origin
        (THIRD, (6000, -900, 7500, 0), (6060, -700, 6210, -670), (5960, -800, 6060, -780)),
    ])
    def test_matched_run_lands_at_the_right_overlay_position_on_mixed_dpi(
            self, mapping, window, word, expected_logical):
        lines = [_line(("Export",) + word)]
        match = so.find_matches("export", lines)[0]
        assert match.rect == word
        plan = sn.plan_overlay([{"control": None, "prect": match.rect, "name": "Export",
                                 "type": sn.OCR_ELEMENT_TYPE}], window, MAPPINGS)
        assert plan["screen_rect"] == mapping[1]
        assert [k["rect"] for k in plan["kept"]] == [expected_logical]

    @pytest.mark.skipif(sys.platform != "win32", reason="real screen capture is Windows-only")
    def test_real_window_text_is_read_back_where_it_is_on_every_monitor(self, qapp):
        pytest.importorskip("winsdk.windows.media.ocr")
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QLabel, QWidget
        if any(s.devicePixelRatio() != 1.0 for s in QApplication.screens()):
            pytest.skip("uses Qt logical positions as physical; needs every monitor at 100%")
        known = [("Export", 30, 30), ("Settings", 240, 30), ("Export", 30, 170)]
        for screen in QApplication.screens():
            g = screen.availableGeometry()
            win = QWidget(None, Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint
                          | Qt.WindowType.Tool)
            win.setStyleSheet("background: white;")
            win.setGeometry(g.x() + 120, g.y() + 120, 420, 260)
            labels = []
            for text, x, y in known:
                label = QLabel(text, win)
                label.setFont(QFont("Segoe UI", 11))
                label.setStyleSheet("color: black;")
                label.move(x, y)
                label.adjustSize()
                labels.append(label)
            win.show()
            try:
                for _ in range(15):
                    qapp.processEvents()
                    time.sleep(0.02)
                snap = so.read_foreground(int(win.winId()), scale=1)
                for (text, _x, _y), label in zip(known, labels):
                    c = label.mapToGlobal(label.rect().center())
                    assert any(w.text == text and w.rect[0] <= c.x() <= w.rect[2] and w.rect[1] <= c.y() <= w.rect[3]
                               for line in snap.lines for w in line.words), (screen.name(), text)
                assert [m.exact for m in so.find_matches("export", snap.lines)] == [True, True]
            finally:
                win.close()
                qapp.processEvents()


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

class TestMatching:
    def test_homophone_of_an_on_screen_word_matches_it(self):
        lines = [_line(("Write", 10, 10, 60, 30)), _line(("Step", 10, 50, 50, 70), ("4", 55, 50, 65, 70))]
        assert [m.text for m in so.find_matches("right", lines)] == ["Write"]
        assert so.find_matches("right", lines)[0].exact
        assert [m.text for m in so.find_matches("step for", lines)] == ["Step 4"]

    def test_multi_word_run_is_one_rectangle_and_punctuation_is_ignored(self):
        lines = [_line(("New", 10, 10, 40, 30), ("chat…", 45, 10, 90, 30), ("Help", 200, 10, 240, 30))]
        (m,) = so.find_matches("new chat", lines)
        assert m.exact and m.rect == (10, 10, 90, 30)

    def test_near_match_is_flagged_not_exact(self):
        lines = [_line(("Setings", 10, 10, 70, 30))]   # OCR dropped a letter
        (m,) = so.find_matches("settings", lines)
        assert not m.exact and m.score < 1.0

    def test_exact_matches_suppress_near_ones(self):
        lines = [_line(("Settings", 10, 10, 70, 30)), _line(("Setting", 10, 50, 70, 70))]
        assert [(m.text, m.exact) for m in so.find_matches("settings", lines)] == [("Settings", True)]

    def test_short_queries_never_fuzzy_match(self):
        assert so.find_matches("ok", [_line(("on", 10, 10, 30, 30))]) == []

    def test_unrelated_text_does_not_match(self):
        assert so.find_matches("export", [_line(("Import", 10, 10, 60, 30))]) == []


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------

class TestClickText:
    def test_single_exact_match_is_clicked(self, rig):
        app, state = rig
        state.snapshot = _snapshot([_line(("File", 120, 120, 150, 140), ("Export", 400, 120, 460, 140))])
        assert sn.handle_click(app, "export") is True
        (element, modifier, _keys), = state.clicked
        assert (element.BoundingRectangle.left, element.BoundingRectangle.right) == (400, 460)
        assert modifier == "single" and state.drawn is None

    def test_two_identical_strings_are_numbered_not_clicked(self, rig):
        app, state = rig
        state.snapshot = _snapshot([_line(("Export", 400, 600, 460, 620)),
                                    _line(("Export", 400, 200, 460, 220))])
        assert sn.handle_click(app, "export") is True
        assert state.clicked == []
        elements, caption, fg = state.drawn
        assert [e["prect"][1] for e in elements] == [200, 600]          # numbered top to bottom
        assert all(e["type"] == sn.OCR_ELEMENT_TYPE for e in elements)
        assert fg["rect"] == state.snapshot.window_rect                  # physical frame, not virtualised
        assert app.spoken == ["2 matches. Say click and a number."]
        assert "say click and a number" in caption
        # ...then the user picks number 2
        assert sn.handle_click(app, "2") is True
        (element, _m, _k), = state.clicked
        assert element.BoundingRectangle.top == 600

    def test_no_match_is_said_plainly_and_nothing_is_clicked(self, rig):
        app, state = rig
        state.snapshot = _snapshot([_line(("File", 120, 120, 150, 140))])
        assert sn.handle_click(app, "export") is DispatchState.FAILED
        assert state.clicked == [] and state.drawn is None
        assert app.spoken == ["Nothing on screen matches that."]
        assert app.chips and "nothing on screen matches that" in app.chips[-1][0]
        assert "icons have no text" in app.chips[-1][0]

    def test_text_covered_by_another_window_is_not_a_match(self, rig):
        app, state = rig
        state.snapshot = _snapshot([_line(("Export", 400, 120, 460, 140))])
        state.reaches = lambda x, y: False
        assert sn.handle_click(app, "export") is DispatchState.FAILED
        assert state.clicked == []

    def test_near_match_alone_is_labelled_for_confirmation_not_clicked(self, rig):
        app, state = rig
        state.snapshot = _snapshot([_line(("Setings", 400, 120, 470, 140))])
        assert sn.handle_click(app, "settings") is True
        assert state.clicked == []
        assert len(state.drawn[0]) == 1
        assert app.spoken == ["Not sure. Say click one to confirm."]

    def test_homophone_through_the_command_clicks_the_word(self, rig):
        app, state = rig
        state.snapshot = _snapshot([_line(("New", 400, 120, 440, 140))])
        assert sn.handle_click(app, "knew") is True
        assert len(state.clicked) == 1

    def test_modifiers_carry_into_the_text_click(self, rig):
        app, state = rig
        state.snapshot = _snapshot([_line(("Export", 400, 120, 460, 140))])
        sn.handle_click(app, "ctrl export twice")
        (_e, modifier, keys), = state.clicked
        assert modifier == "double" and keys == frozenset({"ctrl"})

    @pytest.mark.parametrize("remainder", ["enter", "escape", "page down", "f5"])
    def test_key_names_never_read_the_screen(self, rig, remainder):
        app, state = rig
        assert sn.handle_click(app, remainder) is True
        assert state.reads == 0 and state.clicked == []

    @pytest.mark.parametrize("remainder", ["7", "thirty seven", "number 7", "7 twice"])
    def test_label_numbers_keep_the_numbered_path(self, rig, remainder):
        app, state = rig
        sn.handle_click(app, remainder)
        assert state.reads == 0

    def test_unreadable_screen_is_reported(self, rig, monkeypatch):
        app, state = rig

        def boom(hwnd=None, scale=1):
            raise RuntimeError("no OCR recognizer")
        monkeypatch.setattr(so, "read_foreground", boom)
        assert sn.handle_click(app, "export") is DispatchState.FAILED
        assert app.spoken == ["Could not read the screen."] and state.clicked == []


class TestOcrTarget:
    def test_stale_match_is_reported_not_clicked(self, monkeypatch):
        performed = []
        monkeypatch.setattr(sn, "_perform_click", lambda *a, **k: performed.append(a) or True)
        monkeypatch.setattr(so, "foreground_hwnd", lambda: 0x9999)          # focus moved elsewhere
        monkeypatch.setattr(so, "window_frame_rect", lambda hwnd: (100, 100, 1500, 1100))
        target = sn._OcrTarget((400, 120, 460, 140), HWND, (100, 100, 1500, 1100), "Export")
        assert sn._click_with_validation(target, "single") is False
        monkeypatch.setattr(so, "foreground_hwnd", lambda: HWND)
        monkeypatch.setattr(so, "window_frame_rect", lambda hwnd: (140, 100, 1540, 1100))  # window moved
        assert sn._click_with_validation(target, "single") is False
        assert performed == []

    def test_cursor_is_placed_inside_a_physical_dpi_context(self, monkeypatch):
        import win32api
        events, in_context = [], []

        def physical(fn):
            in_context.append(True)
            try:
                return fn()
            finally:
                in_context.pop()

        monkeypatch.setattr(sn, "_with_physical_dpi_context", physical)
        monkeypatch.setattr(win32api, "SetCursorPos", lambda pos: events.append(("move", pos, bool(in_context))))
        monkeypatch.setattr(win32api, "mouse_event", lambda flag, *a: events.append(("button", flag)))
        sn._OcrTarget((400, 120, 460, 140), HWND, (0, 0, 1, 1), "Export").Click()
        assert events[0] == ("move", (430, 130), True)
        assert len(events) == 3


def test_ocr_labels_do_not_trigger_the_app_hides_its_controls_warning(monkeypatch):
    app = _App()
    shown = []
    monkeypatch.setattr(sn, "current_monitor_mappings",
                        lambda: [(p, q, d, SimpleNamespace(name=lambda: "S", devicePixelRatio=lambda: d))
                                 for p, q, d in MAPPINGS])
    monkeypatch.setattr(sn, "_show_overlay_qt", lambda labels, *a, **k: shown.append(labels))
    monkeypatch.setattr(sn, "_report_chip", lambda *a, **k: None)
    elements = [{"control": object(), "prect": (400, 400, 460, 420), "name": "Export", "type": sn.OCR_ELEMENT_TYPE},
                {"control": object(), "prect": (400, 800, 460, 820), "name": "Export", "type": sn.OCR_ELEMENT_TYPE}]
    sn._render_elements_qt(app, elements, {"hwnd": HWND, "kind": "chromium", "rect": (100, 100, 1500, 1100)})
    assert len(shown) == 1 and len(shown[0]) == 2
    assert app.spoken == []
    with sn._state_lock:
        sn._elements.clear()
