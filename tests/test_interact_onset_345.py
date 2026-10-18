"""345d: utterance-owned Interact onset metadata across the toggle FIFO."""

from __future__ import annotations

import ast
import collections
import dataclasses
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.audio_engine import wake_consumer as wake_module
from samsara.interact import InteractController
from samsara.session_modes import is_self_hear_capture, normalize_utterance
from samsara.subjects import InteractState, OnsetContext, WindowIdentity
from samsara.tts.coordinator import SpeechWindow


def _consumer(monkeypatch, callback=None):
    delivered, closed, workers = [], [], []
    app = SimpleNamespace(
        command_mode_active=True,
        config={"command_mode": {"mode": "toggle"}},
        _handle_command_mode_utterance=lambda frames, rate, **kw: delivered.append((frames, rate, kw)),
        _close_hands_free_capture_duck=lambda token: closed.append(token),
    )
    if callback is not None:
        app._capture_interact_onset = callback
    consumer = object.__new__(wake_module.WakeConsumer)
    consumer._app = app
    consumer._toggle_queue = collections.deque()
    consumer._toggle_queue_lock = threading.Lock()
    consumer._toggle_worker_active = False
    consumer._hands_free_capture_duck_token = None
    consumer._speech_start_ns = None
    consumer._mouth_shadow = None
    consumer._onset_context = None
    consumer._log_frame = Mock()
    monkeypatch.setattr(wake_module.thread_registry, "spawn",
                        lambda _name, worker, daemon=True: workers.append(worker))
    return consumer, delivered, closed, workers


def _context(utterance_id, speech_start_ns, observed_at_ns, pointer, revision):
    identity = WindowIdentity(pointer, 42, r"C:\Windows\explorer.exe", 123.5,
                              "CabinetWClass")
    return OnsetContext(utterance_id, speech_start_ns, observed_at_ns,
                        7, 3, 11, None, None, (pointer, 10), identity)


def test_two_captures_freeze_distinct_targets_through_delayed_fifo(monkeypatch):
    pointer = [101]
    captures = []

    def capture(**metadata):
        captures.append(metadata)
        return _context(pointer=pointer[0], revision=0, **metadata)

    consumer, delivered, closed, workers = _consumer(monkeypatch, capture)
    monkeypatch.setattr(wake_module.time, "perf_counter_ns", lambda: 1_010_000_000)
    first = consumer._capture_onset_context(1_000_000_000)
    consumer._flush(["first"], 41, last_voice_ns=1_030_000_000,
                    speech_end_ns=1_050_000_000, speech_start_ns=1_000_000_000,
                    onset_context=first)
    pointer[0] = 202
    monkeypatch.setattr(wake_module.time, "perf_counter_ns", lambda: 2_010_000_000)
    second = consumer._capture_onset_context(2_000_000_000)
    consumer._flush(["second"], 42, last_voice_ns=2_030_000_000,
                    speech_end_ns=2_050_000_000, speech_start_ns=2_000_000_000,
                    onset_context=second)
    pointer[0] = 303  # A later pointer position cannot retarget either record.

    assert len(workers) == 1
    assert consumer._toggle_queue[0].utterance_id != consumer._toggle_queue[1].utterance_id
    assert first.utterance_id != second.utterance_id
    assert [item.onset_context.pointer_identity.hwnd for item in consumer._toggle_queue] == [101, 202]
    with pytest.raises(dataclasses.FrozenInstanceError):
        consumer._toggle_queue[0].onset_context = second
    workers.pop()()
    assert [kw["onset_context"].pointer_identity.hwnd for _, _, kw in delivered] == [101, 202]
    assert [kw["speech_start_ns"] for _, _, kw in delivered] == [1_000_000_000, 2_000_000_000]
    assert [kw["last_voice_ns"] for _, _, kw in delivered] == [1_030_000_000, 2_030_000_000]
    assert closed == [41, 42]
    assert len(captures) == 2


def test_missing_callback_and_metadata_free_flush_do_not_borrow_context(monkeypatch):
    consumer, delivered, _, workers = _consumer(monkeypatch)
    assert consumer._capture_onset_context(10) is None
    consumer._onset_context = _context("old", 10, 20, 101, 0)
    consumer._flush(["legacy"], last_voice_ns=30, speech_end_ns=40)
    assert consumer._onset_context is None
    workers.pop()()
    assert delivered == [(["legacy"], 16_000, {"last_voice_ns": 30, "speech_end_ns": 40})]


