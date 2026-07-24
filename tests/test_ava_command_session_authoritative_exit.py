"""Tests for the Ava command session's authoritative exit across async work
(2026-07-19 incident report items 3, 5, 6, 7, 8 -- Fix 2 / P0b -- carried
over verbatim into D3 per the Ava Front Door spec's explicit "2026-07-19
exclusive-ownership + generation guards carry over" requirement).

The incident's root latch (Fix 1) is necessary but not sufficient: even
with exclusive mode ownership, the old ai_command_mode's own exit could be
missed or made ineffective several other ways:
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

Ava Front Door P1: migrated from
tests/test_ai_command_mode_authoritative_exit.py (deleted) --
ai_command_mode.py was replaced by samsara/ava_command_session.py +
DictationApp.enter_ava_command_session/exit_ava_command_session/
_handle_ava_command_utterance. This file covers: the reason-counted
WakeConsumer lease (item 5), config-change exit (item 3), the
session-generation staleness guard at each of its checkpoints (item 7 +
the resolution half of item 8), and cancel-persists-until-reentry (the
queue half of item 8). The crash force-exit (item 6) is covered separately
in test_inactivity_chokepoint.py, already migrated in this same pass.

Checkpoint (c)/(d) note: the old module split "after resolution" and
"before every plan step" into two separate checkpoints because it had a
separate resolver + multi-step plan executor (_execute_plan). That
architecture is EXPLICITLY DROPPED by the spec ("separate resolver+plan
executor" in the behavior inventory's DROP list) -- the new waterfall's
stage (c) is a single LLM fallback call that delegates entirely to
ask_ollama.handle_response() (which has its own pre-existing, separately
tested execution path), so there is no new-module equivalent to
_execute_plan's per-step staleness recheck to migrate. The single
remaining checkpoint (immediately after the stage (c) LLM call returns,
before parsing/dispatching) is covered below as
TestGenerationCheckpointC_AfterLLMFallback.
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
from samsara import ava_command_session
from plugins.commands import ask_ollama


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

    app.ava_command_session_active = False
    app.command_mode_active = False
    app.ava_mode_active = False
    app._ava_cmd_mode_lock = threading.Lock()
    app._ava_cmd_miss_count = 0
    app._ava_cmd_generation = 0
    app._ava_cmd_ready = threading.Event()
    app._ava_cmd_ready.set()
    app.config = {
        'command_mode': {'enter_debounce_ms': 0},
        'ava_command_session': {'enabled': True, 'key': 'right_ctrl'},
    }
    app.play_sound = Mock()
    # Inactivity timer plumbing is stubbed rather than bound -- this file
    # asserts on lease/generation/config-change behavior, not timer wiring.
    app._reset_ava_cmd_inactivity_timer = lambda timeout_s: None
    app._cancel_ava_cmd_inactivity_timer = lambda: None

    # No-op the async worker spawn -- enter/exit's own boolean flips and
    # lease calls all happen synchronously before spawn() is reached.
    # Actually running the spawned body would trigger a real Ollama
    # warm-up HTTP call and winsound ready-cue playback.
    # _do_enter_ava_command_session itself must still exist as an
    # attribute -- it's evaluated as spawn()'s argument even though the
    # no-op spawn never calls it.
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)
    app._do_enter_ava_command_session = lambda: None

    app.enter_ava_command_session = types.MethodType(dictation.DictationApp.enter_ava_command_session, app)
    app.exit_ava_command_session = types.MethodType(dictation.DictationApp.exit_ava_command_session, app)
    app.update_config = types.MethodType(dictation.DictationApp.update_config, app)
    app._apply_disk_config = types.MethodType(dictation.DictationApp._apply_disk_config, app)
    app._maybe_exit_ava_command_session_on_config_change = types.MethodType(
        dictation.DictationApp._maybe_exit_ava_command_session_on_config_change, app,
    )
    app._config_lock = threading.Lock()
    app.save_config = Mock()
    app._config_last_disk_snapshot = None
    return app


class TestWakeConsumerLease:
    def test_entering_with_wake_word_off_starts_the_consumer(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ava_command_session()
        assert app._wake_consumer._running is True
        assert 'ava_command_session' in app._wake_consumer_reasons

    def test_exiting_stops_the_consumer_when_wake_word_was_off(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ava_command_session()
        app.exit_ava_command_session()
        assert app._wake_consumer._running is False, \
            "must not leave an orphaned pipeline running after exit"

    def test_entering_with_wake_word_already_on_leaves_it_running_on_exit(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=True, existing_reasons={'wake_word'})
        app.enter_ava_command_session()
        assert app._wake_consumer._running is True
        app.exit_ava_command_session()
        assert app._wake_consumer._running is True, \
            "Ava command session exit must not stop wake detection's own consumer"
        assert app._wake_consumer_reasons == {'wake_word'}

    def test_double_enter_does_not_double_acquire(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ava_command_session()
        app.enter_ava_command_session()
        assert app._wake_consumer.start_calls == 1
        assert app._wake_consumer_reasons == {'ava_command_session'}

    def test_double_exit_is_safe(self, monkeypatch):
        app = _make_app(monkeypatch, wake_consumer_running=False)
        app.enter_ava_command_session()
        app.exit_ava_command_session()
        app.exit_ava_command_session()
        assert app._wake_consumer.stop_calls == 1


class TestGenerationBumpedOnEnterAndExit:
    def test_enter_bumps_generation(self, monkeypatch):
        app = _make_app(monkeypatch)
        before = app._ava_cmd_generation
        app.enter_ava_command_session()
        assert app._ava_cmd_generation == before + 1

    def test_exit_bumps_generation_again(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        after_enter = app._ava_cmd_generation
        app.exit_ava_command_session()
        assert app._ava_cmd_generation == after_enter + 1


class TestCancelPersistsUntilReentry:
    """Item 8, queue half: exit used to cancel_queue() then IMMEDIATELY
    reset_cancel(), leaving only a brief window where the worker's
    dequeue-time check could catch anything. The cancel flag now stays
    SET for the entire time the session is inactive."""

    def test_cancel_is_clear_after_a_fresh_entry(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        assert ava_command_session._cancel.is_set() is False

    def test_cancel_stays_set_after_exit(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        app.exit_ava_command_session()
        assert ava_command_session._cancel.is_set() is True

    def test_cancel_remains_set_indefinitely_until_next_entry(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        app.exit_ava_command_session()
        assert ava_command_session._cancel.is_set() is True
        assert ava_command_session._cancel.is_set() is True  # still set, no auto-clear
        app.enter_ava_command_session()
        assert ava_command_session._cancel.is_set() is False, \
            "only a fresh entry's reset_cancel() re-arms the worker"


class TestConfigChangeWhileActiveForcesExit:
    """Item 3: disabling the feature or changing its key while active must
    not silently strand the user with ava_command_session_active still
    True and no working exit binding."""

    def test_update_config_disabling_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        assert app.ava_command_session_active is True
        app.update_config({'ava_command_session': {'enabled': False, 'key': 'right_ctrl'}}, save=False)
        assert app.ava_command_session_active is False

    def test_update_config_changing_key_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        app.update_config({'ava_command_session': {'enabled': True, 'key': 'left_alt'}}, save=False)
        assert app.ava_command_session_active is False

    def test_update_config_unrelated_change_while_active_does_not_exit(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.enter_ava_command_session()
        app.update_config(
            {'ava_command_session': {'enabled': True, 'key': 'right_ctrl', 'miss_limit': 5}},
            save=False,
        )
        assert app.ava_command_session_active is True

    def test_update_config_change_while_inactive_is_a_noop(self, monkeypatch):
        app = _make_app(monkeypatch)
        assert app.ava_command_session_active is False
        app.update_config({'ava_command_session': {'enabled': False}}, save=False)
        assert app.ava_command_session_active is False  # nothing to exit, no crash

    def test_disk_reload_disabling_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.config['ava_command_session'] = {'enabled': True, 'key': 'right_ctrl'}
        app.enter_ava_command_session()
        assert app.ava_command_session_active is True
        # _three_way_merge only honours an external (disk) edit when the
        # last-known-disk snapshot shows THAT key unchanged at runtime --
        # otherwise "new in both" resolves to memory winning. Snapshot
        # must reflect the current in-memory config for the disk edit
        # below to actually take effect through the merge.
        app._config_last_disk_snapshot = copy.deepcopy(app.config)
        new_disk = copy.deepcopy(app.config)
        new_disk['ava_command_session'] = {'enabled': False, 'key': 'right_ctrl'}
        app._apply_disk_config(new_disk)
        assert app.ava_command_session_active is False

    def test_disk_reload_changing_key_while_active_exits(self, monkeypatch):
        app = _make_app(monkeypatch)
        app.config['ava_command_session'] = {'enabled': True, 'key': 'right_ctrl'}
        app.enter_ava_command_session()
        app._config_last_disk_snapshot = copy.deepcopy(app.config)
        new_disk = copy.deepcopy(app.config)
        new_disk['ava_command_session'] = {'enabled': True, 'key': 'left_alt'}
        app._apply_disk_config(new_disk)
        assert app.ava_command_session_active is False


# =============================================================================
# Session-generation staleness guard: checkpoints (a) ready-wait and
# (b) after-transcription, both in dictation.py's _handle_ava_command_utterance.
# =============================================================================

class _GenerationBumpingEvent:
    """Stand-in for threading.Event whose .wait() simulates "an exit (and
    generation bump) happened while this coroutine was blocked here"."""

    def __init__(self, app, bump_to):
        self._app = app
        self._bump_to = bump_to

    def wait(self, timeout=None):
        self._app._ava_cmd_generation = self._bump_to
        return True


