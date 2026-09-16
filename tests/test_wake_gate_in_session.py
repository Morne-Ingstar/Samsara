"""In-session wake RMS gate (2026-09-14 live-log incident).

Quick dictation opened on "jarvis. dictate.", then every in-session capture
logged `[WAKE] gated (rms 0.0042 .. 0.0105 < adaptive 0.0132 [floor 0.0088
x1.5]) -- skipping`: nothing reached Whisper, so no end/cancel/abort word
could ever match and the session had to be killed by hand.

FENCE: this file must never `import dictation` -- that attaches a second
RotatingFileHandler to the owner's live log if Samsara is running. The real
DictationApp methods under test are extracted from dictation.py's source with
`ast` and bound onto a small harness class instead.
"""
import ast
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import config_defaults
from samsara.audio_engine.wake_dispatch import TranscriptionOwners
from samsara.constants import ADAPTIVE_SPEECH_FLOOR_RATIO, DEFAULT_SPEECH_THRESHOLD

DICTATION_SRC = Path(__file__).resolve().parent.parent / "dictation.py"

_METHODS = ("_wake_audio_is_below_gate", "_decode_wake_word_buffer")
_CONSTANTS = (
    "_NOISE_FLOOR_ALPHA", "_NOISE_FLOOR_SPEECH_RATIO", "_NOISE_FLOOR_MIN",
    "_SPEECH_FLOOR_RATIO", "_ABS_FLOOR_MIN",
)

_LOGGER = logging.getLogger("test_wake_gate_in_session.dictation")


def _load_dictation_methods():
    """Compile the real gate/decode methods and gate constants from source."""
    tree = ast.parse(DICTATION_SRC.read_text(encoding="utf-8"), filename=str(DICTATION_SRC))
    nodes = []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in _CONSTANTS):
            nodes.append(node)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    found = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in _METHODS}
    assert set(found) == set(_METHODS), f"missing methods in dictation.py: {set(_METHODS) - set(found)}"
    nodes.extend(found[name] for name in _METHODS)
    namespace = {
        "__name__": "dictation_extract",
        "np": np,
        "re": re,
        "time": time,
        "logging": logging,
        "logger": _LOGGER,
        "config_defaults": config_defaults,
        "ADAPTIVE_SPEECH_FLOOR_RATIO": ADAPTIVE_SPEECH_FLOOR_RATIO,
        "DEFAULT_SPEECH_THRESHOLD": DEFAULT_SPEECH_THRESHOLD,
        "resample_audio": lambda audio, src, dst: audio,
        "flight_recorder": Mock(),
        "diagnostics": Mock(),
        "pyautogui": Mock(),
        "_WAKE_SESSION_SEND_WORDS": ["send"],
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(DICTATION_SRC), "exec"), namespace)
    return namespace


_NS = _load_dictation_methods()
_ADAPTIVE_FLOOR = 0.0088  # the live-log floor at decision time
_ADAPTIVE_THRESHOLD = max(_ADAPTIVE_FLOOR * _NS["_SPEECH_FLOOR_RATIO"], _NS["_ABS_FLOOR_MIN"])
# The asleep path folds the buffer into the EMA BEFORE computing the gate, so
# seed the pre-update floor that lands on exactly 0.0088 for a 0.005 buffer.
_SPEECH_RMS = 0.005
_ALPHA = _NS["_NOISE_FLOOR_ALPHA"]
_PRE_FLOOR = (_ADAPTIVE_FLOOR - _ALPHA * _SPEECH_RMS) / (1 - _ALPHA)


class _Harness:
    _wake_audio_is_below_gate = _NS["_wake_audio_is_below_gate"]
    _decode_wake_word_buffer = _NS["_decode_wake_word_buffer"]

    def __init__(self, *, app_state, transcript="hello world", floor=_PRE_FLOOR, audio_cfg=None):
        self.capture_rate = 16000
        self.model_rate = 16000
        self.config = {"wake_word_config": {"audio": dict(audio_cfg or {})}}
        self.app_state = app_state
        self.wake_word_triggered = False
        self._wake_noise_floor = floor
        self._wake_gate_frozen = lambda: False
        self._vad_available = True
        self._vad_reset = Mock()
        self._transcription_owners = TranscriptionOwners()
        self.model_lock = threading.Lock()
        segment = SimpleNamespace(text=transcript)
        self.model = SimpleNamespace(transcribe=Mock(return_value=([segment], SimpleNamespace(language="en"))))
        self.get_transcription_params = lambda include_vocabulary=False: {}
        self._filter_dictation_language = lambda text, info: text
        self.voice_training_window = SimpleNamespace(apply_corrections=lambda text: text)
        self._emit_wake_trace = Mock()
        self._log_history = Mock()
        self.play_sound = Mock()
        self._output_dictation = Mock()
        self._reset_wake_dictation = Mock()
        self._end_wake_session = Mock()
        self._restart_dictation_timer = Mock()
        self._dictation_require_end = False
        self._dictation_paused = False
        self.wake_dictation_buffer = []


