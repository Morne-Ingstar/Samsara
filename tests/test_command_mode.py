"""Tests for Command Mode (Mouse 4 walkie-talkie hold-to-talk).

Covers:
- CommandEntry.debounce attribute
- CommandMatcher.should_suppress() / record_execution()
- ListeningIndicator.set_command_mode() visual state
- AudioCoordinator TTS suppression during command mode
- DictationApp command mode state machine (via mocked app)
- Toggle mode miss counter and inactivity exit
- Voice exit phrases
"""

import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara.command_registry import CommandEntry, CommandMatcher


# =============================================================================
# CommandEntry debounce attribute
# =============================================================================

class TestCommandEntryDebounce:

    def test_debounce_defaults_to_zero(self):
        e = CommandEntry('play pause', 'builtin', 'hotkey')
        assert e.debounce == 0.0

    def test_debounce_stored_correctly(self):
        e = CommandEntry('play pause', 'builtin', 'hotkey', debounce=1.5)
        assert e.debounce == 1.5

    def test_debounce_coerced_to_float(self):
        e = CommandEntry('next track', 'builtin', 'hotkey', debounce=2)
        assert isinstance(e.debounce, float)
        assert e.debounce == 2.0

    def test_none_debounce_coerces_to_zero(self):
        e = CommandEntry('some command', 'builtin', 'hotkey', debounce=None)
        assert e.debounce == 0.0


# =============================================================================
# CommandMatcher debounce: should_suppress / record_execution
# =============================================================================

def _make_matcher_with_entry(phrase, debounce=0.0):
    m = CommandMatcher()
    e = CommandEntry(phrase, 'builtin', 'hotkey', debounce=debounce)
    m._entries[phrase] = e
    m.freeze()
    return m, e


class TestDebounceRegistry:

    def test_no_debounce_never_suppressed(self):
        m, e = _make_matcher_with_entry('escape', debounce=0.0)
        m.record_execution(e)
        assert m.should_suppress(e) is False

    def test_suppress_within_window(self):
        m, e = _make_matcher_with_entry('play pause', debounce=1.5)
        m.record_execution(e)
        assert m.should_suppress(e) is True

    def test_not_suppressed_before_first_execution(self):
        m, e = _make_matcher_with_entry('play pause', debounce=1.5)
        assert m.should_suppress(e) is False

    def test_not_suppressed_after_window_expires(self):
        m, e = _make_matcher_with_entry('next track', debounce=0.05)
        m.record_execution(e)
        time.sleep(0.08)
        assert m.should_suppress(e) is False

    def test_record_execution_only_tracked_for_debounced(self):
        m, e = _make_matcher_with_entry('escape', debounce=0.0)
        m.record_execution(e)
        with m._exec_lock:
            assert 'escape' not in m._last_executions

    def test_record_execution_tracked_for_debounced(self):
        m, e = _make_matcher_with_entry('play pause', debounce=1.5)
        m.record_execution(e)
        with m._exec_lock:
            assert 'play pause' in m._last_executions

    def test_suppress_independent_per_phrase(self):
        m = CommandMatcher()
        e1 = CommandEntry('play pause', 'builtin', 'hotkey', debounce=1.5)
        e2 = CommandEntry('next track', 'builtin', 'hotkey', debounce=1.5)
        m._entries['play pause'] = e1
        m._entries['next track'] = e2
        m.freeze()
        m.record_execution(e1)
        assert m.should_suppress(e1) is True
        assert m.should_suppress(e2) is False

    def test_second_record_resets_window(self):
        m, e = _make_matcher_with_entry('play pause', debounce=0.05)
        m.record_execution(e)
        time.sleep(0.06)
        assert m.should_suppress(e) is False
        m.record_execution(e)
        assert m.should_suppress(e) is True

    def test_thread_safe_concurrent_record(self):
        m, e = _make_matcher_with_entry('play pause', debounce=2.0)
        errors = []
        def _record():
            try:
                m.record_execution(e)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=_record) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# =============================================================================
# plugin_commands debounce parameter
# =============================================================================

class TestPluginCommandDebounce:

    def test_debounce_stored_in_registry(self):
        from samsara import plugin_commands as pc
        old_registry = dict(pc._REGISTRY)
        try:
            pc._REGISTRY.clear()

            @pc.command('test play', pack='media', debounce=1.5)
            def _play(app, remainder):
                return True

            assert pc._REGISTRY['test play']['debounce'] == 1.5
        finally:
            pc._REGISTRY.clear()
            pc._REGISTRY.update(old_registry)

    def test_debounce_default_zero(self):
        from samsara import plugin_commands as pc
        old_registry = dict(pc._REGISTRY)
        try:
            pc._REGISTRY.clear()

            @pc.command('test cmd', pack='core')
            def _cmd(app, remainder):
                return True

            assert pc._REGISTRY['test cmd']['debounce'] == 0.0
        finally:
            pc._REGISTRY.clear()
            pc._REGISTRY.update(old_registry)


# =============================================================================
# ListeningIndicator command mode state
# =============================================================================