def _make_handler_app(entry_generation=0, transcript="take a screenshot"):
    app = types.SimpleNamespace()
    app._ava_cmd_generation = entry_generation
    app._ava_cmd_ready = threading.Event()
    app._ava_cmd_ready.set()
    app._wake_transcription_in_progress = False
    app.model_rate = 16000
    app.model_lock = threading.Lock()
    segment = types.SimpleNamespace(text=transcript)
    app.model = types.SimpleNamespace(transcribe=Mock(return_value=([segment], None)))
    app.get_transcription_params = Mock(return_value={})
    app.voice_training_window = types.SimpleNamespace(apply_corrections=lambda t: t)
    app._vad_reset = Mock()
    app.config = {}
    app._reset_ava_cmd_inactivity_timer = lambda timeout_s: None
    app._handle_ava_command_utterance = types.MethodType(
        dictation.DictationApp._handle_ava_command_utterance, app,
    )
    return app


def _one_second_buffer():
    return [np.zeros(16000, dtype=np.float32)]


class TestGenerationCheckpointA_ReadyWait:
    def test_stale_generation_after_ready_wait_drops_before_transcribing(self, monkeypatch, caplog):
        app = _make_handler_app(entry_generation=0)
        app._ava_cmd_ready = _GenerationBumpingEvent(app, bump_to=1)
        enqueue_mock = Mock()
        monkeypatch.setattr(ava_command_session, 'enqueue_utterance', enqueue_mock)
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._handle_ava_command_utterance(_one_second_buffer(), 16000)
        app.model.transcribe.assert_not_called()
        enqueue_mock.assert_not_called()
        assert any('Stale generation after ready wait' in r.message for r in caplog.records)

    def test_fresh_generation_after_ready_wait_proceeds(self, monkeypatch):
        app = _make_handler_app(entry_generation=0)  # real Event, already set, no change
        enqueue_mock = Mock()
        monkeypatch.setattr(ava_command_session, 'enqueue_utterance', enqueue_mock)
        app._handle_ava_command_utterance(_one_second_buffer(), 16000)
        app.model.transcribe.assert_called_once()
        enqueue_mock.assert_called_once_with(app, 0, "take a screenshot")


