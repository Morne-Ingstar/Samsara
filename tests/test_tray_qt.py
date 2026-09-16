"""Headless structure tests for the tray declutter pass (2026-07-10).

SamsaraTrayQt._rebuild_menu() is exercised directly against a real QMenu
(built via the session-scoped `qapp` fixture, no visible tray icon needed)
-- confirms every previously-existing action callback is still present
after the reorganization, and that the new grouping (top-level daily-use,
Tools submenu, Developer submenu) matches spec.
"""
import math
import types
from unittest.mock import Mock

import pytest

from samsara.ui import theme, tray_qt
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

    def test_history_present_in_workspace(self, tray):
        t, app = tray
        workspace = _submenu(t._menu, "Workspace")
        assert workspace is not None
        assert "History" in [action.text() for action in workspace.actions()]

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


class TestTrayDrawsTheMark:
    """09b-2: the tray icon is the shared mark (tray_qt.render_mark), rendered
    on the Qt thread from a MarkFrame; the procedural wheel is gone."""

    def test_apply_icon_renders_a_mark_frame_with_the_shared_routine(self, tray, monkeypatch):
        t, app = tray
        calls = []
        real = tray_qt.render_mark
        monkeypatch.setattr(tray_qt, "render_mark",
                            lambda *a, **k: calls.append((a, k)) or real(*a, **k))
        t._apply_icon(tray_qt.MarkFrame("recording", "armed", 30.0, 0.5))
        assert {a[2] for a, _k in calls} == set(tray_qt._TRAY_SIZES)
        assert all(a[:2] == ("recording", "armed") and a[3:] == (30.0, 0.5) for a, _k in calls)
        assert not t._tray.icon().isNull()

    @staticmethod
    def _app(**state):
        import dictation

        app = types.SimpleNamespace(
            recording=False, ava_mode_active=False, ava_command_session_active=False,
            command_mode_active=False, continuous_active=False, wake_word_active=False,
            snoozed=False, _tray_heard_eye=None, _icon_rotation=0.0,
        )
        for name, value in state.items():
            setattr(app, name, value)
        for name in ("_tray_mark", "create_icon_image", "_push_tray_icon", "_flash_tray_heard"):
            setattr(app, name, getattr(dictation.DictationApp, name).__get__(app))
        return app

    @pytest.mark.parametrize("state, live", [
        ("off",       {}),
        ("idle",      {}),
        ("asleep",    {"snoozed": True}),
        ("listening", {"command_mode_active": True}),
        ("recording", {"recording": True}),
        ("ava",       {"ava_mode_active": True}),
        ("armed",     {"wake_word_active": True}),
        ("heard",     {"wake_word_active": True, "_tray_heard_eye": "heard"}),
    ])
    def test_live_app_state_resolves_to_each_named_state(self, state, live):
        app = self._app(**live)
        assert app._tray_mark() == tray_qt.MARK_STATES[state]
        frame = app.create_icon_image()
        assert isinstance(frame, tray_qt.MarkFrame)
        assert (frame.capture, frame.eye) == tray_qt.MARK_STATES[state]

    def test_heard_flash_plays_the_keyframes_then_hands_back(self, monkeypatch):
        import dictation

        pushed = []
        app = self._app(wake_word_active=True)
        app._push_tray_icon = lambda: pushed.append(app._tray_mark()[1])
        monkeypatch.setattr(dictation.thread_registry, "timer",
                            lambda name, delay, fn, daemon=None: fn())
        app._flash_tray_heard()
        assert pushed == ["heard", "armed", "heard", "armed", "asleep", "armed"]


