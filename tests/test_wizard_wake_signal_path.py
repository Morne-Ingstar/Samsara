"""Queue 67: the mic setup guide's wake-word test must score the signal the
live wake detector scores.

2026-09-15 02:09, wake mode on while the guide's test ran: the live detector
scored the owner's "Jarvis" 0.977 and 0.436 while the guide, over the same
seconds, scored 0.007 / 0.018 / 0.058. The guide had its own blocking
sounddevice stream (channels=1 on a 2-channel WASAPI endpoint, which returned
samples with their time structure destroyed), its own linear-interpolation
resampler and its own copy of the gain. It now reads the engine ring and calls
the live consumer's own pre-filter.

Never imports dictation.py. The recorded-utterance tests use the real
OpenWakeWord model and skip when it (or the owner's wake clips) is absent.
"""
import ast
import glob
import os
import threading
import time as _real_time
import types
import wave
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine import wake_consumer as wake_module
from samsara.audio_engine.frame import FRAME_SIZE, SAMPLE_RATE
from samsara.audio_engine.ring import FrameBus

ROOT = Path(__file__).resolve().parent.parent
WIZARD_SRC = ROOT / "samsara" / "ui" / "mic_setup_wizard_qt.py"
CONSUMER_SRC = ROOT / "samsara" / "audio_engine" / "wake_consumer.py"
THRESHOLD = 0.2
#: Same frames, same pre-filter, detectors from the same (reset) state: the
#: two paths must agree to float noise.
SAME_STATE_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _ScoreRecorder:
    """Wraps a real WakeWordDetector; records every score it produces, in
    both the live consumer's detected() form and the guide's process_audio()."""

    def __init__(self, detector):
        self._detector = detector
        self.is_available = detector.is_available
        self._threshold = THRESHOLD
        self.scores = []
        self.resets = 0

    def process_audio(self, chunk):
        score = float(self._detector.process_audio(chunk))
        self.scores.append(score)
        return score

    def detected(self, chunk):
        return self.process_audio(chunk) >= THRESHOLD

    def reset(self):
        self.resets += 1
        self._detector.reset()


def _real_detector():
    pytest.importorskip("openwakeword")
    from samsara.wake_detector import WakeWordDetector
    # openwakeword seeds a new model's feature buffer with np.random noise;
    # left unseeded, two fresh detectors differ by up to ~1e-4 in their first
    # second of scores whatever they are fed. Same seed = same start state, so
    # any difference left is the signal path's.
    np.random.seed(67)
    det = WakeWordDetector("jarvis", threshold=THRESHOLD)
    if not det.is_available:
        pytest.skip("hey_jarvis OpenWakeWord model not available")
    return det


def _frames_from_wav(path):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getsampwidth() == 2 and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    n = len(pcm) // FRAME_SIZE
    # one second of near-silence either side, so the utterance is inside the
    # detector's window whatever the clip's own padding
    pad = [np.zeros(FRAME_SIZE, dtype=np.int16)] * 10
    return pad + [pcm[i * FRAME_SIZE:(i + 1) * FRAME_SIZE].copy() for i in range(n)] + pad


def _owner_wake_clips():
    home = Path(os.path.expanduser("~"))
    found = []
    for base in (home / ".samsara-daily" / "debug_audio", home / ".samsara" / "debug_audio"):
        found.extend(sorted(glob.glob(str(base / "wake_*.wav"))))
    return found


def _prefix_to_first_hit(scores):
    """Scores up to and including the first one at/above threshold. Past it
    the live consumer resets its detector at once and the guide only after
    re-arming, by design, so later frames are not comparable."""
    for i, s in enumerate(scores):
        if s >= THRESHOLD:
            return scores[:i + 1]
    return scores


