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
                            lambda painter, rect, capture, eye, rotation=0.0, opacity=1.0:
                            painted.append((capture, eye)))
        app.pair = pair
        before = app.tray_mark_calls
        win._header_mark.refresh()
        assert app.tray_mark_calls == before + 1
        assert (win._header_mark.frame.capture, win._header_mark.frame.eye) == pair
        win._header_mark.grab()            # renders paintEvent even while hidden
        assert painted and painted[-1] == pair

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
