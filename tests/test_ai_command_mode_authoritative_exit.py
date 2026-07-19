"""Tests for AI-command-mode's authoritative exit across async work
(2026-07-19 incident report items 3, 5, 6, 7, 8 -- Fix 2 / P0b).

The incident's root latch (Fix 1) is necessary but not sufficient: even
with exclusive mode ownership, AI-command-mode's own exit could be missed
or made ineffective several other ways:
  5. enter_ai_command_mode() never took a WakeConsumer lease -- it only
     worked if wake-word mode (or a toggle session) happened to already be
     running the pipeline. Wake word off -> latched but deaf.
  6. A WakeConsumer poll-loop crash force-exited toggle-command-mode only,
     never AI-command-mode.
  3. Disabling ai_command_mode, or changing its key, while active had no
     runtime side effect -- the user's exit binding could vanish while the
     boolean stayed True.
  7. exit_ai_command_mode() set the ready event but nothing rechecked
     ai_command_mode_active before enqueueing or speaking -- work already
     in flight (ready-wait, transcription) could land after a legitimate
     exit.
  8. exit_ai_command_mode() cleared its own cancel flag immediately after
     setting it, so the worker's dequeue-time check only had a brief
     window to actually catch anything.

This file covers: the reason-counted WakeConsumer lease (item 5), the
crash force-exit (item 6, covered separately in
test_inactivity_chokepoint.py -- already exercises that exact code path),
config-change exit (item 3), the session-generation staleness guard at
each of its four checkpoints (item 7 + the plan/TTS half of item 8), and
cancel-persists-until-reentry (the queue half of item 8).
"""
import copy
import logging
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from samsara import ai_command_mode


@pytest.fixture(autouse=True)
def _reset_ai_command_mode_module_state():
    ai_command_mode._pending_plan = None
    ai_command_mode._cancel.clear()
    yield
    ai_command_mode._pending_plan = None
    ai_command_mode._cancel.clear()


# =============================================================================
# Item 5: reason-counted WakeConsumer lease + item 3: config-change exit +
# the cancel-persists-until-reentry half of item 8.
#
# Real bound DictationApp methods via types.MethodType, same philosophy as
# tests/test_wake_consumer_lifecycle.py -- a hand-copied reimplementation
# would not reproduce wiring bugs like the incident's.
# =============================================================================

class FakeWakeConsumer:
    def __init__(self):
        self._running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.stop_return = []

    def start(self):
        self.start_calls += 1
        self._running = True

    def stop(self):
        self.stop_calls += 1
        self._running = False
        return self.stop_return


def _make_app(monkeypatch, wake_consumer_running=False, existing_reasons=None):
    app = types.SimpleNamespace()
    app._wake_consumer = FakeWakeConsumer()
    app._wake_consumer._running = wake_consumer_running
    app._wake_consumer_reasons = set(existing_reasons or ())
    app._wake_consumer_lock = threading.Lock()
    app.wake_word_triggered = False
    app.process_wake_word_buffer = Mock()
    app._ensure_wake_consumer = types.MethodType(dictation.DictationApp._ensure_wake_consumer, app)
    app._release_wake_consumer = types.MethodType(dictation.DictationApp._release_wake_consumer, app)

    app.ai_command_mode_active = False
    app.command_mode_active = False
    app.ava_mode_active = False
    app._ai_cmd_mode_lock = threading.Lock()
    app._ai_cmd_miss_count = 0
    app._ai_cmd_generation = 0
    app._ai_cmd_ready = threading.Event()
    app._ai_cmd_ready.set()
    app.config = {
        'command_mode': {'enter_debounce_ms': 0},
        'ai_command_mode': {'enabled': True, 'key': 'right_ctrl'},
    }
    app.play_sound = Mock()

    # No-op the async worker spawn -- enter/exit's own boolean flips and
    # lease calls all happen synchronously before spawn() is reached.
    # Actually running the spawned body would trigger a real Ollama
    # warm-up HTTP call and winsound ready-cue playback. _do_enter_ai_
    # command_mode itself must still exist as an attribute -- it's
    # evaluated as spawn()'s argument even though the no-op spawn never
    # calls it.
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)
    app._do_enter_ai_command_mode = lambda: None

    app.enter_ai_command_mode = types.MethodType(dictation.DictationApp.enter_ai_command_mode, app)
    app.exit_ai_command_mode = types.MethodType(dictation.DictationApp.exit_ai_command_mode, app)
    app.update_config = types.MethodType(dictation.DictationApp.update_config, app)
    app._apply_disk_config = types.MethodType(dictation.DictationApp._apply_disk_config, app)
    app._maybe_exit_ai_command_mode_on_config_change = types.MethodType(
        dictation.DictationApp._maybe_exit_ai_command_mode_on_config_change, app,
    )
    app._config_lock = threading.Lock()
    app.save_config = Mock()
    app._config_last_disk_snapshot = None
    return app


