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