class TestTraySpinSpeed:
    """09b3: spin speed is a state channel on the existing chase timer."""

    def _tick_app(self, monkeypatch, reasons, recording):
        import math as _math
        import dictation

        frames = []
        app = TestTrayDrawsTheMark._app(recording=recording)
        app._icon_animating = True
        app._icon_anim_reasons = set(reasons)
        app._icon_chase_counter = 0
        app._icon_rotation = 1.0
        app._icon_chase_timer = None
        app.tray_icon = types.SimpleNamespace()
        app._icon_chase_tick = dictation.DictationApp._icon_chase_tick.__get__(app)
        app._stop_icon_chase = dictation.DictationApp._stop_icon_chase.__get__(app)
        app._push_tray_icon = lambda: frames.append(app._icon_rotation)
        monkeypatch.setattr(dictation.thread_registry, "timer", lambda *a, **k: None)
        return app, dictation, _math

    def test_transcribing_turns_once_per_0_9_seconds(self, monkeypatch):
        app, dictation, math = self._tick_app(monkeypatch, {"recording"}, recording=False)
        app._icon_chase_tick()
        step = 2 * math.pi * dictation.ICON_TICK_FAST / tray_qt.SPIN_SECONDS_PER_TURN["transcribing"]
        assert app._icon_rotation == pytest.approx(1.0 + step)

    def test_recording_turns_once_per_1_5_seconds(self, monkeypatch):
        """19: motion means capture -- recording spins (it was still in 09b3)."""
        app, dictation, math = self._tick_app(monkeypatch, {"recording"}, recording=True)
        app._icon_chase_tick()
        step = 2 * math.pi * dictation.ICON_TICK_FAST / tray_qt.SPIN_SECONDS_PER_TURN["recording"]
        assert tray_qt.SPIN_SECONDS_PER_TURN["recording"] == 1.5
        assert app._icon_rotation == pytest.approx(1.0 + step)

    @pytest.mark.parametrize("reason, tick", [("continuous", "ICON_TICK_MEDIUM"),
                                              ("wake_word", "ICON_TICK_SLOW")])
    def test_ambient_capture_turns_once_per_3_seconds(self, monkeypatch, reason, tick):
        app, dictation, math = self._tick_app(monkeypatch, {reason}, recording=False)
        app._icon_chase_tick()
        step = 2 * math.pi * getattr(dictation, tick) / 3.0
        assert app._icon_rotation == pytest.approx(1.0 + step)

    def test_tick_frames_carry_no_opacity_pulse(self, monkeypatch):
        seen = []
        for reasons, recording in (({"recording"}, True), ({"recording"}, False),
                                   ({"continuous"}, False), ({"wake_word"}, False)):
            app, _dictation, _math = self._tick_app(monkeypatch, reasons, recording)
            app.create_icon_image = lambda rotation=0.0, opacity=1.0: seen.append(opacity)
            for _ in range(5):
                app._icon_chase_tick()
        assert seen and set(seen) == {1.0}

    def test_stop_keeps_the_angle_no_aligned_rest(self, monkeypatch):
        app, _dictation, _math = self._tick_app(monkeypatch, {"recording"}, recording=False)
        app._icon_chase_tick()
        angle = app._icon_rotation
        app._stop_icon_chase()
        assert app._icon_rotation == angle != 0.0


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

    def test_inert_hotkey_and_model_info_are_not_menu_items(self, tray):
        t, app = tray
        tools = _submenu(t._menu, "Tools")
        texts = [a.text() for a in tools.actions()]
        assert not any(txt.startswith("Hotkey:") for txt in texts)
        assert not any(txt.startswith("Model:") for txt in texts)

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


# ---------------------------------------------------------------------------
# 61: a dead entry, a promise the app can't keep, and too many items
# ---------------------------------------------------------------------------

def _all_actions(menu, acc=None):
    acc = [] if acc is None else acc
    for a in menu.actions():
        if a.isSeparator():
            continue
        if a.menu() is not None:
            _all_actions(a.menu(), acc)
        else:
            acc.append(a)
    return acc


def _action(menu, text):
    return next(a for a in _all_actions(menu) if a.text() == text)


#: Info rows with nothing connected (they are disabled labels).
_INFO_PREFIXES = ("Hotkey:", "Model:", "Mouse hotkey disabled")

