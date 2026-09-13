"""Headless structure tests for the tray declutter pass (2026-07-10).

SamsaraTrayQt._rebuild_menu() is exercised directly against a real QMenu
(built via the session-scoped `qapp` fixture, no visible tray icon needed)
-- confirms every previously-existing action callback is still present
after the reorganization, and that the new grouping (top-level daily-use,
Tools submenu, Developer submenu) matches spec.
"""
from unittest.mock import Mock

import pytest

from samsara.ui import tray_qt
from samsara.ui.tray_qt import SamsaraTrayQt


def _make_app():
    """Mock() as the base so every handler method (only ever CONNECTED to
    a lambda during _rebuild_menu, never CALLED) is automatically present
    and callable. Attributes that ARE read/branched on during construction
    get explicit, correctly-typed overrides -- a bare Mock() there would
    silently satisfy an `if`/f-string with a truthy garbage value instead
    of the real thing being exercised."""
    app = Mock()
    app.config = {
        'microphone': 'mic-1',
        'mode': 'hold',
        'wake_word_config': {'phrase': 'samsara'},
        'wake_word_enabled': False,
        'streaming_mode': False,
        'gesture': {'enabled': False},
        'listening_indicator_enabled': False,
        'cleanup_mode': 'clean',
        'hotkey': 'ctrl+shift',
        'model_size': 'base',
    }
    app.available_mics = [{'id': 'mic-1', 'name': 'Test Microphone'}]
    app._mic_refresh_blocked = Mock(return_value=True)  # skip the mic re-enumeration branch
    app.get_current_microphone_name = Mock(return_value='Test Microphone')
    app.snoozed = False
    app.cheat_sheet = None

    from PIL import Image
    app.create_icon_image = Mock(return_value=Image.new('RGBA', (16, 16)))

    return app


@pytest.fixture
def tray(qapp):
    app = _make_app()
    t = SamsaraTrayQt(app)
    t._rebuild_menu()
    return t, app


def _top_level_texts(menu):
    return [a.text() for a in menu.actions()]


def _submenu(menu, label_substring):
    for a in menu.actions():
        if label_substring in a.text() and a.menu() is not None:
            return a.menu()
    return None


class TestTopLevelDailyUseActions:
    def test_show_samsara_present(self, tray):
        t, app = tray
        assert "Show Samsara" in _top_level_texts(t._menu)

    def test_mode_status_indicator_present(self, tray):
        """Current mode/status indicator -- unchanged from before, still
        one click at top level."""
        t, app = tray
        assert any(txt.startswith("Mode:") for txt in _top_level_texts(t._menu))

    def test_settings_present_top_level(self, tray):
        t, app = tray
        assert "Settings" in _top_level_texts(t._menu)

    def test_history_present_top_level(self, tray):
        t, app = tray
        assert "History" in _top_level_texts(t._menu)

    def test_quick_reference_promoted_to_top_level(self, tray):
        t, app = tray
        assert "Quick Reference" in _top_level_texts(t._menu)

    def test_snooze_is_the_pause_resume_listening_control(self, tray):
        t, app = tray
        assert any(txt in ("Snooze", "Snoozed") for txt in _top_level_texts(t._menu))

    def test_exit_present_and_last(self, tray):
        t, app = tray
        texts = _top_level_texts(t._menu)
        assert texts[-1] == "Exit"

    def test_exit_preceded_by_separator(self, tray):
        t, app = tray
        actions = t._menu.actions()
        assert actions[-1].text() == "Exit"
        assert actions[-2].isSeparator()

    def test_tools_and_developer_submenus_present_top_level(self, tray):
        t, app = tray
        texts = _top_level_texts(t._menu)
        assert "Tools" in texts
        assert "Developer" in texts


class TestSomethingWrongOpensSupportTab:
    def test_item_is_top_level(self, tray):
        t, app = tray
        assert "Something wrong?" in _top_level_texts(t._menu)

    def test_item_opens_settings_on_help_and_support(self, qapp, monkeypatch):
        from types import SimpleNamespace
        from samsara.ui import qt_runtime, settings_qt
        from tests.test_support_feedback_ui import _FeedbackApp

        monkeypatch.setattr(qt_runtime, "post", lambda cb: cb())
        settings_app = _FeedbackApp()
        window = settings_qt._SettingsWindow(settings_app)
        try:
            app = _make_app()
            app._settings_qt = SimpleNamespace(_window=window)
            t = SamsaraTrayQt(app)
            t._rebuild_menu()
            action = next(a for a in t._menu.actions() if a.text() == "Something wrong?")

            action.trigger()

            app.open_settings.assert_called_once_with()
            assert window._stack.currentIndex() == settings_qt._TAB_NAMES.index("Help & Support")
        finally:
            window.hide()
            window.deleteLater()


class TestQuickReferenceNoLongerBuriedInTools:
    def test_quick_reference_removed_from_tools_submenu(self, tray):
        t, app = tray
        tools = _submenu(t._menu, "Tools")
        assert tools is not None
        assert "Quick Reference" not in [a.text() for a in tools.actions()]


