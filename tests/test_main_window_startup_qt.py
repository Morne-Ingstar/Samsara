"""64: the Home window must be on screen when startup ends.

Real "windows" QPA widgets against the session `qapp` (see
test_main_window_qt.py for why qt_runtime itself is not driven here).
Visibility is asserted from the window's own state, not from show() calls.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QWidget

from samsara.ui import main_window_qt


def _app(**config):
    return types.SimpleNamespace(
        config=dict(config),
        create_icon_image=lambda *a, **k: None,
        _tray_mark=lambda: ("idle", "off"),
    )


@pytest.fixture
def startup(qapp, monkeypatch):
    monkeypatch.setattr(main_window_qt, "HistoryView", lambda *a, **k: QWidget())
    made = []

    def build(**config):
        mw = main_window_qt.MainWindowQt(_app(**config))
        made.append(mw)
        return mw

    yield build
    for mw in made:
        win = mw._window
        if win is not None:
            win._poll_timer.stop()
            win._header_mark.stop()
            win.hide()
            win.deleteLater()
    qapp.processEvents()


def _center_on_some_screen(win):
    c = win.frameGeometry().center()
    return any(s.availableGeometry().contains(c) for s in QApplication.screens())


class TestStartupShowsWindow:
    def test_init_window_leaves_home_visible_on_a_monitor(self, qapp, startup):
        mw = startup()
        mw._init_window()
        qapp.processEvents()

        win = mw._window
        assert win is not None
        assert win.isVisible()
        assert not win.isMinimized()
        assert _center_on_some_screen(win)

    def test_construction_failure_is_logged_not_silent(self, qapp, startup, monkeypatch, caplog):
        def boom(app):
            raise RuntimeError("panel exploded")
        monkeypatch.setattr(main_window_qt, "_MainWindow", boom)
        mw = startup()
        mw._init_posted = True

        with caplog.at_level("INFO"):
            mw._init_window()

        assert mw._window is None
        assert mw._init_posted is False  # a later show() can retry
        assert "main window construction failed" in caplog.text
        assert "panel exploded" in caplog.text

    def test_sequence_is_logged(self, qapp, startup, caplog):
        mw = startup()
        with caplog.at_level("INFO"):
            mw._init_window()
        text = caplog.text
        assert "_init_window entered" in text
        assert "startup: show() called, isVisible=True windowState=normal" in text
        assert "_init_window completed" in text

    def test_foreground_lock_raises_without_activation_and_logs(
            self, qapp, startup, monkeypatch, caplog):
        mw = startup()
        mw._init_window()
        raised = []
        monkeypatch.setattr(main_window_qt, "_is_foreground", lambda w: False)
        monkeypatch.setattr(main_window_qt, "_raise_without_activation",
                            lambda w: raised.append(w) or True)
        monkeypatch.setattr(main_window_qt.sys, "platform", "win32")

        with caplog.at_level("INFO"):
            mw._confirm_in_front("startup")

        assert raised == [mw._window]
        assert "refused by the Windows foreground lock" in caplog.text
        assert mw._window.isVisible()


_CHILD = r'''
import sys, types
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QWidget
app = QApplication([])
from samsara.ui import main_window_qt, splash_qt
main_window_qt.HistoryView = lambda *a, **k: QWidget()
splash = splash_qt._SplashWidget(); splash.show(); app.processEvents()   # popup first, as at boot
mw = main_window_qt.MainWindowQt(types.SimpleNamespace(
    config={}, create_icon_image=lambda *a, **k: None, _tray_mark=lambda: ("idle", "off")))
mw._init_window()
for _ in range(10):
    app.processEvents()
win = mw._window
print(f"visible={win.isVisible()} minimized={win.isMinimized()}")
win._poll_timer.stop(); win._header_mark.stop(); splash.close()
'''


class TestLaunchedMinimized:
    """64, second pass: the 2026-09-15 02:57 boot logged windowState=minimized
    right after show(). Samsara had been started from a "Run: Minimized"
    shortcut (STARTUPINFO wShowWindow=SW_SHOWMINNOACTIVE), which Windows
    applies to the process's first ShowWindow on an ordinary window."""

    def test_window_minimized_by_the_launcher_on_first_show_is_restored(
            self, qapp, startup, monkeypatch, caplog):
        calls = []

        def launcher_override(self):   # what Windows does to the first show
            calls.append(1)
            self.showMinimized()

        monkeypatch.setattr(main_window_qt._MainWindow, "show", launcher_override)
        mw = startup()
        with caplog.at_level("INFO"):
            mw._init_window()
        qapp.processEvents()

        win = mw._window
        assert calls == [1]
        assert win.isVisible()
        assert not win.isMinimized()
        assert "startup: show() called, isVisible=True windowState=minimized" in caplog.text
        assert "window came up minimized on its first show" in caplog.text
        assert "startup restore: show() called, isVisible=True windowState=normal" in caplog.text

    @pytest.mark.skipif(sys.platform != "win32", reason="STARTUPINFO show state is Windows-only")
    def test_process_started_minimized_ends_startup_with_home_shown(self):
        import subprocess
        root = str(Path(__file__).resolve().parent.parent)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7   # SW_SHOWMINNOACTIVE, a "Run: Minimized" shortcut
        out = subprocess.run([sys.executable, "-c", _CHILD, root], startupinfo=si,
                             capture_output=True, text=True, timeout=90, cwd=root)
        assert "visible=True minimized=False" in out.stdout, out.stdout + out.stderr[-2000:]