class TestWakeConsumerLease:
    def test_entering_with_wake_word_off_starts_the_consumer(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ai_command_mode()
        assert app._wake_consumer._running is True
        assert 'ai_command_mode' in app._wake_consumer_reasons

    def test_exiting_stops_the_consumer_when_wake_word_was_off(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ai_command_mode()
        app.exit_ai_command_mode()
        assert app._wake_consumer._running is False, \
            "must not leave an orphaned pipeline running after exit"

    def test_entering_with_wake_word_already_on_leaves_it_running_on_exit(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=True, existing_reasons={'wake_word'})
        app.enter_ai_command_mode()
        assert app._wake_consumer._running is True
        app.exit_ai_command_mode()
        assert app._wake_consumer._running is True, \
            "AI-command-mode exit must not stop wake detection's own consumer"
        assert app._wake_consumer_reasons == {'wake_word'}

    def test_double_enter_does_not_double_acquire(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ai_command_mode()
        app.enter_ai_command_mode()
        assert app._wake_consumer.start_calls == 1
        assert app._wake_consumer_reasons == {'ai_command_mode'}

    def test_double_exit_is_safe(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ai_command_mode()
        app.exit_ai_command_mode()
        app.exit_ai_command_mode()
        assert app._wake_consumer.stop_calls == 1


class TestGenerationBumpedOnEnterAndExit:
    def test_enter_bumps_generation(self, monkeypatch):
        app = _make_app(monkeypatch)
        before = app._ai_cmd_generation
        app.enter_ai_command_mode()
        assert app._ai_cmd_generation == before + 1

    def test_exit_bumps_generation_again(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        after_enter = app._ai_cmd_generation
        app.exit_ai_command_mode()
        assert app._ai_cmd_generation == after_enter + 1


class TestCancelPersistsUntilReentry:
    """Item 8, queue half: exit used to cancel_queue() then IMMEDIATELY
    reset_cancel(), leaving only a brief window where the worker's
    dequeue-time check could catch anything. The cancel flag now stays
    SET for the entire time the mode is inactive."""

    def test_cancel_is_clear_after_a_fresh_entry(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        assert ai_command_mode._cancel.is_set() is False

    def test_cancel_stays_set_after_exit(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        app.exit_ai_command_mode()
        assert ai_command_mode._cancel.is_set() is True

    def test_cancel_remains_set_indefinitely_until_next_entry(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        app.exit_ai_command_mode()
        assert ai_command_mode._cancel.is_set() is True
        assert ai_command_mode._cancel.is_set() is True  # still set, no auto-clear
        app.enter_ai_command_mode()
        assert ai_command_mode._cancel.is_set() is False, \
            "only a fresh entry's reset_cancel() re-arms the worker"


class TestConfigChangeWhileActiveForcesExit:
    """Item 3: disabling the feature or changing its key while active must
    not silently strand the user with ai_command_mode_active still True
    and no working exit binding."""

    def test_update_config_disabling_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        assert app.ai_command_mode_active is True
        app.update_config({'ai_command_mode': {'enabled': False, 'key': 'right_ctrl'}}, save=False)
        assert app.ai_command_mode_active is False

    def test_update_config_changing_key_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        app.update_config({'ai_command_mode': {'enabled': True, 'key': 'left_alt'}}, save=False)
        assert app.ai_command_mode_active is False

    def test_update_config_unrelated_change_while_active_does_not_exit(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ai_command_mode()
        app.update_config(
            {'ai_command_mode': {'enabled': True, 'key': 'right_ctrl', 'miss_limit': 5}},
            save=False,
        )
        assert app.ai_command_mode_active is True

    def test_update_config_change_while_inactive_is_a_noop(self, monkeypatch):
        app = _make_app(monkeypatch)
        assert app.ai_command_mode_active is False
        app.update_config({'ai_command_mode': {'enabled': False}}, save=False)
        assert app.ai_command_mode_active is False  # nothing to exit, no crash

    def test_disk_reload_disabling_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.config['ai_command_mode'] = {'enabled': True, 'key': 'right_ctrl'}
        app.enter_ai_command_mode()
        assert app.ai_command_mode_active is True
        # _three_way_merge only honours an external (disk) edit when the
        # last-known-disk snapshot shows THAT key unchanged at runtime --
        # otherwise "new in both" resolves to memory winning. Snapshot
        # must reflect the current in-memory config for the disk edit
        # below to actually take effect through the merge.
        app._config_last_disk_snapshot = copy.deepcopy(app.config)
        new_disk = copy.deepcopy(app.config)
        new_disk['ai_command_mode'] = {'enabled': False, 'key': 'right_ctrl'}
        app._apply_disk_config(new_disk)
        assert app.ai_command_mode_active is False

    def test_disk_reload_changing_key_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.config['ai_command_mode'] = {'enabled': True, 'key': 'right_ctrl'}
        app.enter_ai_command_mode()
        app._config_last_disk_snapshot = copy.deepcopy(app.config)
        new_disk = copy.deepcopy(app.config)
        new_disk['ai_command_mode'] = {'enabled': True, 'key': 'left_alt'}
        app._apply_disk_config(new_disk)
        assert app.ai_command_mode_active is False


# =============================================================================
# Session-generation staleness guard: checkpoints (a) ready-wait and
# (b) after-transcription, both in dictation.py's _handle_ai_command_utterance.
# =============================================================================

class _GenerationBumpingEvent:
    """Stand-in for threading.Event whose .wait() simulates "an exit (and
    generation bump) happened while this coroutine was blocked here"."""

    def __init__(self, app, bump_to):
        self._app = app
        self._bump_to = bump_to

    def wait(self, timeout=None):
        self._app._ai_cmd_generation = self._bump_to
        return True


def _make_handler_app(entry_generation=0, transcript="take a screenshot"):
    app = types.SimpleNamespace()
    app._ai_cmd_generation = entry_generation
    app._ai_cmd_ready = threading.Event()
    app._ai_cmd_ready.set()
    app._wake_transcription_in_progress = False
    app.model_rate = 16000
    app.model_lock = threading.Lock()
    segment = types.SimpleNamespace(text=transcript)
    app.model = types.SimpleNamespace(transcribe=Mock(return_value=([segment], None)))
    app.get_transcription_params = Mock(return_value={})
    app.voice_training_window = types.SimpleNamespace(apply_corrections=lambda t: t)
    app._vad_reset = Mock()
    app._handle_ai_command_utterance = types.MethodType(
        dictation.DictationApp._handle_ai_command_utterance, app,
    )
    return app


def _one_second_buffer():
    return [np.zeros(16000, dtype=np.float32)]


class TestGenerationCheckpointA_ReadyWait:
    def test_stale_generation_after_ready_wait_drops_before_transcribing(self, monkeypatch, caplog):
        app = _make_handler_app(entry_generation=0)
        app._ai_cmd_ready = _GenerationBumpingEvent(app, bump_to=1)
        enqueue_mock = Mock()
        monkeypatch.setattr(ai_command_mode, 'enqueue_utterance', enqueue_mock)
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._handle_ai_command_utterance(_one_second_buffer(), 16000)
        app.model.transcribe.assert_not_called()
        enqueue_mock.assert_not_called()
        assert any('Stale generation after ready wait' in r.message for r in caplog.records)

    def test_fresh_generation_after_ready_wait_proceeds(self, monkeypatch):
        app = _make_handler_app(entry_generation=0)  # real Event, already set, no change
        enqueue_mock = Mock()
        monkeypatch.setattr(ai_command_mode, 'enqueue_utterance', enqueue_mock)
        app._handle_ai_command_utterance(_one_second_buffer(), 16000)
        app.model.transcribe.assert_called_once()
        enqueue_mock.assert_called_once_with(app, 0, "take a screenshot")


class TestGenerationCheckpointB_AfterTranscription:
    def test_stale_generation_after_transcription_drops_before_enqueue(self, monkeypatch, caplog):
        app = _make_handler_app(entry_generation=0)

        def _transcribe_then_go_stale(*a, **k):
            app._ai_cmd_generation = 1  # simulate exit happened mid-transcription
            return ([types.SimpleNamespace(text="take a screenshot")], None)

        app.model.transcribe = Mock(side_effect=_transcribe_then_go_stale)
        enqueue_mock = Mock()
        monkeypatch.setattr(ai_command_mode, 'enqueue_utterance', enqueue_mock)
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._handle_ai_command_utterance(_one_second_buffer(), 16000)
        enqueue_mock.assert_not_called()
        assert any('Stale generation after transcription' in r.message for r in caplog.records)

    def test_fresh_generation_after_transcription_enqueues_with_captured_generation(self, monkeypatch):
        app = _make_handler_app(entry_generation=7)
        enqueue_mock = Mock()
        monkeypatch.setattr(ai_command_mode, 'enqueue_utterance', enqueue_mock)
        app._handle_ai_command_utterance(_one_second_buffer(), 16000)
        enqueue_mock.assert_called_once_with(app, 7, "take a screenshot")


# =============================================================================
# Session-generation staleness guard: checkpoints (c) after resolution and
# (d) before every plan step / every TTS call, both in ai_command_mode.py.
# =============================================================================

from types import SimpleNamespace as _NS  # noqa: E402


def _make_ai_cmd_app(generation=0, commands=None):
    app = Mock()
    app._ai_cmd_generation = generation
    app._ai_cmd_miss_count = 0
    app.ai_command_mode_active = True
    app.command_executor = _NS(
        commands=commands or {'screenshot': {'ai_visible': True}},
        execute_command=Mock(),
    )
    app.audio_coordinator = Mock()
    app.exit_ai_command_mode = Mock()
    app.config = {'ai_command_mode': {'show_plan_hud': False}}
    return app


def _spoken_texts(app):
    return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]


class TestGenerationCheckpointC_AfterResolution:
    def test_stale_generation_after_resolution_drops_silently(self, monkeypatch, caplog):
        app = _make_ai_cmd_app(generation=0)

        def _resolve_then_go_stale(*a, **k):
            app._ai_cmd_generation = 1  # simulate exit happened during resolve
            return ['screenshot']

        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', _resolve_then_go_stale)
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.ai_command_mode"):
            ai_command_mode._process_utterance(app, 0, "take a screenshot")
        app.command_executor.execute_command.assert_not_called()
        assert _spoken_texts(app) == []
        assert app._ai_cmd_miss_count == 0
        assert any('Stale generation after resolution' in r.message for r in caplog.records)

    def test_fresh_generation_after_resolution_executes_normally(self, monkeypatch):
        app = _make_ai_cmd_app(generation=0)
        monkeypatch.setattr(ai_command_mode, 'resolve_utterance', Mock(return_value=['screenshot']))
        ai_command_mode._process_utterance(app, 0, "take a screenshot")
        app.command_executor.execute_command.assert_called_once_with('screenshot')


class TestGenerationCheckpointD_PlanStepsAndTTS:
    def test_stale_generation_stops_remaining_plan_steps(self, caplog):
        app = _make_ai_cmd_app(generation=0)
        executed = []

        def _exec_side_effect(cmd):
            executed.append(cmd)
            app._ai_cmd_generation = 1  # simulate exit happened right after step 1

        app.command_executor.execute_command = Mock(side_effect=_exec_side_effect)
        cfg = {'show_plan_hud': False, 'step_settle_seconds': 0}
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.ai_command_mode"):
            ai_command_mode._execute_plan(app, 0, ['cmd_a', 'cmd_b', 'cmd_c'], cfg)
        assert executed == ['cmd_a']
        assert any('Stale generation before step' in r.message for r in caplog.records)

    def test_fresh_generation_runs_every_step(self):
        app = _make_ai_cmd_app(generation=0)
        executed = []
        app.command_executor.execute_command = Mock(side_effect=lambda cmd: executed.append(cmd))
        cfg = {'show_plan_hud': False, 'step_settle_seconds': 0}
        ai_command_mode._execute_plan(app, 0, ['cmd_a', 'cmd_b', 'cmd_c'], cfg)
        assert executed == ['cmd_a', 'cmd_b', 'cmd_c']

    def test_speak_drops_when_generation_stale(self, caplog):
        app = _make_ai_cmd_app(generation=5)
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.ai_command_mode"):
            ai_command_mode._speak(app, "hello", 0)
        assert _spoken_texts(app) == []
        assert any(
            'Stale generation' in r.message and 'dropping TTS' in r.message
            for r in caplog.records
        )

    def test_speak_fires_when_generation_matches(self):
        app = _make_ai_cmd_app(generation=0)
        ai_command_mode._speak(app, "hello", 0)
        assert _spoken_texts(app) == ["hello"]

    def test_register_miss_gated_on_generation_before_any_state_change(self):
        """The whole point of gating _register_miss at its top: a stale
        call must not increment the counter, chime, speak, or -- worst
        case -- exit a DIFFERENT, possibly freshly re-entered session."""
        app = _make_ai_cmd_app(generation=5)
        cfg = {'miss_limit': 3}
        ai_command_mode._register_miss(app, cfg, 0)
        assert app._ai_cmd_miss_count == 0
        app.play_sound.assert_not_called()
        app.exit_ai_command_mode.assert_not_called()
        assert _spoken_texts(app) == []
