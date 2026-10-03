"""Explicit window opens use one Qt-thread activation attempt; passive work does not."""

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from samsara.ui import main_window_qt, settings_qt, window_activation


class _Window(QWidget):
    def __init__(self):
        super().__init__()
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


def test_explicit_open_restores_minimized_and_orders_activation(qapp, monkeypatch):
    window = _Window()
    window.showMinimized()
    qapp.processEvents()
    assert window.isMinimized()
    monkeypatch.setattr(window_activation, "_active_modal", lambda: None)
    try:
        window.calls.clear()
        window_activation.surface_requested(window, confirm=lambda: window.calls.append("confirm"))
        assert window.calls == ["restore", "raise", "activate", "confirm"]
        assert not window.isMinimized()
        window.calls.clear()
        window_activation.surface_requested(window, confirm=lambda: window.calls.append("confirm"))
        assert window.calls == ["show", "raise", "activate", "confirm"]
    finally:
        window.hide()
        window.deleteLater()


def test_modal_dialog_prevents_activation_and_fallback(qapp, monkeypatch):
    window = _Window()
    monkeypatch.setattr(window_activation, "_active_modal", lambda: object())
    try:
        window_activation.surface_requested(window, confirm=lambda: window.calls.append("confirm"))
        assert window.calls == []
    finally:
        window.hide()
        window.deleteLater()


def test_settings_controller_posts_explicit_open_on_qt_thread(monkeypatch):
    posted = []
    opened = []
    monkeypatch.setattr(settings_qt.qt_runtime, "post", posted.append)
    monkeypatch.setattr(settings_qt, "_SettingsWindow", lambda app: SimpleNamespace(
        show_tab=lambda page: opened.append(("tab", page)),
        open_tool=lambda tool: opened.append(("tool", tool)),
    ))
    controller = settings_qt.SettingsQt(object())
    controller.show()
    controller.show_tool("Music")
    assert opened == []
    for callback in posted:
        callback()
    assert opened == [("tab", "General"), ("tool", "Music")]
    assert len(posted) == 2


def test_windows_foreground_refusal_uses_one_no_activation_fallback(qapp, monkeypatch):
    window = _Window()
    actions = []
    monkeypatch.setattr(window_activation.sys, "platform", "win32")
    monkeypatch.setattr(window_activation, "_active_modal", lambda: None)
    monkeypatch.setattr(window_activation, "_is_foreground", lambda _: False)
    monkeypatch.setattr(window_activation, "_raise_without_activation",
                        lambda _: actions.append("no-activate-raise") or True)
    monkeypatch.setattr(window_activation.QApplication, "alert",
                        lambda *_: actions.append("alert"))
    try:
        window_activation.surface_requested(window)
        assert window.calls == ["show", "raise", "activate"]
        assert actions == ["no-activate-raise", "alert"]
        actions.clear()
        monkeypatch.setattr(window_activation, "_is_foreground", lambda _: True)
        window_activation.surface_requested(window)
        assert actions == []
    finally:
        window.hide()
        window.deleteLater()


def test_native_raise_failure_never_requests_topmost_and_reports_refusal(monkeypatch, caplog):
    set_pos = Mock(return_value=0)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        user32=SimpleNamespace(SetWindowPos=set_pos)))
    monkeypatch.setattr(window_activation.sys, "platform", "win32")
    monkeypatch.setattr(window_activation, "_active_modal", lambda: None)
    monkeypatch.setattr(window_activation, "_is_foreground", lambda _: False)
    alerts = []
    monkeypatch.setattr(window_activation.QApplication, "alert", lambda *_: alerts.append(True))
    window = SimpleNamespace(winId=lambda: 123)

    window_activation.confirm_foreground(window)

    set_pos.assert_called_once()
    hwnd, insert_after, _, _, _, _, flags = set_pos.call_args.args
    assert hwnd == 123
    assert insert_after.value is None  # HWND_TOP; never HWND_TOPMOST
    assert flags & 0x0010  # SWP_NOACTIVATE
    assert alerts == [True]
    assert "raise succeeded=False" in caplog.text


def test_native_raise_exception_is_logged_without_second_window_operation(monkeypatch, caplog):
    set_pos = Mock(side_effect=OSError("synthetic SetWindowPos failure"))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        user32=SimpleNamespace(SetWindowPos=set_pos)))
    monkeypatch.setattr(window_activation.sys, "platform", "win32")
    monkeypatch.setattr(window_activation, "_active_modal", lambda: None)
    monkeypatch.setattr(window_activation, "_is_foreground", lambda _: False)

    window_activation.confirm_foreground(SimpleNamespace(winId=lambda: 123))

    set_pos.assert_called_once()
    assert "Could not confirm explicit window activation" in caplog.text


def test_main_explicit_open_uses_shared_surface_and_passive_update_does_not(monkeypatch):
    posted = []
    surfaced = []
    monkeypatch.setattr(main_window_qt.qt_runtime, "post", posted.append)
    monkeypatch.setattr(main_window_qt.window_activation, "surface_requested",
                        lambda window, confirm=None: surfaced.append(window))
    main = main_window_qt.MainWindowQt(SimpleNamespace(config={}))
    geometry = SimpleNamespace(x=lambda: 0, y=lambda: 0, width=lambda: 800, height=lambda: 600)
    window = SimpleNamespace(
        _dictation_sig=SimpleNamespace(emit=lambda text: None),
        geometry=lambda: geometry, isVisible=lambda: True,
        isMinimized=lambda: False, isMaximized=lambda: False,
    )
    main._window = window
    main.on_dictation_complete("dummy")
    assert posted == [] and surfaced == []
    main.show()
    assert len(posted) == 1
    posted.pop()()
    assert surfaced == [window]
    main._after_splash_closed()
    assert surfaced == [window]
