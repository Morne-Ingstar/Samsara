"""Standalone History opens explicitly; data refresh remains passive."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from samsara.ui import history_qt, window_activation


@pytest.fixture
def history_harness(qapp, monkeypatch):
    posted = []
    monkeypatch.setattr(history_qt.qt_runtime, "post", posted.append)
    monkeypatch.setattr(window_activation, "_active_modal", lambda: None)
    monkeypatch.setattr(window_activation, "confirm_foreground", lambda _: None)

    class HiddenHistoryWindow(history_qt._HistoryWindow):
        def __init__(self, app):
            super().__init__(app)
            self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            self.calls = []

        def showNormal(self):
            self.calls.append("restore")
            super().showNormal()

        def show(self):
            self.calls.append("show")
            super().show()

        def raise_(self):
            self.calls.append("raise")
            super().raise_()

        def activateWindow(self):
            self.calls.append("activate")
            super().activateWindow()

    monkeypatch.setattr(history_qt, "_HistoryWindow", HiddenHistoryWindow)
    app = SimpleNamespace(history=[(datetime.now().isoformat(), "synthetic entry", False)])
    controller = history_qt.HistoryQt(app)
    try:
        yield controller, posted
    finally:
        if controller._window is not None:
            controller._window.hide()
            controller._window.deleteLater()
            qapp.processEvents()


def _run_posted(posted):
    assert len(posted) == 1
    posted.pop()()


def test_first_history_open_surfaces_on_posted_qt_callback(history_harness):
    controller, posted = history_harness
    controller.show()
    assert controller._window is None
    _run_posted(posted)
    assert controller._window.calls == ["show", "raise", "activate"]


def test_minimized_history_reopen_restores_before_activation(history_harness, qapp):
    controller, posted = history_harness
    controller.show()
    _run_posted(posted)
    window = controller._window
    window.showMinimized()
    qapp.processEvents()
    assert window.isMinimized()
    window.calls.clear()

    controller.show()
    assert window.calls == []
    _run_posted(posted)
    assert window.calls == ["restore", "raise", "activate"]
    assert not window.isMinimized()


def test_background_history_refresh_keeps_minimized_window_passive(history_harness, qapp):
    controller, posted = history_harness
    controller.show()
    _run_posted(posted)
    window = controller._window
    window.showMinimized()
    qapp.processEvents()
    window.calls.clear()

    controller.refresh()
    _run_posted(posted)
    assert window.isMinimized()
    assert window.calls == []


def test_active_modal_blocks_history_open_surface(history_harness, monkeypatch):
    controller, posted = history_harness
    controller.show()
    _run_posted(posted)
    window = controller._window
    window.hide()
    window.calls.clear()
    monkeypatch.setattr(window_activation, "_active_modal", lambda: object())

    controller.show()
    _run_posted(posted)
    assert window.calls == []
    assert not window.isVisible()


def test_active_modal_blocks_first_history_open_surface(history_harness, monkeypatch):
    controller, posted = history_harness
    monkeypatch.setattr(window_activation, "_active_modal", lambda: object())

    controller.show()
    _run_posted(posted)

    assert controller._window.calls == []
    assert not controller._window.isVisible()
