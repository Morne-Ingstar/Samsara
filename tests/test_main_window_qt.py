"""Tests for samsara.ui.main_window_qt.MainWindowQt's show()/restore-from-
minimized behavior (tray "Show Samsara" / tray-icon-click bug fix).

qt_runtime itself is NOT exercised here -- qt_runtime.ensure_started() can
only ever run once per process and cannot be restarted after shutdown, so
(matching the existing precedent in test_history_view.py/test_settings.py)
these tests construct widgets directly against the session-scoped `qapp`
fixture and call the Qt-thread methods in-process, rather than going
through the real post()-to-a-background-thread marshaling. show() itself
is covered by asserting it posts the correct single callable (verified via
monkeypatching qt_runtime.post) rather than by running the real event loop
thread.

Real isMinimized()/showMinimized()/showNormal() Qt window-state calls DO
work headlessly in this environment (verified empirically: this machine
runs the real "windows" QPA platform, not "offscreen"), so this is a real
behavioral test, not a fake one.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QWidget

from samsara.ui import main_window_qt


def _make_app():
    return types.SimpleNamespace(config={})


class TestShowPostsSingleCallable:
    """show() must post ONE callable that does restore+raise+activate --
    not three separate posts (the original bug: three separate posted
    show()/raise_()/activateWindow() calls, where a bare show() alone
    never clears the minimized state)."""

    def test_show_posts_show_and_raise_when_window_exists(self, monkeypatch):
        mw = main_window_qt.MainWindowQt(_make_app())
        mw._window = object()  # sentinel -- show() only needs it to be non-None
        posted = []
        monkeypatch.setattr(main_window_qt.qt_runtime, 'post', posted.append)

        mw.show()

        assert posted == [mw._show_and_raise]

    def test_show_does_not_re_post_init_when_window_already_exists(self, monkeypatch):
        mw = main_window_qt.MainWindowQt(_make_app())
        mw._window = object()
        mw._init_posted = False
        posted = []
        monkeypatch.setattr(main_window_qt.qt_runtime, 'post', posted.append)

        mw.show()

        assert mw._init_posted is False
        assert posted == [mw._show_and_raise]

    def test_show_posts_init_window_on_first_call(self, monkeypatch):
        mw = main_window_qt.MainWindowQt(_make_app())
        posted = []
        monkeypatch.setattr(main_window_qt.qt_runtime, 'post', posted.append)

        mw.show()

        assert posted == [mw._init_window]
        assert mw._init_posted is True

    def test_show_does_not_double_post_init_on_repeated_calls(self, monkeypatch):
        mw = main_window_qt.MainWindowQt(_make_app())
        posted = []
        monkeypatch.setattr(main_window_qt.qt_runtime, 'post', posted.append)

        mw.show()
        mw.show()

        assert posted == [mw._init_window]


class TestShowAndRaiseRestoresMinimizedWindow:
    def test_minimized_window_is_restored(self, qapp):
        mw = main_window_qt.MainWindowQt(_make_app())
        window = QWidget()
        mw._window = window
        try:
            window.show()
            window.showMinimized()
            qapp.processEvents()
            assert window.isMinimized()

            mw._show_and_raise()
            qapp.processEvents()

            assert not window.isMinimized()
        finally:
            window.close()

    def test_non_minimized_window_is_just_shown(self, qapp):
        mw = main_window_qt.MainWindowQt(_make_app())
        window = QWidget()
        mw._window = window
        try:
            window.show()
            qapp.processEvents()
            assert not window.isMinimized()

            mw._show_and_raise()
            qapp.processEvents()

            assert not window.isMinimized()
            assert window.isVisible()
        finally:
            window.close()

    def test_noop_when_window_is_none(self):
        mw = main_window_qt.MainWindowQt(_make_app())
        mw._window = None

        mw._show_and_raise()  # must not raise


# ---------------------------------------------------------------------------
# 09b4: the live Samsara mark in the shared header
# ---------------------------------------------------------------------------

class _MarkApp:
    """A mocked app: its _tray_mark is scripted, and create_icon_image is the
    REAL DictationApp method, so the header provably reads the tray's own
    accessor (no second copy of the priority logic)."""

    def __init__(self, pair=("idle", "off")):
        import dictation

        self.config = {}
        self.pair = pair
        self.tray_mark_calls = 0
        self.create_icon_image = dictation.DictationApp.create_icon_image.__get__(self)

    def _tray_mark(self):
        self.tray_mark_calls += 1
        return self.pair


@pytest.fixture
def window(qapp, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(main_window_qt, "HistoryView", lambda *a, **k: QWidget())
    monkeypatch.setattr(main_window_qt.QTimer, "singleShot", MagicMock())
    app = _MarkApp()
    win = main_window_qt._MainWindow(app)
    yield win, app
    win._poll_timer.stop()
    win._header_mark.stop()
    win.hide()
    win.deleteLater()


class TestHeaderMark:
    def test_mark_sits_left_of_the_wordmark_with_a_12px_gap(self, window):
        win, _app = window
        mark = win._header_mark
        layout = mark.parentWidget().layout()
        widgets = [layout.itemAt(i) for i in range(layout.count())]
        assert widgets[0].widget() is mark
        assert widgets[1].spacerItem() is not None and widgets[1].sizeHint().width() == 12
        assert isinstance(widgets[2].widget(), main_window_qt.QLabel)
        assert widgets[2].widget().text() == "Samsara"
        assert mark.width() == mark.height() == 26

    @pytest.mark.parametrize("pair", [("listening", "armed"), ("recording", "off"), ("idle", "asleep")])
    def test_renders_the_frame_tray_mark_returned(self, window, monkeypatch, pair):
        win, app = window
        painted = []
        monkeypatch.setattr(main_window_qt, "paint_mark",
                            lambda painter, rect, capture, eye, rotation=0.0, opacity=1.0, brand=False:
                            painted.append((capture, eye, brand)))
        app.pair = pair
        before = app.tray_mark_calls
        win._header_mark.refresh()
        assert app.tray_mark_calls == before + 1
        assert (win._header_mark.frame.capture, win._header_mark.frame.eye) == pair
        win._header_mark.grab()            # renders paintEvent even while hidden
        assert painted and painted[-1] == (*pair, True)   # 38: brand presentation

    def test_accessible_name_follows_state(self, window):
        win, app = window
        mark = win._header_mark
        app.pair = ("listening", "armed")
        mark.refresh()
        assert mark.accessibleName() == "Samsara \u2014 listening, hands-free armed"
        app.pair = ("recording", "off")
        mark.refresh()
        assert mark.accessibleName() == "Samsara \u2014 recording, hands-free off"
        assert win._badge.text() == "ready"          # badge text unchanged by the mark

    def test_timer_runs_while_shown_and_stops_on_hide(self, window, qapp):
        win, _app = window
        win.show()
        qapp.processEvents()
        assert win._header_mark._timer.isActive()
        win.hide()
        qapp.processEvents()
        assert not win._header_mark._timer.isActive()

    def test_app_without_the_accessor_shows_idle(self, qapp):
        from PySide6.QtWidgets import QWidget as _W

        mark = main_window_qt._HeaderMark(types.SimpleNamespace(config={}))
        mark.refresh()
        assert (mark.frame.capture, mark.frame.eye) == ("idle", "off")


# ---------------------------------------------------------------------------
# 38: brand lockup, sidebar spine, paused affordance
# ---------------------------------------------------------------------------

from samsara.ui import theme as _theme
from samsara.ui import tray_qt as _tray


class TestBrandHeaderAndSidebar:
    def test_header_mark_is_accent_with_an_eye_at_rest(self, window, qapp):
        """The 26 px lockup at rest: ACCENT, never ICON_IDLE, eye present."""
        win, app = window
        app.pair = ("idle", "off")
        win._header_mark.refresh()
        image = win._header_mark.grab().toImage()
        accent = _theme._hex_to_rgb(_theme.ACCENT)
        grey = _theme._hex_to_rgb(_theme.ICON_IDLE)

        def near(c, rgb, tol=30):
            return abs(c.red() - rgb[0]) <= tol and abs(c.green() - rgb[1]) <= tol and abs(c.blue() - rgb[2]) <= tol

        pixels = [image.pixelColor(x, y) for y in range(image.height()) for x in range(image.width())]
        assert sum(near(c, accent) for c in pixels) > 20
        assert sum(near(c, grey) and not near(c, accent) for c in pixels) == 0
        centre = image.pixelColor(image.width() // 2, image.height() // 2)
        assert near(centre, accent), "the closed lid is drawn at the centre when hands-free is off"

    def test_wordmark_uses_the_display_face_on_a_bordered_band(self, window):
        win, _ = window
        title = win.findChild(main_window_qt.QLabel, "hubWordmark")
        assert title is not None and title.text() == "Samsara"
        css = title.styleSheet()
        assert _theme.FONT_FAMILY_DISPLAY in css and f"{_theme.FONT_SIZE_DISPLAY}px" in css
        assert "letter-spacing" in css
        header = title.parentWidget()
        assert header.height() == main_window_qt.HEADER_H
        assert f"border-bottom: 1px solid {_theme.BORDER}" in header.styleSheet()

    def test_sidebar_and_content_are_different_planes(self, window):
        win, _ = window
        sidebar = win._nav_btns["Home"].parentWidget()
        assert f"background: {_theme.BG0}" in sidebar.styleSheet()
        assert f"background: {_theme.BG1}" in win._stack.styleSheet()

    def test_nav_rows_are_44px_with_icons_and_a_selected_state_beyond_colour(self, window, qapp):
        win, _ = window
        win.show(); qapp.processEvents()
        for name, btn in win._nav_btns.items():
            assert btn.minimumHeight() >= 44 and btn.height() >= 44, name
            assert not btn.icon().isNull(), f"{name} has no icon"
            assert btn.accessibleName() == btn.text() == name
        rest = main_window_qt._MainWindow._nav_style(False)
        selected = main_window_qt._MainWindow._nav_style(True)
        assert f"border-left: 2px solid {_theme.ACCENT}" in selected and f"background: {_theme.BG2}" in selected
        assert "font-weight: 600" in selected and _theme.TEXT_PRIMARY in selected
        assert "border-left: 2px solid transparent" in rest and "font-weight: 400" in rest
        assert _theme.TEXT_SECONDARY in rest and "QPushButton:hover" in rest
        assert win._nav_btns["Home"].isChecked() and not win._nav_btns["History"].isChecked()
        assert win._nav_btns["Home"].styleSheet() == selected
        assert win._nav_btns["History"].styleSheet() == rest

    def test_paused_is_visible_and_exitable(self, window, qapp):
        win, app = window
        calls = []
        app.snoozed = True
        app.resume_listening = lambda: (calls.append("resume"), setattr(app, "snoozed", False))
        win._refresh_status()
        assert win._paused_btn.isVisibleTo(win) and not win._badge.isVisibleTo(win)
        assert win._paused_btn.text() == win._paused_btn.accessibleName() == "Paused \u2014 resume"
        assert win._paused_btn.minimumHeight() >= 44
        win._paused_btn.click()
        assert calls == ["resume"]
        assert not win._paused_btn.isVisibleTo(win) and win._badge.isVisibleTo(win)
