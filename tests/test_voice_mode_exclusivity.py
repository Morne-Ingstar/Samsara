"""One live voice owner, including the unified toggle session's Ava lane.

The independent Ava-command latch was retired in f44ea3c9. Ava is now a
SessionMode within the toggle session; hold-to-talk Ava and command capture
refuse competing entry. Preserve the incident guarantees against that live
contract: no overlapping capture, no routing after exit, normal exit feedback,
and no resurrection by a delayed entry worker or a subsequent ghost tap.

Historical test IDs remain so the commit gate can compare the same inventory.
The tests bind real DictationApp methods and use a real SessionModeManager;
only audio, timers, the clock and background worker launch are synthetic.
"""
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from plugins.commands import ask_ollama
from samsara import ava_command_session, execution_policy
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals


_SIGNALS = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


@pytest.fixture(autouse=True)
def _reset_ava_command_session_module_state(monkeypatch):
    # Public cancellation functions operate on a private per-test set. Restore
    # incoming state instead of clearing another test's cancellation tokens.
    monkeypatch.setattr(ava_command_session, '_CANCELLED', set())
    # Real session exit calls stop_all. Give its plugin cleanup private state
    # too, so it cannot reject another test's pending action or stop its workers.
    monkeypatch.setattr(ask_ollama, '_pending_action', None)
    monkeypatch.setattr(ask_ollama, '_scheduled_task', None)
    monkeypatch.setattr(ask_ollama, '_scheduler_thread', None)
    monkeypatch.setattr(ask_ollama, '_live_schedules', [])


def _make_app(monkeypatch, command_mode='toggle'):
    app = types.SimpleNamespace(
        command_mode_active=False,
        ava_mode_active=False,
        recording=False,
        _command_mode_lock=threading.Lock(),
        _ava_mode_lock=threading.Lock(),
        _ava_cmd_mode_lock=threading.Lock(),
        _ava_cmd_generation=0,
        _wake_consumer_reasons=set(),
        _wake_consumer_lock=threading.Lock(),
        wake_word_triggered=False,
        config={
            'command_mode': {
                'mode': command_mode,
                'enter_debounce_ms': 200,
                'exit_earcon': True,
                'inactivity_timeout_s': 300,
            },
        },
    )
    app.play_sound = Mock()
    app.start_recording = Mock(side_effect=lambda **kw: setattr(app, 'recording', True))
    app.stop_recording = Mock(side_effect=lambda: setattr(app, 'recording', False))
    app.process_wake_word_buffer = Mock()
    app._reset_command_mode_inactivity_timer = Mock()
    app._cancel_command_mode_inactivity_timer = Mock()
    app._update_mode_overlay = Mock()
    app._update_streaming_preview = Mock()
    app._release_streaming_preview = Mock()
    app._show_outcome_chip = Mock()
    app._clock = types.SimpleNamespace(monotonic=Mock(return_value=100.0), sleep=Mock())
    monkeypatch.setattr(dictation, 'time', app._clock)
    # Entry state changes are synchronous. Run selected real workers explicitly
    # below; never launch capture, playback or a provider request in this suite.
    monkeypatch.setattr(dictation.thread_registry, 'spawn', Mock())

    consumer = types.SimpleNamespace(_running=False)
    consumer.start = Mock(side_effect=lambda: setattr(consumer, '_running', True))
    consumer.stop = Mock(side_effect=lambda: setattr(consumer, '_running', False))
    app._wake_consumer = consumer

    app._session_mode_manager = SessionModeManager(
        abort_phrases=['stop listening'],
        foreground_exe_resolver=lambda: 'notepad.exe',
        foreground_hwnd_resolver=lambda: 111,
        inject_fn=Mock(return_value=True),
        remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(),
        agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
        ava_ready_probe_fn=lambda: None,
        on_mode_change=Mock(),
    )
    app._ensure_session_mode_manager = lambda: app._session_mode_manager

    for name in (
        '_ensure_wake_consumer', '_release_wake_consumer',
        'enter_command_mode', '_do_enter_command_mode', 'exit_command_mode',
        'enter_ava_mode', '_do_enter_ava_mode', 'exit_ava_mode',
    ):
        setattr(app, name, types.MethodType(getattr(dictation.DictationApp, name), app))
    return app


def _enter_unified_ava(app):
    app.enter_command_mode()
    assert app._session_mode_manager.mode is SessionMode.DICTATE
    outcome = app._session_mode_manager.dispatch_utterance('ava mode', _SIGNALS)
    assert outcome.kind == 'mode_switch'
    assert app._session_mode_manager.mode is SessionMode.AVA
    assert WakeConsumer._is_latched_ava_mode(app) is True
    assert app.command_mode_active is True
    assert app.ava_mode_active is False
    assert app._wake_consumer_reasons == {'toggle_session'}


def _exit_then_ava_ghost_tap(app):
    app.exit_command_mode()
    assert app.command_mode_active is False
    assert WakeConsumer._is_latched_ava_mode(app) is False
    app.enter_ava_mode()
    assert app.ava_mode_active is True
    assert app.command_mode_active is False
    app._clock.monotonic.return_value = 100.157
    app.exit_ava_mode()
    assert app._ava_mode_ghost_tap is True