class TestGenerationCheckpointB_AfterTranscription:
    def test_stale_generation_after_transcription_drops_before_enqueue(self, monkeypatch, caplog):
        app = _make_handler_app(entry_generation=0)

        def _transcribe_then_go_stale(*a, **k):
            app._ava_cmd_generation = 1  # simulate exit happened mid-transcription
            return ([types.SimpleNamespace(text="take a screenshot")], None)

        app.model.transcribe = Mock(side_effect=_transcribe_then_go_stale)
        enqueue_mock = Mock()
        monkeypatch.setattr(ava_command_session, 'enqueue_utterance', enqueue_mock)
        with caplog.at_level(logging.DEBUG, logger="Samsara.dictation"):
            app._handle_ava_command_utterance(_one_second_buffer(), 16000)
        enqueue_mock.assert_not_called()
        assert any('Stale generation after transcription' in r.message for r in caplog.records)

    def test_fresh_generation_after_transcription_enqueues_with_captured_generation(self, monkeypatch):
        app = _make_handler_app(entry_generation=7)
        enqueue_mock = Mock()
        monkeypatch.setattr(ava_command_session, 'enqueue_utterance', enqueue_mock)
        app._handle_ava_command_utterance(_one_second_buffer(), 16000)
        enqueue_mock.assert_called_once_with(app, 7, "take a screenshot")