def _live_path_scores(frames, detector):
    """Feed ring frames through the real WakeConsumer._process_frame."""
    app = types.SimpleNamespace(
        config={}, app_state="asleep", _hotkey_recording=False,
        command_mode_active=False, ava_command_session_active=False,
        wake_word_active=True, wake_word_triggered=False, continuous_active=True,
        _oww_wake_detected=False, _wake_detector=detector, _wake_profile_detectors={},
        _tts_last_speaking=-100.0, _command_executed_at=None, audio_coordinator=None,
        echo_canceller=types.SimpleNamespace(is_active=False),
        is_speaking=False, silence_start=None, _vad_available=False,
        _vad_is_speech=Mock(return_value=False), _vad_reset=Mock(),
        _dictation_silence_timeout=None, _session_mode_manager=None,
        _wake_rearm_needs_onset=False, _wake_consumer_reasons={"wake_word"},
        _open_hands_free_capture_duck=Mock(return_value=41),
        _close_hands_free_capture_duck=Mock(), play_sound=Mock(),
        _expire_wake_session=Mock(), _end_wake_session=Mock(),
        exit_command_mode=Mock(), exit_ava_command_session=Mock(), set_app_state=Mock(),
    )
    bus = FrameBus()
    reader = bus.new_reader()
    engine = types.SimpleNamespace(register_consumer=lambda name: reader, unregister_consumer=Mock())
    consumer = wake_module.WakeConsumer(engine, app)
    consumer._flush = Mock()
    for pcm in frames:
        bus.write(pcm, t_capture=_real_time.monotonic(), device_epoch=0)
        consumer._process_frame(reader.read_next())
    return detector.scores


class _GuideApp:
    def __init__(self, engine=None):
        self.config = {"microphone": 30, "microphone_name": "Mic",
                       "wake_word_enabled": True,
                       "wake_word_config": {"phrase": "jarvis", "oww_threshold": THRESHOLD}}
        self._ace_engine = engine

    def get_available_microphones(self):
        return [{"id": 30, "name": "Mic"}]


class _RingEngine:
    """The engine surface guide_capture.running_engine and the guide use."""

    _running = True

    def __init__(self):
        self.bus = FrameBus()
        self.registered = []
        self.unregistered = []

    def register_consumer(self, name):
        reader = self.bus.new_reader(name)
        self.registered.append(name)
        return reader

    def unregister_consumer(self, reader):
        self.unregistered.append(reader)
        reader.invalidate()


def _guide_window(monkeypatch, engine):
    from samsara.ui import mic_setup_wizard_qt as wizard
    monkeypatch.setattr(wizard._WizardWindow, "_ensure_audio_running", lambda self: None)
    window = wizard._WizardWindow(_GuideApp(engine))
    return wizard, window


def _guide_path_scores(monkeypatch, frames, detector):
    """Run the guide's real audio worker on the wake step, reading the ring."""
    engine = _RingEngine()
    wizard, window = _guide_window(monkeypatch, engine)
    monkeypatch.setattr(wizard.sd, "InputStream",
                        Mock(side_effect=AssertionError("the wake step must read the engine ring")))
    window._current_step = window._STEP_WAKE
    window._oww_detector = detector
    window._oww_threshold = THRESHOLD
    window._oww_levels = wizard._derive_wake_levels(0.00125)
    window._oww_armed = True
    window._oww_running = True
    window._wizard_active = True
    pending = list(frames)

    def fake_sleep(_s):
        # the worker found the ring empty: publish the next frame, or stop
        if pending:
            engine.bus.write(pending.pop(0), t_capture=_real_time.monotonic(), device_epoch=0)
        else:
            window._wizard_active = False

    monkeypatch.setattr(wizard.time, "sleep", fake_sleep)
    window._audio_worker()
    window._oww_running = False
    window.close()
    assert engine.registered == ["mic-setup-guide"] and len(engine.unregistered) == 1
    return detector.scores, window


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------

def _synthetic_utterance_frames():
    """A deterministic non-trivial signal (voiced harmonics + noise bursts)
    for when the owner's clips are not on this machine."""
    rng = np.random.default_rng(67)
    t = np.arange(FRAME_SIZE * 30) / SAMPLE_RATE
    env = np.exp(-((t - 1.5) / 0.35) ** 2)
    sig = env * (0.2 * np.sin(2 * np.pi * 140 * t) + 0.1 * np.sin(2 * np.pi * 280 * t)
                 + 0.05 * rng.standard_normal(len(t)))
    pcm = np.clip(sig * 32767, -32768, 32767).astype(np.int16)
    return [pcm[i * FRAME_SIZE:(i + 1) * FRAME_SIZE] for i in range(30)]