#: Every app method a tray action calls.
_HANDLERS = (
    "show_main_window", "reenable_mouse_hotkey", "release_mouse_buttons",
    "switch_microphone_and_refresh", "switch_mode_from_tray", "set_wake_word_enabled",
    "set_streaming_mode", "set_gesture_enabled", "snooze_listening", "resume_listening",
    "open_settings", "open_history", "open_quick_reference", "toggle_cheat_sheet",
    "toggle_listening_indicator", "enter_indicator_move_mode", "show_tutorial",
    "open_mic_setup_guide", "open_ava_guide", "open_voice_training", "open_benchmark_review",
    "open_correction_capture", "open_stress_test_wizard", "recalibrate_mic", "set_cleanup_mode",
    "open_dictation_diagnostics", "open_wake_word_debug", "open_log_viewer",
    "calibrate_echo_cancellation", "open_config_folder", "preview_first_run",
    "open_main_log", "open_voice_training_log", "quit_app",
)


@pytest.fixture
def slot_errors(monkeypatch):
    """Exceptions escaping a Qt slot go to sys.excepthook (PySide prints and
    swallows them) -- collect them so a test can assert there were none."""
    import sys
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))
    return errors


def _stub_tray(monkeypatch, app, *, raising=False):
    boom = RuntimeError("boom")
    started = Mock(side_effect=boom if raising else None)
    monkeypatch.setattr(tray_qt.os, "startfile", started, raising=False)
    monkeypatch.setattr(tray_qt, "open_support_tab", Mock(side_effect=boom if raising else None))
    monkeypatch.setattr(SamsaraTrayQt, "_open_update_dialog",
                        Mock(side_effect=boom if raising else None))
    if raising:
        for name in _HANDLERS:
            getattr(app, name).side_effect = boom
    t = SamsaraTrayQt(app)
    t._tray = Mock()
    t._rebuild_menu()
    return t, started


RAW_MEMOS = "Open the raw memo file"


class TestOpenMemos:
    """Queue 61's guarantees, kept, on the item that now does the opening.

    Queue 92 gave "Open memos" a real destination -- the memo list, which
    plays the audio and searches the transcripts -- so the raw markdown
    moved to its own item. The file is still created with the right header,
    a failure is still reported, and nothing fails silently.
    """

    def test_no_memo_file_creates_it_with_the_header_and_opens_it(self, qapp, tmp_path, monkeypatch, slot_errors):
        app = _make_app()
        memo = tmp_path / "never" / "memos.md"
        app.config["memo_file"] = str(memo)
        t, started = _stub_tray(monkeypatch, app)

        _action(t._menu, RAW_MEMOS).trigger()

        assert memo.read_text(encoding="utf-8") == "# Memos\n\n"
        started.assert_called_once_with(str(memo))
        t._tray.showMessage.assert_not_called()
        assert slot_errors == []

    def test_existing_memo_file_is_opened_untouched(self, qapp, tmp_path, monkeypatch, slot_errors):
        app = _make_app()
        memo = tmp_path / "memos.md"
        memo.write_text("# Memos\n\n## 2026-09-14 10:00\nbuy milk\n\n", encoding="utf-8")
        app.config["memo_file"] = str(memo)
        t, started = _stub_tray(monkeypatch, app)

        _action(t._menu, RAW_MEMOS).trigger()

        assert "buy milk" in memo.read_text(encoding="utf-8")
        started.assert_called_once_with(str(memo))
        assert slot_errors == []

    def test_open_failure_is_told_to_the_user_and_never_raises(self, qapp, tmp_path, monkeypatch, slot_errors):
        app = _make_app()
        app.config["memo_file"] = str(tmp_path / "memos.md")
        t, _started = _stub_tray(monkeypatch, app, raising=True)

        _action(t._menu, RAW_MEMOS).trigger()

        assert slot_errors == []
        title, message = t._tray.showMessage.call_args[0][:2]
        assert RAW_MEMOS in title and "boom" in message

    def test_memo_header_matches_quick_memo(self, qapp, tmp_path, monkeypatch):
        """A memo recorded after the tray created the file gets no second header."""
        from samsara.quick_memo import append_memo
        app = _make_app()
        memo = tmp_path / "memos.md"
        app.config["memo_file"] = str(memo)
        t, _started = _stub_tray(monkeypatch, app)
        _action(t._menu, RAW_MEMOS).trigger()
        append_memo("first", "voice", home=str(memo))
        assert memo.read_text(encoding="utf-8").count("# Memos") == 1

    def test_open_memos_goes_to_the_memo_list_not_notepad(self, qapp, tmp_path, monkeypatch, slot_errors):
        """Queue 92: the tray item points at the UI."""
        from samsara.ui.home_qt import MEMOS
        app = _make_app()
        memo = tmp_path / "memos.md"
        app.config["memo_file"] = str(memo)
        app.open_hub_page = Mock(return_value=True)
        t, started = _stub_tray(monkeypatch, app)

        _action(t._menu, "Open memos").trigger()

        app.open_hub_page.assert_called_once_with(MEMOS)
        started.assert_not_called()          # no Notepad
        assert not memo.exists()             # and no file created behind it
        assert slot_errors == []

    def test_open_memos_falls_back_to_the_file_when_the_hub_cannot_take_it(
            self, qapp, tmp_path, monkeypatch, slot_errors):
        """A hub that is not up must not make the tray item do nothing."""
        app = _make_app()
        memo = tmp_path / "memos.md"
        app.config["memo_file"] = str(memo)
        app.open_hub_page = Mock(return_value=False)
        t, started = _stub_tray(monkeypatch, app)

        _action(t._menu, "Open memos").trigger()

        started.assert_called_once_with(str(memo))
        assert memo.read_text(encoding="utf-8") == "# Memos\n\n"
        assert slot_errors == []