# =============================================================================
# Session-generation staleness guard: checkpoint (c) after the stage (c) LLM
# fallback call returns, and the _speak/_register_miss gates the waterfall's
# escalation helpers apply on every call (samsara/ava_command_session.py).
# =============================================================================

def _make_ai_cmd_app(generation=0):
    app = Mock()
    app._ava_cmd_generation = generation
    app._ava_cmd_miss_count = 0
    app.ava_command_session_active = True
    app.command_executor = Mock()
    app.command_executor.process_text = Mock(return_value=(None, False))  # stage (a) miss
    app.audio_coordinator = Mock()
    app.exit_ava_command_session = Mock()
    app.play_sound = Mock()
    app.config = {'ava_command_session': {}}
    return app


def _spoken_texts(app):
    return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]


class TestGenerationCheckpointC_AfterLLMFallback:
    def test_stale_generation_after_llm_call_drops_silently(self, monkeypatch, caplog):
        app = _make_ai_cmd_app(generation=0)
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: [])
        handle_response_mock = Mock()
        monkeypatch.setattr(ask_ollama, 'handle_response', handle_response_mock)

        def _ask_then_go_stale(*a, **k):
            app._ava_cmd_generation = 1  # simulate exit happened during the LLM call
            return "some conversational reply"

        monkeypatch.setattr(ask_ollama, 'ask_ollama', _ask_then_go_stale)
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.ava_command_session"):
            ava_command_session._process_utterance(app, 0, "take a screenshot")
        handle_response_mock.assert_not_called()
        assert app._ava_cmd_miss_count == 0
        assert any('Stale generation' in r.message for r in caplog.records)

    def test_fresh_generation_after_llm_call_dispatches_normally(self, monkeypatch):
        app = _make_ai_cmd_app(generation=0)
        monkeypatch.setattr(ava_command_session, '_match_action2_grammar', lambda u: None)
        # 'screenshot' must be a real shortlist member: the stage (c)
        # closed-world gate (2026-07-23, samsara.ava_command_session.
        # _closed_world_selection_ok) only forwards an ACTION whose command
        # name matches the shortlist it was given verbatim -- see
        # tests/test_ava_command_session_closed_world.py for that gate's
        # own dedicated coverage. This test is about generation freshness,
        # not command validation, so the mocked parse must be a realistic
        # (shortlist-matching, real 'command' key) response for the hit
        # path below to actually exercise "dispatches normally".
        monkeypatch.setattr(ava_command_session, '_build_shortlist', lambda app, u, cfg: ['screenshot'])
        monkeypatch.setattr(ask_ollama, 'ask_ollama', lambda *a, **k: 'ACTION: screenshot')
        monkeypatch.setattr(
            ask_ollama, '_parse_structured_response',
            lambda resp: {'type': 'action', 'command': 'screenshot'},
        )
        handle_response_mock = Mock()
        monkeypatch.setattr(ask_ollama, 'handle_response', handle_response_mock)
        ava_command_session._process_utterance(app, 0, "take a screenshot")
        handle_response_mock.assert_called_once()
        assert app._ava_cmd_miss_count == 0  # a hit, not a miss


class TestGenerationGatedSpeakAndRegisterMiss:
    def test_speak_drops_when_generation_stale(self, caplog):
        app = _make_ai_cmd_app(generation=5)
        with caplog.at_level(logging.DEBUG, logger="Samsara.samsara.ava_command_session"):
            ava_command_session._speak(app, "hello", 0)
        assert _spoken_texts(app) == []
        assert any(
            'Stale generation' in r.message and 'dropping TTS' in r.message
            for r in caplog.records
        )

    def test_speak_fires_when_generation_matches(self):
        app = _make_ai_cmd_app(generation=0)
        ava_command_session._speak(app, "hello", 0)
        assert _spoken_texts(app) == ["hello"]

    def test_register_miss_gated_on_generation_before_any_state_change(self):
        """The whole point of gating _register_miss at its top: a stale
        call must not increment the counter, chime, speak, or -- worst
        case -- exit a DIFFERENT, possibly freshly re-entered session."""
        app = _make_ai_cmd_app(generation=5)
        cfg = {'miss_limit': 3}
        ava_command_session._register_miss(app, cfg, 0)
        assert app._ava_cmd_miss_count == 0
        app.play_sound.assert_not_called()
        app.exit_ava_command_session.assert_not_called()
        assert _spoken_texts(app) == []