def _buffer(rms):
    """One 1-second 16 kHz buffer whose whole-buffer RMS is `rms`."""
    return [np.full(16000, rms, dtype=np.float32)]


def test_adaptive_threshold_matches_live_log():
    assert _ADAPTIVE_THRESHOLD == pytest.approx(0.0132)


def test_regression_in_session_capture_below_adaptive_gate_is_transcribed_not_skipped(caplog):
    """THE 2026-09-14 regression: rms 0.005 inside quick dictation, adaptive
    threshold 0.0132 -> must reach Whisper and be buffered."""
    app = _Harness(app_state="quick_dictation")
    with caplog.at_level(logging.DEBUG):
        app._decode_wake_word_buffer(_buffer(0.005), src_rate=16000)

    app.model.transcribe.assert_called_once()
    assert app.wake_dictation_buffer == ["hello world"]
    assert not any("[WAKE] gated" in r.getMessage() for r in caplog.records)
    decision = [r.getMessage() for r in caplog.records if "[WAKE-GATE]" in r.getMessage()]
    assert decision and "rule=in_session_near_silence" in decision[0]
    assert "rms=0.0050" in decision[0] and "threshold=0.0020" in decision[0] and "decision=pass" in decision[0]
    # In-session speech must not be learned as ambient noise.
    assert app._wake_noise_floor == _PRE_FLOOR


def test_asleep_capture_at_0005_is_still_gated_exactly_as_before(caplog):
    app = _Harness(app_state="asleep")
    with caplog.at_level(logging.DEBUG):
        app._decode_wake_word_buffer(_buffer(0.005), src_rate=16000)

    app.model.transcribe.assert_not_called()
    gated = [r.getMessage() for r in caplog.records if "[WAKE] gated" in r.getMessage()]
    assert gated and "adaptive 0.0132" in gated[0] and "floor 0.0088 x1.5" in gated[0]
    assert not any("[WAKE-GATE]" in r.getMessage() for r in caplog.records)
    # Floor EMA still learns from asleep ambient buffers, unchanged formula.
    assert app._wake_noise_floor == pytest.approx(_ADAPTIVE_FLOOR, rel=1e-6)


def test_end_word_at_0005_inside_open_session_ends_the_session():
    app = _Harness(app_state="quick_dictation", transcript="that is all. over.")
    app._decode_wake_word_buffer(_buffer(0.005), src_rate=16000)

    app.model.transcribe.assert_called_once()
    app._output_dictation.assert_called_once_with("that is all.")
    app._reset_wake_dictation.assert_called_once_with()


def test_near_silence_inside_open_session_is_still_skipped(caplog):
    app = _Harness(app_state="long_dictation")
    with caplog.at_level(logging.DEBUG):
        app._decode_wake_word_buffer(_buffer(0.001), src_rate=16000)

    app.model.transcribe.assert_not_called()
    decision = [r.getMessage() for r in caplog.records if "[WAKE-GATE]" in r.getMessage()]
    assert decision, "an in-session skip must never be silent"
    assert "rms=0.0010" in decision[0] and "threshold=0.0020" in decision[0]
    assert "rule=in_session_near_silence" in decision[0] and "decision=skip" in decision[0]


def test_config_key_false_restores_adaptive_gate_in_session_and_logs_it(caplog):
    app = _Harness(
        app_state="quick_dictation",
        audio_cfg={"bypass_adaptive_gate_in_session": False},
    )
    with caplog.at_level(logging.DEBUG):
        app._decode_wake_word_buffer(_buffer(0.005), src_rate=16000)

    app.model.transcribe.assert_not_called()
    decision = [r.getMessage() for r in caplog.records if "[WAKE-GATE]" in r.getMessage()]
    assert decision and "rule=adaptive" in decision[0] and "threshold=0.0132" in decision[0]
    assert "decision=skip" in decision[0]