class TestNoTrayActionFailsSilently:
    def _triggerable(self, t):
        acts = [a for a in _all_actions(t._menu) if not a.text().startswith(_INFO_PREFIXES)]
        for a in acts:
            a.setEnabled(True)   # e.g. "Resume now" is disabled while not snoozed
        return acts

    def test_every_action_with_a_stubbed_handler_runs_without_raising(self, qapp, monkeypatch, slot_errors):
        app = _make_app()
        app.config["updates"] = {"tray_menu_entry": True}
        t, _started = _stub_tray(monkeypatch, app)
        acts = self._triggerable(t)
        assert len(acts) > 40
        for a in acts:
            a.trigger()
        assert slot_errors == []
        t._tray.showMessage.assert_not_called()
        app.quit_app.assert_called_once_with()
        app.open_history.assert_called_once_with()

    def test_every_action_whose_handler_raises_tells_the_user(self, qapp, monkeypatch, slot_errors):
        app = _make_app()
        app.config["updates"] = {"tray_menu_entry": True}
        t, _started = _stub_tray(monkeypatch, app, raising=True)
        for a in self._triggerable(t):
            if a.actionGroup() is not None and a.isChecked():
                continue   # re-selecting the current mode / cleanup is a deliberate no-op
            before = t._tray.showMessage.call_count
            a.trigger()
            assert t._tray.showMessage.call_count == before + 1, a.text()
        assert slot_errors == []

    def test_a_guide_that_was_never_built_says_so(self, qapp, monkeypatch, slot_errors):
        app = _make_app()
        app.mic_setup_wizard = None
        t, _started = _stub_tray(monkeypatch, app)
        _action(t._menu, "Mic Setup Guide").trigger()
        app.open_mic_setup_guide.assert_not_called()
        assert "isn't available" in t._tray.showMessage.call_args[0][1]
        assert slot_errors == []