def test_67_wizard_wake_score_matches_live_detector_on_the_same_utterance(qapp, monkeypatch):
    """The acceptance criterion: one recorded utterance, fed as ring frames to
    the live WakeConsumer and to the guide's wake step, gets the same scores
    (within SAME_STATE_TOLERANCE) up to the first detection."""
    clips = _owner_wake_clips()
    frames = _frames_from_wav(clips[0]) if clips else _synthetic_utterance_frames()

    live = _prefix_to_first_hit(_live_path_scores(frames, _ScoreRecorder(_real_detector())))
    guide_scores, _ = _guide_path_scores(monkeypatch, frames, _ScoreRecorder(_real_detector()))
    guide = _prefix_to_first_hit(guide_scores)

    assert len(live) == len(guide) and len(live) > 0
    assert np.max(np.abs(np.array(live) - np.array(guide))) <= SAME_STATE_TOLERANCE
    assert abs(max(live) - max(guide)) <= SAME_STATE_TOLERANCE


def test_owner_wake_clips_the_live_detector_clears_also_clear_the_wizard(qapp, monkeypatch):
    """The owner's own live wake buffers (~/.samsara-daily/debug_audio or
    ~/.samsara/debug_audio). Not every dumped buffer is a detection -- the
    dumps include buffers OWW scored low -- so the requirement is: every clip
    the live detector clears, the guide clears too, at the same peak score."""
    clips = _owner_wake_clips()
    if not clips:
        pytest.skip("no owner wake clips on this machine")
    cleared = 0
    for path in clips:
        frames = _frames_from_wav(path)
        live = _prefix_to_first_hit(_live_path_scores(frames, _ScoreRecorder(_real_detector())))
        guide_scores, _ = _guide_path_scores(monkeypatch, frames, _ScoreRecorder(_real_detector()))
        guide = _prefix_to_first_hit(guide_scores)
        name = os.path.basename(path)
        assert abs(max(live) - max(guide)) <= SAME_STATE_TOLERANCE, name
        if max(live) >= THRESHOLD:
            cleared += 1
            assert max(guide) >= THRESHOLD, f"{name}: live {max(live):.3f}, guide {max(guide):.3f}"
    assert cleared > 0, "no clip cleared the live detector; nothing was proven"


def test_the_wake_step_counts_a_ring_detection_as_heard(qapp, monkeypatch):
    engine = _RingEngine()
    wizard, window = _guide_window(monkeypatch, engine)
    detector = types.SimpleNamespace(process_audio=Mock(return_value=0.9), reset=Mock())
    window._current_step = window._STEP_WAKE
    window._oww_detector = detector
    window._oww_levels = wizard._derive_wake_levels(None)
    window._oww_armed = True
    window._oww_running = True
    hits = []
    window._oww_hit_sig.connect(lambda: hits.append(True))
    window._on_capture_frame(np.full(FRAME_SIZE, 0.02, dtype=np.float32))
    assert hits == [True]
    fed = detector.process_audio.call_args[0][0]
    assert abs(float(np.sqrt(np.mean(fed ** 2))) - 0.10) < 1e-3   # the live gain, applied once


# ---------------------------------------------------------------------------
# where the guide gets its audio
# ---------------------------------------------------------------------------

def test_level_and_wake_steps_read_the_ring_and_open_no_stream(qapp, monkeypatch):
    engine = _RingEngine()
    wizard, window = _guide_window(monkeypatch, engine)
    monkeypatch.setattr(wizard.sd, "InputStream", Mock(side_effect=AssertionError("second stream")))
    for step in (window._STEP_LEVEL, window._STEP_WAKE):
        window._current_step = step
        assert window._ring_engine() is engine


def test_device_preview_and_no_engine_use_the_guides_own_stream(qapp, monkeypatch):
    engine = _RingEngine()
    _, window = _guide_window(monkeypatch, engine)
    window._current_step = window._STEP_DEVICE
    assert window._ring_engine() is None
    engine._running = False
    window._current_step = window._STEP_WAKE
    assert window._ring_engine() is None