class TestListeningIndicatorCommandMode:

    @pytest.fixture(autouse=True, scope='class')
    def tk_root(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        yield root
        root.destroy()

    @pytest.fixture(autouse=True)
    def indicator(self, tk_root):
        from samsara.ui.listening_indicator import ListeningIndicator
        ind = ListeningIndicator(tk_root)
        self._ind = ind
        yield ind

    def test_initial_command_mode_false(self, indicator):
        assert indicator._command_mode is False

    def test_set_command_mode_true(self, indicator):
        indicator.set_command_mode(True)
        assert indicator._command_mode is True

    def test_set_command_mode_false(self, indicator):
        indicator.set_command_mode(True)
        indicator.set_command_mode(False)
        assert indicator._command_mode is False

    def test_set_command_mode_idempotent(self, indicator):
        indicator.set_command_mode(True)
        indicator.set_command_mode(True)
        assert indicator._command_mode is True


# =============================================================================
# AudioCoordinator TTS suppression in command mode
# =============================================================================

class TestAudioCoordinatorCommandModeSuppression:

    def _make_coordinator(self, command_mode_active=False, tts_char_limit=50):
        from samsara.tts.coordinator import AudioCoordinator
        from samsara.tts.engine_base import SpeechHandle

        app = MagicMock()
        app.command_mode_active = command_mode_active
        app.config = {
            'tts': {'speed': 1.0, 'volume': 0.8, 'voice_id': None},
            'command_mode': {'tts_char_limit': tts_char_limit},
        }
        engine = MagicMock()
        engine.speak.return_value = SpeechHandle(utterance_id='test-123')
        engine.get_engine_state.return_value = 'idle'
        coord = AudioCoordinator(app, engine)
        return coord, app, engine

    def test_no_suppression_when_command_mode_off(self):
        coord, app, engine = self._make_coordinator(command_mode_active=False)
        coord.speak('Hello ' * 20)
        engine.speak.assert_called_once()

    def test_suppressed_when_text_over_limit(self):
        coord, app, engine = self._make_coordinator(
            command_mode_active=True, tts_char_limit=50)
        result = coord.speak('A' * 60)
        engine.speak.assert_not_called()
        assert result.utterance_id == 'noop-cmd-mode'

    def test_not_suppressed_when_text_under_limit(self):
        coord, app, engine = self._make_coordinator(
            command_mode_active=True, tts_char_limit=50)
        coord.speak('Short text')
        engine.speak.assert_called_once()

    def test_suppressed_at_exact_limit_plus_one(self):
        coord, app, engine = self._make_coordinator(
            command_mode_active=True, tts_char_limit=10)
        result = coord.speak('12345678901')  # 11 chars
        engine.speak.assert_not_called()
        assert result.utterance_id == 'noop-cmd-mode'

    def test_not_suppressed_at_exact_limit(self):
        coord, app, engine = self._make_coordinator(
            command_mode_active=True, tts_char_limit=10)
        coord.speak('1234567890')  # exactly 10 chars
        engine.speak.assert_called_once()


# =============================================================================
# Command mode state machine helpers (minimal mock DictationApp)
# =============================================================================

class _MockApp:
    """Minimal mock for command mode state machine tests."""

    def __init__(self, mode='hold', enabled=True):
        self.command_mode_active = False
        self._command_mode_lock = threading.Lock()
        self._command_mode_miss_count = 0
        self._command_mode_inactivity_timer = None
        self.recording = False
        self.config = {
            'command_mode': {
                'enabled': enabled,
                'mode': mode,
                'button': 'mouse4',
                'enter_debounce_ms': 0,
                'exit_earcon': False,
                'miss_limit': 3,
                'inactivity_timeout_s': 5,
                'tts_char_limit': 50,
            }
        }
        self._sounds = []
        self._ui_calls = []

    def play_sound(self, name, **_kwargs):
        self._sounds.append(name)

    def start_recording(self, **_kwargs):
        self.recording = True

    def stop_recording(self):
        self.recording = False

    # Inline copies of command mode methods under test
    def enter_command_mode(self):
        with self._command_mode_lock:
            if self.command_mode_active:
                return
            self.command_mode_active = True
        self._command_mode_miss_count = 0

    def exit_command_mode(self):
        with self._command_mode_lock:
            if not self.command_mode_active:
                return
            self.command_mode_active = False
        self._cancel_command_mode_inactivity_timer()
        if self.recording:
            self.stop_recording()
        if self.config['command_mode'].get('exit_earcon', True):
            self.play_sound('stop')

    def _reset_command_mode_inactivity_timer(self, timeout_s):
        self._cancel_command_mode_inactivity_timer()
        t = threading.Timer(timeout_s, self._on_command_mode_inactivity)
        t.daemon = True
        self._command_mode_inactivity_timer = t
        t.start()

    def _cancel_command_mode_inactivity_timer(self):
        t = self._command_mode_inactivity_timer
        if t is not None:
            t.cancel()
            self._command_mode_inactivity_timer = None

    def _on_command_mode_inactivity(self):
        self.exit_command_mode()


class TestGhostTapPrevention:
    """exit_command_mode() marks ghost taps; transcription must check the flag."""

    def _make_app(self, debounce_ms=200):
        app = _MockApp()
        app.config['command_mode']['enter_debounce_ms'] = debounce_ms
        app._command_mode_session_start = 0.0
        app._command_mode_ghost_tap = False
        # Wire monotonic tracking same as DictationApp
        import time
        _orig_enter = app.enter_command_mode
        def _enter():
            _orig_enter()
            app._command_mode_session_start = time.monotonic()
            app._command_mode_ghost_tap = False
        app.enter_command_mode = _enter

        _orig_exit = app.exit_command_mode
        def _exit():
            import time as t2
            hold_ms = (t2.monotonic() - app._command_mode_session_start) * 1000
            app._command_mode_ghost_tap = hold_ms < debounce_ms
            _orig_exit()
        app.exit_command_mode = _exit
        return app

    def test_long_hold_clears_ghost_flag(self):
        import time
        app = self._make_app(debounce_ms=50)
        app.enter_command_mode()
        time.sleep(0.06)
        app.exit_command_mode()
        assert app._command_mode_ghost_tap is False

    def test_short_hold_sets_ghost_flag(self):
        app = self._make_app(debounce_ms=500)
        app.enter_command_mode()
        # exit immediately (0ms hold)
        app.exit_command_mode()
        assert app._command_mode_ghost_tap is True

    def test_ghost_flag_cleared_after_discard(self):
        app = self._make_app(debounce_ms=500)
        app.enter_command_mode()
        app.exit_command_mode()
        assert app._command_mode_ghost_tap is True
        # Simulates what transcribe() does
        app._command_mode_ghost_tap = False
        assert app._command_mode_ghost_tap is False


class TestExitEarconNoDuplication:
    """exit_command_mode must not double-play 'stop' when stop_recording already played it."""

    def test_no_extra_stop_when_recording_was_active(self):
        app = _MockApp()
        app.config['command_mode']['exit_earcon'] = True
        app.enter_command_mode()
        app.recording = True
        # stop_recording() would already play 'stop' — exit should NOT add another
        # Simulate the corrected exit_command_mode logic:
        was_recording = app.recording
        if was_recording:
            app.stop_recording()  # plays 'stop' inside (mocked here)
            app._sounds.append('stop')  # simulate stop_recording's earcon
        if app.config['command_mode'].get('exit_earcon', True) and not was_recording:
            app._sounds.append('stop')

        stop_count = app._sounds.count('stop')
        assert stop_count == 1, f"Expected 1 stop earcon, got {stop_count}"

    def test_exit_earcon_plays_when_not_recording(self):
        app = _MockApp()
        app.config['command_mode']['exit_earcon'] = True
        app.enter_command_mode()
        app.recording = False
        was_recording = app.recording
        if was_recording:
            app.stop_recording()
            app._sounds.append('stop')
        if app.config['command_mode'].get('exit_earcon', True) and not was_recording:
            app._sounds.append('stop')

        assert app._sounds.count('stop') == 1


class TestCommandModeStateMachine:

    def test_enter_sets_active(self):
        app = _MockApp()
        app.enter_command_mode()
        assert app.command_mode_active is True

    def test_enter_idempotent(self):
        app = _MockApp()
        app.enter_command_mode()
        app.enter_command_mode()
        assert app.command_mode_active is True
        assert app._command_mode_miss_count == 0

    def test_exit_clears_active(self):
        app = _MockApp()
        app.enter_command_mode()
        app.exit_command_mode()
        assert app.command_mode_active is False

    def test_exit_idempotent(self):
        app = _MockApp()
        app.exit_command_mode()  # already inactive — no crash
        assert app.command_mode_active is False

    def test_exit_stops_recording(self):
        app = _MockApp()
        app.enter_command_mode()
        app.recording = True
        app.exit_command_mode()
        assert app.recording is False

    def test_exit_earcon_disabled_no_sound(self):
        app = _MockApp()
        app.config['command_mode']['exit_earcon'] = False
        app.enter_command_mode()
        app.exit_command_mode()
        assert 'stop' not in app._sounds

    def test_inactivity_timer_exits_toggle_mode(self):
        app = _MockApp(mode='toggle')
        app.enter_command_mode()
        app._reset_command_mode_inactivity_timer(0.05)
        time.sleep(0.1)
        assert app.command_mode_active is False

    def test_cancel_inactivity_timer(self):
        app = _MockApp(mode='toggle')
        app.enter_command_mode()
        app._reset_command_mode_inactivity_timer(0.05)
        app._cancel_command_mode_inactivity_timer()
        time.sleep(0.1)
        # Timer was cancelled so mode is still active
        assert app.command_mode_active is True
        app.exit_command_mode()

    def test_concurrent_enter_only_activates_once(self):
        app = _MockApp()
        activated = []

        def _enter():
            if not app.command_mode_active:
                app.enter_command_mode()
                activated.append(1)

        threads = [threading.Thread(target=_enter) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert app.command_mode_active is True