class TestToolsSubmenuContents:
    EXPECTED = {
        "Interactive Tutorial", "Mic Setup Guide", "Ava Guide",
        "Voice Training", "Benchmark Review", "Correct Last Dictation",
        "Stress Test Wizard", "Recalibrate Mic", "Cleanup",
    }

    def test_all_expected_tools_present(self, tray):
        t, app = tray
        tools = _submenu(t._menu, "Tools")
        texts = {a.text() for a in tools.actions()}
        missing = self.EXPECTED - texts
        assert not missing, f"missing from Tools: {missing}"

    def test_hotkey_and_model_info_present(self, tray):
        t, app = tray
        tools = _submenu(t._menu, "Tools")
        texts = [a.text() for a in tools.actions()]
        assert any(txt.startswith("Hotkey:") for txt in texts)
        assert any(txt.startswith("Model:") for txt in texts)

    def test_dev_only_items_not_in_tools(self, tray):
        """Dictation Diagnostics / Wake Word Debug / View Live Log /
        Calibrate Echo Cancellation / Open Config Folder / View Logs
        moved to Developer -- must not remain in Tools."""
        t, app = tray
        tools = _submenu(t._menu, "Tools")
        texts = {a.text() for a in tools.actions()}
        for dev_only in ("Dictation Diagnostics", "Wake Word Debug", "View Live Log",
                          "Calibrate Echo Cancellation", "Open Config Folder", "View Logs"):
            assert dev_only not in texts


class TestDeveloperSubmenuContents:
    EXPECTED = {
        "Dictation Diagnostics", "Wake Word Debug", "View Live Log",
        "Calibrate Echo Cancellation", "Open Config Folder", "View Logs",
    }

    def test_all_expected_dev_surfaces_present(self, tray):
        t, app = tray
        dev = _submenu(t._menu, "Developer")
        assert dev is not None
        texts = {a.text() for a in dev.actions()}
        missing = self.EXPECTED - texts
        assert not missing, f"missing from Developer: {missing}"

    def test_view_logs_submenu_has_both_logs(self, tray):
        t, app = tray
        dev = _submenu(t._menu, "Developer")
        view_logs = _submenu(dev, "View Logs")
        assert view_logs is not None
        texts = {a.text() for a in view_logs.actions()}
        assert texts == {"Main Log", "Voice Training Log"}


class TestAllPreviousActionsStillReachable:
    """Every action callback that existed before the reorg must still be
    present SOMEWHERE in the new structure (top level, Tools, or
    Developer) -- nothing became unreachable."""

    ALL_EXPECTED_LABELS = {
        "Show Samsara", "Settings", "History", "Quick Reference",
        "Command Reference", "Show Listening Indicator",
        "Interactive Tutorial", "Mic Setup Guide", "Ava Guide",
        "Voice Training", "Wake Word Debug", "Dictation Diagnostics",
        "Benchmark Review", "Correct Last Dictation", "View Live Log",
        "Stress Test Wizard", "Recalibrate Mic",
        "Calibrate Echo Cancellation", "Open Config Folder",
        "Main Log", "Voice Training Log", "Exit",
    }

    def _collect_all_texts(self, menu, acc):
        for a in menu.actions():
            if a.isSeparator():
                continue
            acc.add(a.text())
            if a.menu() is not None:
                self._collect_all_texts(a.menu(), acc)
        return acc

    def test_every_previous_label_present_somewhere(self, tray):
        t, app = tray
        all_texts = self._collect_all_texts(t._menu, set())
        missing = self.ALL_EXPECTED_LABELS - all_texts
        assert not missing, f"actions lost in reorg: {missing}"

    def test_cleanup_submenu_options_present(self, tray):
        t, app = tray
        tools = _submenu(t._menu, "Tools")
        cleanup = _submenu(tools, "Cleanup")
        assert cleanup is not None
        texts = {a.text() for a in cleanup.actions()}
        assert any("Clean" in txt for txt in texts)
        assert any("Verbatim" in txt for txt in texts)