class TestSavedGeometryOffScreen:
    PRIMARY = QRect(0, 0, 2560, 1400)
    SCREENS = [PRIMARY, QRect(2560, 1, 1920, 1040), QRect(4480, -496, 3840, 2120)]

    def test_position_on_current_primary_is_kept(self):
        assert main_window_qt.placement_on_screens(
            845, 243, 1065, 906, self.SCREENS, self.PRIMARY) == (845, 243, 1065, 906)

    def test_position_on_secondary_with_negative_y_is_kept(self):
        # The old primary-only clamp dragged this back onto monitor 1.
        assert main_window_qt.placement_on_screens(
            5000, -300, 1065, 906, self.SCREENS, self.PRIMARY) == (5000, -300, 1065, 906)

    def test_position_on_disconnected_monitor_is_centred_on_primary(self):
        x, y, w, h = main_window_qt.placement_on_screens(
            -3000, 200, 1065, 906, self.SCREENS, self.PRIMARY)
        assert self.PRIMARY.contains(QRect(x, y, w, h))

    def test_title_bar_above_every_monitor_is_corrected(self):
        x, y, w, h = main_window_qt.placement_on_screens(
            845, -2000, 1065, 906, self.SCREENS, self.PRIMARY)
        assert self.PRIMARY.contains(QRect(x, y, w, h))

    def test_oversized_window_is_shrunk_to_primary(self):
        x, y, w, h = main_window_qt.placement_on_screens(
            99999, 99999, 4000, 3000, [self.PRIMARY], self.PRIMARY)
        assert (w, h) == (2560, 1400)
        assert self.PRIMARY.contains(QRect(x, y, w, h))

    def test_real_window_restored_off_screen_lands_on_a_monitor(self, qapp, startup):
        mw = startup(window_x=-50000, window_y=-50000, window_width=900, window_height=650)
        mw._init_window()
        qapp.processEvents()
        assert mw._window.isVisible()
        assert _center_on_some_screen(mw._window)


class TestSplashHandOff:
    def test_splash_closing_leaves_main_visible_and_in_front(self, qapp, startup, monkeypatch):
        from samsara.ui.splash_qt import _SplashWidget

        splash = _SplashWidget()
        splash.show()
        qapp.processEvents()
        assert splash.objectName() == main_window_qt.SPLASH_OBJECT_NAME

        mw = startup()
        mw._init_window()
        qapp.processEvents()
        surfaced = []
        real_surface = mw._surface
        monkeypatch.setattr(mw, "_surface", lambda r: (surfaced.append(r), real_surface(r)))

        splash.close()  # WA_DeleteOnClose -> destroyed -> re-surface
        for _ in range(20):
            qapp.processEvents()
            if surfaced:
                break
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for _ in range(20):
            qapp.processEvents()

        win = mw._window
        assert surfaced == ["splash closed"]
        assert win.isVisible()
        assert not win.isMinimized()
        # In front: the top-level at the window's centre is the main window.
        c = win.frameGeometry().center()
        top = QApplication.topLevelAt(c)
        assert top is win

    def test_user_hidden_window_is_not_re_shown_after_splash(self, qapp, startup):
        mw = startup()
        mw._init_window()
        mw._window.hide()
        mw._after_splash_closed()
        assert not mw._window.isVisible()
