"""Queue 340: only Ava-owned capture is refused; a real interruption survives."""
from __future__ import annotations
from samsara.log import describe_text, safe_exception

import ast
import collections
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from samsara.audio_engine import wake_consumer as wake_module
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.session_modes import SessionMode, is_self_hear_capture
from samsara.stage_timing import StageTiming
from samsara.tts.coordinator import AudioCoordinator, SpeechWindow
from samsara.tts.engine_base import SpeechHandle


def _coordinator(grace_ms=400):
    app = SimpleNamespace(
        config={"tts": {"enabled": True, "engine": "edge",
                        "self_hear_grace_ms": grace_ms},
                "wake_word_config": {"audio": {"speech_threshold": 0.03}}},
        is_speaking=False,
    )
    engine = Mock()
    engine.speak.return_value = SpeechHandle(utterance_id="test")
    engine.get_engine_state.return_value = "playing"
    coordinator = AudioCoordinator(app, engine)
    coordinator._start_interrupt_poll = lambda: None  # deterministic poll in the barge-in test
    return coordinator, app, engine


def test_inside_speech_window_is_dropped_and_tagged(tmp_path):
    assert SETTINGS_SCHEMA["tts.self_hear_grace_ms"]["default"] == 400
    coordinator, _, _ = _coordinator()
    coordinator.speak("Ava response")
    coordinator.cancel_speech()
    window = coordinator.speech_window()
    assert window is not None
    assert is_self_hear_capture(coordinator, window.start_ns, window.end_ns - 1)

    timing = StageTiming("handsfree", "command", home_dir=tmp_path)
    timing.mark("capture_finalised", self_hear=True)
    timing.complete(outcome="gated")
    record = json.loads((tmp_path / "perf" / "stage_timing.jsonl").read_text())
    assert record["self_hear"] is True and record["outcome"] == "gated"


def test_after_grace_and_partial_overlap_are_kept():
    coordinator, _, _ = _coordinator(grace_ms=400)
    coordinator.speak("Ava response")
    coordinator.cancel_speech()
    window = coordinator.speech_window()
    assert not is_self_hear_capture(coordinator, window.end_ns + 1, window.end_ns + 2)
    assert not is_self_hear_capture(coordinator, window.start_ns - 1, window.start_ns + 1)


def test_interrupt_poll_cancels_tts_and_keeps_barge_in():
    coordinator, app, engine = _coordinator()
    coordinator.speak("Ava response", interruptible=True)
    start_ns = coordinator.speech_window().start_ns
    app.is_speaking = True
    coordinator._interrupt_poll_active = True
    coordinator._interrupt_grace_active = False
    coordinator._interrupt_poll_loop()

    engine.cancel_all.assert_called_once()
    window = coordinator.speech_window()
    assert window.interrupted is True
    assert not is_self_hear_capture(coordinator, start_ns, window.end_ns - 1)


def test_speech_window_covers_all_queued_playback_callbacks():
    coordinator, _, engine = _coordinator()
    callbacks = []
    engine.speak.side_effect = lambda _text, **kwargs: (
        callbacks.append(kwargs["on_done"]) or SpeechHandle(utterance_id=str(len(callbacks))))
    coordinator.speak("first")
    coordinator.speak("second")
    callbacks[0]()
    assert coordinator.is_speaking
    window = coordinator.speech_window()
    assert is_self_hear_capture(coordinator, window.start_ns, window.end_ns - 1)
    callbacks[1]()
    assert not coordinator.is_speaking


def test_fifo_delivers_real_vad_onset_without_changing_old_callers(monkeypatch):
    consumer = object.__new__(wake_module.WakeConsumer)
    delivered = []
    consumer._app = SimpleNamespace(
        command_mode_active=True, config={"command_mode": {"mode": "toggle"}},
        _handle_command_mode_utterance=lambda _buffer, _rate, **kw: delivered.append(kw),
    )
    consumer._hands_free_capture_duck_token = None
    consumer._toggle_queue = collections.deque()
    consumer._toggle_queue_lock = threading.Lock()
    consumer._toggle_worker_active = False
    monkeypatch.setattr(wake_module.thread_registry, "spawn",
                        lambda _name, target, daemon=True: target())
    consumer._flush([object()], last_voice_ns=30, speech_end_ns=40, speech_start_ns=10)
    assert delivered == [{"last_voice_ns": 30, "speech_end_ns": 40,
                          "speech_start_ns": 10}]


def test_real_command_handler_drops_before_whisper_without_importing_app():
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "DictationApp")
    method = next(node for node in app_class.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_handle_command_mode_utterance")
    timing = Mock()
    flight = SimpleNamespace(record=Mock())
    namespace = {
        "describe_text": describe_text, "safe_exception": safe_exception,
        "SessionMode": SessionMode,
        "StageTiming": SimpleNamespace(create=lambda *a, **kw: timing),
        "set_current_stage_timing": lambda _timing: object(),
        "reset_current_stage_timing": lambda _token: None,
        "np": np,
        "resample_audio": lambda audio, _src, _dst: audio,
        "is_self_hear_capture": is_self_hear_capture,
        "flight_recorder": flight,
        "logger": Mock(),
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    app = SimpleNamespace(
        _session_mode_manager=SimpleNamespace(mode=SessionMode.COMMAND),
        _stage_timing_enabled=True, _command_mode_session_start=1,
        _stage_timing_segment_session=1, _stage_timing_segment_index=0,
        _transcription_owners=Mock(), model_rate=16_000,
        audio_coordinator=SimpleNamespace(
            speech_windows=lambda: (SpeechWindow(100, 300),)),
        _show_outcome_chip=Mock(), _vad_reset=Mock(), model=Mock(),
    )
    namespace["_handle_command_mode_utterance"](
        app, [np.zeros(16_000, dtype=np.float32)], 16_000,
        speech_start_ns=150, last_voice_ns=250, speech_end_ns=260,
    )
    app.model.transcribe.assert_not_called()
    app._show_outcome_chip.assert_called_once_with("ignored - Ava was speaking", "warning")
    timing.mark.assert_any_call("capture_finalised", self_hear=True)
    timing.complete.assert_called_once_with(outcome="gated")
    assert flight.record.call_args.kwargs["self_hear"] is True