class TestUpdateEntryIsBehindAFlag:
    @staticmethod
    def _texts(t):
        return [a.text() for a in _all_actions(t._menu)]

    @pytest.mark.parametrize("updates", [None, {}, {"tray_menu_entry": False}, {"tray_menu_entry": "yes"}])
    def test_absent_when_the_flag_is_off(self, qapp, monkeypatch, updates):
        app = _make_app()
        if updates is not None:
            app.config["updates"] = updates
        t, _started = _stub_tray(monkeypatch, app)
        assert not [x for x in self._texts(t) if "Update" in x or "Install Samsara" in x]
        t._available_update = types.SimpleNamespace(version="9.9.9")
        t._rebuild_menu()
        assert not [x for x in self._texts(t) if "Update" in x or "Install Samsara" in x]

    def test_present_in_tools_with_the_updater_capability_label(self, qapp, monkeypatch):
        from samsara.updater import update_check_menu_label

        app = _make_app()
        app.config["updates"] = {"tray_menu_entry": True}
        t, _started = _stub_tray(monkeypatch, app)
        tools = _submenu(t._menu, "Tools")
        label = update_check_menu_label()
        assert label in [a.text() for a in tools.actions()]
        _action(t._menu, label).trigger()
        SamsaraTrayQt._open_update_dialog.assert_called_once()

    def test_a_found_update_is_offered_top_level_when_the_flag_is_on(self, qapp, monkeypatch):
        app = _make_app()
        app.config["updates"] = {"tray_menu_entry": True}
        t, _started = _stub_tray(monkeypatch, app)
        t._available_update = types.SimpleNamespace(version="9.9.9")
        t._rebuild_menu()
        assert "Install Samsara v9.9.9…" in _top_level_texts(t._menu)
        assert "Check for Updates…" not in self._texts(t)


class TestMenuGrouping61:
    TOP = ["Show Samsara", "Snooze", "Wake Word  (samsara)", "[MIC]  Test Microphone",
           "Mode:  Hold", "Quick Reference", "Settings", "Something wrong?",
           "Workspace", "Tools", "Developer", "Exit"]

    def test_top_level_is_the_daily_set(self, qapp, monkeypatch):
        app = _make_app()
        app._mouse_hook = None
        t, _started = _stub_tray(monkeypatch, app)
        texts = [a.text() for a in t._menu.actions() if not a.isSeparator()]
        assert texts == self.TOP

    def test_setup_toggles_and_overlays_moved_to_tools(self, qapp, monkeypatch):
        app = _make_app()
        t, _started = _stub_tray(monkeypatch, app)
        tools = {a.text() for a in _submenu(t._menu, "Tools").actions()}
        for label in ("Streaming Mode  (CapsLock)", "Gesture Lane  (webcam)", "Command Reference",
                      "Show Listening Indicator", "Move listening indicator..."):
            assert label in tools
            assert label not in _top_level_texts(t._menu)

    def test_everyday_records_are_in_workspace_not_the_recovery_top_level(self, qapp, monkeypatch):
        app = _make_app()
        t, _started = _stub_tray(monkeypatch, app)
        workspace = _submenu(t._menu, "Workspace")
        assert workspace is not None
        assert {"History", "Open memos", RAW_MEMOS} <= {a.text() for a in workspace.actions()}
        assert not {"History", "Open memos", RAW_MEMOS} & set(_top_level_texts(t._menu))

    def test_toggles_keep_their_checked_state_and_pass_it_on(self, qapp, monkeypatch):
        app = _make_app()
        app.config["streaming_mode"] = True
        t, _started = _stub_tray(monkeypatch, app)
        act = _action(t._menu, "Streaming Mode  (CapsLock)")
        assert act.isCheckable() and act.isChecked()
        act.trigger()
        app.set_streaming_mode.assert_called_once_with(False)
        wake = _action(t._menu, "Wake Word  (samsara)")
        assert not wake.isChecked()
        wake.trigger()
        app.set_wake_word_enabled.assert_called_once_with(True)
        _action(t._menu, "Toggle (click to start/stop)").trigger()
        app.switch_mode_from_tray.assert_called_once_with("toggle")


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


# ---------------------------------------------------------------------------
# 42: recording spins, the band-weight nose reads as a head, the gap stays open
# ---------------------------------------------------------------------------