def test_wrong_onset_or_failed_callback_is_missing_context(monkeypatch):
    consumer, delivered, _, workers = _consumer(monkeypatch, lambda **kw: None)
    assert consumer._capture_onset_context(10) is None
    wrong = _context("wrong", 10, 20, 101, 0)
    consumer._flush(["wrong"], speech_start_ns=11, last_voice_ns=30,
                    speech_end_ns=40, onset_context=wrong)
    workers.pop()()
    assert "onset_context" not in delivered[0][2]


def test_observation_delay_and_stale_epochs_fail_closed(monkeypatch):
    consumer, _, _, _ = _consumer(
        monkeypatch, lambda **kw: _context(pointer=101, revision=0, **kw))
    ticks = iter((1_010_000_000, 1_150_000_000))
    monkeypatch.setattr(wake_module.time, "perf_counter_ns", lambda: next(ticks))
    late = consumer._capture_onset_context(1_000_000_000)
    assert late.observed_at_ns - late.speech_start_ns == 150_000_000
    controller = object.__new__(InteractController)
    controller._session_epoch = lambda: 7
    controller._policy_generation = lambda: 11
    controller.state = InteractState(arm_epoch=3)
    controller.subject = None
    assert not controller._fresh(late, armed=False)
    fresh = dataclasses.replace(late, observed_at_ns=1_010_000_000)
    assert controller._fresh(fresh, armed=False)
    assert not controller._fresh(dataclasses.replace(fresh, session_epoch=6), armed=False)
    assert not controller._fresh(dataclasses.replace(fresh, arm_epoch=2), armed=False)


def test_abort_and_stop_clear_active_context(monkeypatch):
    consumer, _, closed, _ = _consumer(monkeypatch)
    consumer._utterance_frames = ["audio"]
    consumer._buffer_rms_history = [0.1]
    consumer._last_voice_ns = 30
    consumer._speech_start_ns = 10
    consumer._onset_context = _context("one", 10, 20, 101, 0)
    consumer._hands_free_capture_duck_token = 41
    consumer._app.is_speaking = True
    consumer._app._vad_reset = Mock()
    consumer.abort_utterance()
    assert consumer._onset_context is None and consumer._speech_start_ns is None
    assert closed == [41]
    consumer._onset_context = _context("two", 50, 60, 202, 0)
    consumer._speech_start_ns = 50
    consumer._utterance_frames = ["later"]
    consumer._lifecycle_lock = threading.RLock()
    consumer._stop_event = threading.Event()
    consumer._thread = None
    consumer._running = True
    result = consumer.stop()
    assert result.stopped and result == ["later"]
    assert consumer._onset_context is None and consumer._speech_start_ns is None


def test_dictate_arm_guard_uses_real_playback_window_without_importing_app():
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "DictationApp")
    method = next(node for node in app_class.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_handle_command_mode_utterance")
    guard = next(node for node in ast.walk(method) if isinstance(node, ast.If)
                 and "_was_dictate_lane and normalize_utterance(text)" in ast.unparse(node.test))
    function = ast.parse("def check():\n    return 'admitted'\n").body[0]
    function.body = [guard, ast.Return(value=ast.Constant("admitted"))]
    namespace = {"normalize_utterance": normalize_utterance}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 "<interact-onset-guard-ast>", "exec"), namespace)
    coordinator = SimpleNamespace(speech_windows=lambda: (SpeechWindow(100, 300),))
    app = SimpleNamespace(_show_outcome_chip=Mock())
    namespace.update(
        _was_dictate_lane=True, text="interact", speech_start_ns=150, last_voice_ns=250,
        _self_hear=is_self_hear_capture(coordinator, 150, 250), stage_timing=Mock(),
        _timing_outcome="", self=app,
    )
    assert namespace["check"]() is None
    app._show_outcome_chip.assert_called_once()
    assert namespace["_self_hear"] is True
    namespace["_self_hear"] = is_self_hear_capture(coordinator, 301, 320)
    namespace["speech_start_ns"] = 301
    namespace["last_voice_ns"] = 320
    assert namespace["check"]() == "admitted"
    namespace["speech_start_ns"] = None
    assert namespace["check"]() is None