def test_own_stream_opens_every_endpoint_channel_and_uses_the_first(qapp, monkeypatch):
    """A blocking channels=1 read on the owner's 2-channel WASAPI endpoint
    destroyed the signal; channels=2 column 0 matched the engine exactly."""
    wizard, window = _guide_window(monkeypatch, None)
    opened = []
    left = np.full(1600, 0.03, dtype=np.float32)

    class _Stream:
        def __init__(self, **kwargs):
            opened.append(kwargs)

        def start(self):
            pass

        def read(self, blocksize):
            window._wizard_active = False
            return np.stack([left[:blocksize], np.zeros(blocksize, dtype=np.float32)], axis=1), False

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(wizard.sd, "InputStream", _Stream)
    monkeypatch.setattr(wizard.sd, "query_devices",
                        lambda device, kind=None: {"max_input_channels": 2, "default_samplerate": 16000})
    monkeypatch.setattr(wizard, "_detect_capture_rate", lambda device: 16000)
    levels = []
    window._level_sig.connect(levels.append)
    window._current_step = window._STEP_LEVEL
    window._wizard_active = True
    window._audio_worker()
    assert opened[0]["channels"] == 2
    assert levels and abs(levels[0] - 0.03) < 1e-3          # column 0, not a downmix with the silent input


def test_fallback_conversion_is_the_engines_own_callback(monkeypatch):
    """NativeRateFrames must hand the guide exactly the frame the engine's
    callback writes to the ring for the same native-rate block."""
    import sounddevice
    from samsara.audio_engine.engine import AudioCaptureEngine
    from samsara.audio_engine.wake_prefilter import NativeRateFrames, pcm_to_float

    monkeypatch.setattr(sounddevice, "query_devices",
                        lambda device=None, kind=None: {"default_samplerate": 44100, "name": "x"})
    monkeypatch.setattr(sounddevice, "InputStream",
                        lambda **kw: types.SimpleNamespace(start=lambda: None))
    bus = FrameBus()
    engine = AudioCaptureEngine(bus, config={})
    engine._open_stream(30)
    reader = bus.new_reader()
    rng = np.random.default_rng(1)
    conv = NativeRateFrames(44100)
    assert conv.blocksize == engine._blocksize
    for _ in range(3):
        block = (0.2 * rng.standard_normal(conv.blocksize)).astype(np.float32)
        engine._on_audio_block(block[:, None], len(block), None, None)
        ring_frame = pcm_to_float(reader.read_next().pcm)
        assert np.array_equal(conv.convert(block), ring_frame)


# ---------------------------------------------------------------------------
# regression guard: one pre-filter, no second copy
# ---------------------------------------------------------------------------

def _float_constants(tree):
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)}


def _called_names(tree):
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


def test_wizard_has_no_resampler_or_gain_constants_of_its_own():
    """Fails if the guide ever grows its own resampler or pre-OWW gain again."""
    tree = ast.parse(WIZARD_SRC.read_text(encoding="utf-8"))
    from samsara.audio_engine import wake_prefilter as wp
    gain_literals = {wp.OWW_GAIN_FLOOR_RMS, wp.OWW_TARGET_RMS, wp.OWW_MAX_GAIN}
    assert not gain_literals & _float_constants(tree), "gain literals copied into the wizard"
    funcs = {n.name.lower() for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not any("resampl" in f for f in funcs), "the wizard defines its own resampler"
    calls = _called_names(tree)
    assert not {"interp", "resample_poly", "resample", "clip"} & calls
    assert "oww_prefilter" in calls, "the wake step must call the shared pre-filter"
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and n.module == "samsara.audio_engine.wake_prefilter" for a in n.names}
    assert "oww_prefilter" in imported


def test_live_consumer_uses_the_shared_prefilter_and_holds_no_copy():
    source = CONSUMER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and getattr(n.func, "id", "") == "oww_prefilter"].__len__() >= 2   # primary + profile detectors
    assert "min(0.10" not in source and "_oww_gain" not in source