class TestIconGeometryRefresh:
    """Windows can leave the tray icon's shell-registered screen geometry
    stale after a sleep/resume cycle or a monitor topology change, which
    makes the right-click context menu occasionally pop up at a wrong
    (sometimes primary-screen-center) position instead of anchored to the
    icon. _refresh_icon_registration() (hide+show) forces Windows to
    re-register it -- see _ICON_REFRESH_INTERVAL_MS."""

    def test_refresh_timer_is_running_at_the_documented_interval(self, tray):
        t, app = tray
        assert t._icon_refresh_timer.isActive()
        assert t._icon_refresh_timer.interval() == tray_qt._ICON_REFRESH_INTERVAL_MS

    def test_refresh_hides_and_reshows_the_icon_when_menu_closed(self, tray):
        t, app = tray
        hide = Mock()
        show = Mock()
        t._tray.hide = hide
        t._tray.show = show
        t._refresh_icon_registration()
        hide.assert_called_once()
        show.assert_called_once()

    def test_refresh_skipped_while_menu_is_open(self, tray, monkeypatch):
        """Must never yank an in-progress click/menu out from under the
        user -- if the menu is open, skip silently and let the next timer
        tick or topology-change event try again."""
        t, app = tray
        monkeypatch.setattr(t._menu, "isVisible", lambda: True)
        hide = Mock()
        show = Mock()
        t._tray.hide = hide
        t._tray.show = show
        t._refresh_icon_registration()
        hide.assert_not_called()
        show.assert_not_called()

    def test_refresh_failure_never_raises(self, tray):
        t, app = tray
        t._tray.hide = Mock(side_effect=RuntimeError("boom"))
        t._refresh_icon_registration()  # must not raise

    def test_screen_topology_change_schedules_a_debounced_refresh(self, tray, monkeypatch):
        t, app = tray
        scheduled = []
        monkeypatch.setattr(
            tray_qt.QTimer, "singleShot",
            staticmethod(lambda ms, cb: scheduled.append((ms, cb))),
        )
        t._on_screen_topology_changed()
        assert len(scheduled) == 1
        delay_ms, callback = scheduled[0]
        assert delay_ms == 2000
        assert callback == t._refresh_icon_registration

    def test_screen_added_removed_and_primary_changed_are_all_connected(self, tray, monkeypatch):
        """Confirms the handler actually fires via the real Qt signals, not
        just that it exists as a method. Other SamsaraTrayQt instances from
        other tests may still be connected to this same (session-scoped)
        QGuiApplication -- filter scheduled calls down to THIS instance's
        by identity rather than assuming an exact total count."""
        from PySide6.QtGui import QGuiApplication

        t, app = tray
        scheduled = []
        monkeypatch.setattr(
            tray_qt.QTimer, "singleShot",
            staticmethod(lambda ms, cb: scheduled.append((ms, cb))),
        )
        gui_app = QGuiApplication.instance()
        screen = gui_app.primaryScreen()
        gui_app.screenAdded.emit(screen)
        gui_app.screenRemoved.emit(screen)
        gui_app.primaryScreenChanged.emit(screen)

        this_instance_calls = [
            (delay, cb) for delay, cb in scheduled if cb == t._refresh_icon_registration
        ]
        assert len(this_instance_calls) == 3
        assert all(delay == 2000 for delay, _cb in this_instance_calls)

    def test_stop_stops_the_refresh_timer(self, tray):
        t, app = tray
        t.stop()
        assert not t._icon_refresh_timer.isActive()


class TestLoggingSelfCheckWarning:
    """2026-07-20 incident: logging can silently freeze forever (see
    dictation.py's _SafeRotatingFileHandler / _verify_logging_self_check).
    _poll_startup_health surfaces that as a one-time tray warning once the
    app is confirmed fully operational (_splash_progress == 100)."""

    def _ready_app(self):
        app = _make_app()
        app._splash_progress = 100
        return app

    def _quiet_update_check(self, monkeypatch):
        # Not under test here -- keep _poll_startup_health's unrelated
        # update-reconciliation branch inert so these tests only exercise
        # the new logging-warning branch.
        monkeypatch.setattr("samsara.updater.reconcile_update_on_startup", Mock(return_value=None))
        monkeypatch.setattr("samsara.ui.update_qt.maybe_start_automatic_update_check", Mock())

    def test_shows_tray_warning_when_self_check_failed(self, qapp, monkeypatch):
        self._quiet_update_check(monkeypatch)
        app = self._ready_app()
        app._logging_self_check_failed = True
        t = SamsaraTrayQt(app)
        t._tray.showMessage = Mock()

        t._poll_startup_health()

        titles = [c.args[0] for c in t._tray.showMessage.call_args_list]
        assert "Samsara logging warning" in titles

    def test_no_tray_warning_when_self_check_passed(self, qapp, monkeypatch):
        self._quiet_update_check(monkeypatch)
        app = self._ready_app()
        app._logging_self_check_failed = False
        t = SamsaraTrayQt(app)
        t._tray.showMessage = Mock()

        t._poll_startup_health()

        titles = [c.args[0] for c in t._tray.showMessage.call_args_list]
        assert "Samsara logging warning" not in titles

    def test_no_tray_warning_when_flag_absent(self, qapp, monkeypatch):
        # Every other Mock()-based app double in this file never sets this
        # attribute -- vars(...).get(..., False) must default safely, not
        # raise, so the rest of this test file stays unaffected.
        self._quiet_update_check(monkeypatch)
        app = self._ready_app()
        t = SamsaraTrayQt(app)
        t._tray.showMessage = Mock()

        t._poll_startup_health()

        titles = [c.args[0] for c in t._tray.showMessage.call_args_list]
        assert "Samsara logging warning" not in titles

    def test_warning_not_shown_before_startup_reaches_100_percent(self, qapp, monkeypatch):
        self._quiet_update_check(monkeypatch)
        app = _make_app()
        app._splash_progress = 60
        app._logging_self_check_failed = True
        t = SamsaraTrayQt(app)
        t._tray.showMessage = Mock()

        t._poll_startup_health()

        t._tray.showMessage.assert_not_called()