def _load_gen_icons():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "tools" / "gen_icons.py"
    spec = importlib.util.spec_from_file_location("gen_icons_42", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: sha256 of the three ring_segment_path_data strings at each weight, as
#: committed before 42 (c0a1705). 42 changed only the band weight.
_PATHS_BEFORE_42 = {
    "brand": "5c3748c3b29e4f1ac95fc634bf17c1b60c39d544719580d905537b27faea1339",
    "hollow": "cbe9459344676495faee3d45ff3ab419a94bbecae76ef5c9109319d5be2905e4",
}


def _paths_digest(weight):
    import hashlib

    joined = "\n".join(tray_qt.ring_segment_path_data(i, weight) for i in range(3))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _alpha_bilinear(image, x, y):
    x0, y0 = int(math.floor(x - 0.5)), int(math.floor(y - 0.5))
    fx, fy = x - 0.5 - x0, y - 0.5 - y0
    total = 0.0
    for dx, wx in ((0, 1 - fx), (1, fx)):
        for dy, wy in ((0, 1 - fy), (1, fy)):
            px, py = x0 + dx, y0 + dy
            inside = 0 <= px < image.width() and 0 <= py < image.height()
            total += (image.pixelColor(px, py).alpha() if inside else 0) * wx * wy
    return total


def _gap_alphas(image, size, rotation):
    """Rendered alpha along the ring centreline across the 12 o'clock gap
    (head end -96 deg to tail start -84 deg), turned with the ring."""
    k = size / 64.0
    alphas = []
    for i in range(241):
        a = math.radians(-96.0 + 12.0 * i / 240 + rotation)
        alphas.append(_alpha_bilinear(image,
                                      size / 2 + tray_qt.RING_RADIUS * k * math.cos(a),
                                      size / 2 + tray_qt.RING_RADIUS * k * math.sin(a)))
    return alphas



class TestRecordingSpins:
    def test_recording_motion_entry_is_non_zero(self):
        assert tray_qt.SPIN_SECONDS_PER_TURN["recording"] == pytest.approx(1.5)
        assert tray_qt.SPIN_SECONDS_PER_TURN == {
            "armed": 3.0, "listening": 3.0, "recording": 1.5, "thinking": 2.4, "transcribing": 0.9}

    def test_recording_colour_and_fill_are_unchanged(self):
        assert tray_qt.mark_capture()["recording"] == (theme.RECORDING, "ring-filled")
        assert tray_qt.brand_capture()["recording"] == (theme.RECORDING, "ring-filled")

    def test_recording_frames_differ_with_rotation(self, qapp):
        a = tray_qt.render_mark("recording", "off", 44, rotation=0.0, brand=True)
        b = tray_qt.render_mark("recording", "off", 44, rotation=45.0, brand=True)
        assert a != b


class TestBandNoseReadsAsAHead:
    def test_band_nose_is_long_relative_to_the_head(self):
        half, length, points = tray_qt.nose_metrics(tray_qt.RING_BAND_WIDTH)
        assert tray_qt.NOSE_MIN_RATIO >= 0.9
        assert length / half >= tray_qt.NOSE_MIN_RATIO
        assert half == pytest.approx(8.80)          # the width is not what changed
        assert points >= 24                         # smooth at 128 px
        # The pre-42 band nose (1.29 units on a head 8.80 half-wide, 3
        # samples) is exactly what the floor rejects.
        assert 1.29 / 8.80 < tray_qt.NOSE_MIN_RATIO

    def test_nose_lead_is_a_function_of_weight(self):
        assert tray_qt.nose_lead(tray_qt.RING_LINE_WIDTH) == 0.0
        assert tray_qt.nose_lead(tray_qt.RING_BRAND_WIDTH) == 0.0
        assert tray_qt.nose_lead(tray_qt.RING_BAND_WIDTH) == tray_qt.BAND_NOSE_LEAD > 0.0

    def test_band_nose_closes_as_its_own_round_front(self):
        """No flat face: the width falls continuously from the widest point
        to zero at the stroke end, and the front sits where the old nose
        ended (nose fraction 0.25), so the gap is no narrower."""
        band = tray_qt.RING_BAND_WIDTH
        head = tray_qt.HEAD_SEGMENT
        peak, end = tray_qt._nose_span(band)
        over = tray_qt.HEAD_OVERSHOOT_DEG / tray_qt.SEGMENT_SPAN_DEG
        assert end == pytest.approx(1.0 + 0.25 * over)
        assert tray_qt.stroke_scale(head, peak, band) == pytest.approx(tray_qt.head_scale(band))
        widths = [tray_qt.stroke_scale(head, peak + (end - peak) * i / 50, band) for i in range(51)]
        assert all(a >= b for a, b in zip(widths, widths[1:]))
        assert widths[-1] == pytest.approx(0.0, abs=1e-6)
        assert tray_qt.stroke_scale(head, end + 0.01, band) == 0.0

    def test_band_centreline_is_the_same_circle(self):
        band = tray_qt.RING_BAND_WIDTH
        for _u, _a, x, y in tray_qt.ring_centreline(tray_qt.HEAD_SEGMENT, band):
            assert math.hypot(x - tray_qt.RING_CENTRE, y - tray_qt.RING_CENTRE) == pytest.approx(
                tray_qt.RING_RADIUS, abs=1e-9)

    def test_brand_and_hollow_geometry_unchanged_from_the_previous_commit(self):
        assert tray_qt.weight_profile(tray_qt.RING_BRAND_WIDTH) == tray_qt.BRAND_PROFILE
        assert _paths_digest(tray_qt.RING_BRAND_WIDTH) == _PATHS_BEFORE_42["brand"]
        assert _paths_digest(tray_qt.RING_LINE_WIDTH) == _PATHS_BEFORE_42["hollow"]
        half, length, points = tray_qt.nose_metrics(tray_qt.RING_BRAND_WIDTH)
        assert (round(half, 2), round(length, 2), points) == (4.97, 3.94, 9)

    def test_gen_icons_check_passes(self, qapp):
        gen_icons = _load_gen_icons()
        assert gen_icons.stale_assets() == []
        assert gen_icons.main(["--check"]) == 0

    def test_recording_spin_sheet_renders(self, qapp, tmp_path):
        gen_icons = _load_gen_icons()
        out = gen_icons.write_recording_spin(tmp_path / "recording_spin.png")
        assert out.exists() and out.stat().st_size > 0
        assert gen_icons.RECORDING_SPIN_ANGLES == (0, 45, 90, 135, 180, 225, 270, 315)
        assert gen_icons.RECORDING_SPIN_SIZES == (26, 44, 60, 128)


class TestTwelveOClockGapStaysOpen:
    """Measured on the rendered band-weight mark at every spin-sheet angle:
    somewhere across the gap the centreline is mostly clear (at 26-44 px
    antialiasing never reaches zero), and at 128 px a clear run of pixels."""

    @pytest.mark.parametrize("size, max_alpha", [(26, 90), (34, 90), (44, 90)])
    def test_small_sizes_dip_well_below_the_band(self, qapp, size, max_alpha):
        for rotation in (0, 45, 90, 135, 180, 225, 270, 315):
            image = tray_qt.render_mark("recording", "off", size, rotation=float(rotation), brand=True)
            assert min(_gap_alphas(image, size, rotation)) <= max_alpha, (size, rotation)

    def test_128px_has_a_clear_run(self, qapp):
        size = 128
        arc_px = tray_qt.RING_RADIUS * (size / 64.0) * math.radians(12.0) / 240
        for rotation in (0, 45, 90, 135, 180, 225, 270, 315):
            image = tray_qt.render_mark("recording", "off", size, rotation=float(rotation), brand=True)
            alphas = _gap_alphas(image, size, rotation)
            assert min(alphas) == 0, rotation
            assert sum(arc_px for a in alphas if a < 96) >= 3.5, rotation