class TestIncidentOrderRegression:
    def test_ava_cmd_active_then_ava_ghost_tap_exits_it(self, monkeypatch):
        """Ava is one toggle lane; an overlapping hold cannot steal ownership."""
        app = _make_app(monkeypatch)
        _enter_unified_ava(app)

        # The current contract refuses a competing hold while the session owns
        # capture. Releasing that rejected key must not tear down the owner.
        app.enter_ava_mode()
        app.exit_ava_mode()
        assert app.ava_mode_active is False
        assert WakeConsumer._is_latched_ava_mode(app) is True
        app._wake_consumer.stop.assert_not_called()

        _exit_then_ava_ghost_tap(app)
        assert app.command_mode_active is False
        assert app.ava_mode_active is False
        assert app._wake_consumer_reasons == set()
        assert app._wake_consumer._running is False

    def test_no_ava_cmd_utterance_handling_after_incident_sequence(self, monkeypatch):
        app = _make_app(monkeypatch)
        _enter_unified_ava(app)
        generation = execution_policy.current_generation(app)
        assert ava_command_session.is_cancelled('old-request') is False
        _exit_then_ava_ghost_tap(app)

        # These live WakeConsumer gates replace the removed _is_ai_cmd_mode.
        # Late resolver work is stale as well as unrouteable.
        assert WakeConsumer._is_toggle_cmd(app) is False
        assert WakeConsumer._is_latched_ava_mode(app) is False
        assert ava_command_session.is_cancelled('old-request') is True
        assert execution_policy.is_fresh(app, generation) is False

        # Entry workers queued before either exit must never restart capture.
        app._do_enter_command_mode()
        app._do_enter_ava_mode()
        app.start_recording.assert_not_called()
        assert app.recording is False

    def test_ava_cmd_exit_feedback_played_not_a_silent_teardown(self, monkeypatch):
        app = _make_app(monkeypatch)
        _enter_unified_ava(app)
        app.play_sound.reset_mock()

        app.exit_command_mode()
        app.exit_command_mode()  # Idempotent: one acknowledgement and release.

        app.play_sound.assert_called_once_with('stop')
        app._wake_consumer.stop.assert_called_once_with()
        app._release_streaming_preview.assert_called_once_with()
        app._cancel_command_mode_inactivity_timer.assert_called_once_with()
        assert app.command_mode_active is False


class TestExitFirstOnAvaEntry:
    def test_ava_entry_while_ava_cmd_inactive_does_not_touch_ava_cmd_state(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_mode()
        app.enter_ava_mode()  # Repeated entry must not start another worker.
        assert app.ava_mode_active is True
        assert app.command_mode_active is False
        assert app._wake_consumer_reasons == set()
        app._wake_consumer.start.assert_not_called()
        dictation.thread_registry.spawn.assert_called_once_with(
            'ava-mode-enter', app._do_enter_ava_mode, daemon=True)

    def test_ava_entry_is_still_blocked_by_command_mode(self, monkeypatch):
        app = _make_app(monkeypatch, command_mode='hold')
        app.enter_command_mode()
        app.enter_ava_mode()
        assert app.command_mode_active is True
        assert app.ava_mode_active is False
        dictation.thread_registry.spawn.assert_called_once_with(
            'cmd-mode-enter', app._do_enter_command_mode, daemon=True)


class TestExitFirstOnCommandModeEntry:
    def test_hold_command_mode_entry_exits_active_ava_command_session(self, monkeypatch):
        """Exit the unified owner before reconfiguring capture as a hold."""
        app = _make_app(monkeypatch)
        _enter_unified_ava(app)
        app.exit_command_mode()
        assert WakeConsumer._is_latched_ava_mode(app) is False
        assert app._wake_consumer_reasons == set()

        app.config['command_mode']['mode'] = 'hold'
        app.enter_command_mode()
        app._do_enter_command_mode()

        assert app.command_mode_active is True
        assert app.ava_mode_active is False
        assert WakeConsumer._is_toggle_cmd(app) is False
        app.start_recording.assert_called_once_with(
            streaming=False, play_earcon=False, capture_path='command_key')

    def test_toggle_hands_free_session_entry_exits_active_ava_command_session(self, monkeypatch):
        """Re-entry starts in DICTATE, with no old AVA lane or cancellation."""
        app = _make_app(monkeypatch)
        _enter_unified_ava(app)
        app.exit_command_mode()
        assert ava_command_session.is_cancelled('next-request') is True

        app.enter_command_mode()
        assert app._session_mode_manager.mode is SessionMode.DICTATE
        assert app.command_mode_active is True
        assert app.ava_mode_active is False
        assert WakeConsumer._is_latched_ava_mode(app) is False
        assert WakeConsumer._is_toggle_dictate(app) is True
        assert ava_command_session.is_cancelled('next-request') is False
        assert app._wake_consumer_reasons == {'toggle_session'}
        assert app._wake_consumer.start.call_count == 2


class TestOppositeDirectionUnchanged:
    def test_ava_cmd_entry_rejected_while_ava_active(self, monkeypatch):
        """The unified toggle cannot enter over Ava hold capture either."""
        app = _make_app(monkeypatch)
        app.enter_ava_mode()
        app.enter_command_mode()
        assert app.ava_mode_active is True
        assert app.command_mode_active is False
        assert app._wake_consumer_reasons == set()
        app._reset_command_mode_inactivity_timer.assert_not_called()
        dictation.thread_registry.spawn.assert_called_once_with(
            'ava-mode-enter', app._do_enter_ava_mode, daemon=True)

    def test_ava_cmd_entry_rejected_while_command_mode_active(self, monkeypatch):
        """Repeated entry cannot reset an active unified AVA lane."""
        app = _make_app(monkeypatch)
        _enter_unified_ava(app)
        app.enter_command_mode()

        assert app._session_mode_manager.mode is SessionMode.AVA
        assert WakeConsumer._is_latched_ava_mode(app) is True
        assert app.ava_mode_active is False
        assert app._wake_consumer_reasons == {'toggle_session'}
        app._wake_consumer.start.assert_called_once_with()
        app._reset_command_mode_inactivity_timer.assert_called_once_with(300)
        dictation.thread_registry.spawn.assert_called_once_with(
            'cmd-mode-enter', app._do_enter_command_mode, daemon=True)
