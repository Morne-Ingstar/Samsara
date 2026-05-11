"""Unit tests for the WinRTEngine TTS subsystem.

Tests that require actual audio playback are marked @pytest.mark.audio and
skipped by default in CI. Run with:
    pytest tests/test_tts_winrt.py -m audio
or include them manually.

Tests that need WinRTEngine (which requires the winsdk package) are skipped
automatically when winsdk is not installed. The audio_utils and engine_base
tests always run since they have no OS dependencies.
"""

import io
import threading
import time
import wave
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from samsara.tts.audio_utils import parse_wav, resample_pcm
from samsara.tts.engine_base import SpeechHandle, VoiceInfo
from samsara.tts.exceptions import EngineUnavailableError

# Guard: skip any test class that directly uses WinRTEngine when winsdk is absent.
try:
    import winsdk  # noqa: F401
    from samsara.tts.winrt_engine import WinRTEngine
    _HAS_WINSDK = True
except (ImportError, Exception):
    _HAS_WINSDK = False
    WinRTEngine = None  # type: ignore

requires_winsdk = pytest.mark.skipif(
    not _HAS_WINSDK,
    reason="winsdk not installed in this Python environment"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wav_bytes(sr=16000, duration=0.1, freq=440, channels=1) -> bytes:
    """Generate a minimal sine-wave WAV byte buffer for testing."""
    n = int(sr * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    tone = (np.sin(2 * np.pi * freq * t) * 0.5 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(tone.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# audio_utils
# ---------------------------------------------------------------------------

class TestResamplePCM:
    def test_no_op_when_rates_match(self):
        arr = np.random.rand(1000).astype(np.float32)
        out = resample_pcm(arr, 44100, 44100)
        np.testing.assert_array_equal(arr, out)

    def test_doubles_length_for_2x_upsample(self):
        arr = np.random.rand(1000).astype(np.float32)
        out = resample_pcm(arr, 22050, 44100)
        # Length should approximately double (allow ±1 sample rounding)
        assert abs(len(out) - 2000) <= 2

    def test_halves_length_for_2x_downsample(self):
        arr = np.random.rand(1000).astype(np.float32)
        out = resample_pcm(arr, 44100, 22050)
        assert abs(len(out) - 500) <= 2

    def test_output_is_float32(self):
        arr = np.random.rand(500).astype(np.float32)
        out = resample_pcm(arr, 16000, 44100)
        assert out.dtype == np.float32


class TestParseWav:
    def test_extracts_correct_sample_rate(self):
        raw = _make_wav_bytes(sr=16000)
        _, sr, _ = parse_wav(raw)
        assert sr == 16000

    def test_extracts_channels(self):
        raw = _make_wav_bytes(channels=1)
        _, _, ch = parse_wav(raw)
        assert ch == 1

    def test_normalizes_to_float32(self):
        raw = _make_wav_bytes()
        arr, _, _ = parse_wav(raw)
        assert arr.dtype == np.float32
        assert arr.max() <= 1.0
        assert arr.min() >= -1.0

    def test_stereo_is_mixed_to_mono(self):
        raw = _make_wav_bytes(channels=2)
        arr, _, _ = parse_wav(raw)
        # Output should be 1D (mono)
        assert arr.ndim == 1

    def test_known_riff_header(self):
        raw = _make_wav_bytes(sr=22050)
        assert raw[:4] == b"RIFF"
        assert raw[8:12] == b"WAVE"
        _, sr, _ = parse_wav(raw)
        assert sr == 22050


# ---------------------------------------------------------------------------
# WinRTEngine initialization
# ---------------------------------------------------------------------------

@requires_winsdk
class TestWinRTEngineInit:
    def test_initializes_without_error(self):
        engine = WinRTEngine()
        assert engine is not None
        engine.shutdown()

    def test_voice_list_not_empty(self):
        engine = WinRTEngine()
        voices = engine.list_voices()
        assert len(voices) > 0
        engine.shutdown()

    def test_list_voices_returns_voiceinfo_objects(self):
        engine = WinRTEngine()
        for v in engine.list_voices():
            assert isinstance(v, VoiceInfo)
            assert v.voice_id
            assert v.display_name
            assert v.language
            assert v.gender in ("male", "female", "neutral", "unknown")
        engine.shutdown()

    def test_raises_engine_unavailable_when_winsdk_missing(self):
        with patch("samsara.tts.winrt_engine._import_winsdk") as mock_import:
            mock_import.side_effect = EngineUnavailableError("winsdk not found")
            with pytest.raises(EngineUnavailableError):
                WinRTEngine()


# ---------------------------------------------------------------------------
# speak() non-blocking contract
# ---------------------------------------------------------------------------

@requires_winsdk
class TestSpeakNonBlocking:
    def _make_engine_with_mock_playback(self):
        """Return an engine whose _playback_worker is patched to be instant."""
        engine = WinRTEngine()
        # Patch _stream_pcm so no actual OutputStream is opened.
        engine._stream_pcm = MagicMock()
        return engine

    def test_speak_returns_handle_immediately(self):
        engine = WinRTEngine()
        # Patch _synthesize and _stream_pcm so there's no real audio I/O.
        engine._synthesize = MagicMock(return_value=_make_wav_bytes())
        engine._stream_pcm = MagicMock()

        t0 = time.monotonic()
        handle = engine.speak("test phrase")
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert isinstance(handle, SpeechHandle)
        assert elapsed_ms < 50, f"speak() took {elapsed_ms:.1f} ms (should be <50 ms)"
        engine.shutdown()

    def test_speak_returns_speech_handle(self):
        engine = WinRTEngine()
        engine._synthesize = MagicMock(return_value=_make_wav_bytes())
        engine._stream_pcm = MagicMock()
        handle = engine.speak("hello")
        assert handle.utterance_id
        engine.shutdown()


# ---------------------------------------------------------------------------
# is_speaking transitions
# ---------------------------------------------------------------------------

@requires_winsdk
class TestIsSpeaking:
    def test_false_before_speak(self):
        engine = WinRTEngine()
        assert engine.is_speaking() is False
        engine.shutdown()

    def test_transitions_correctly(self):
        engine = WinRTEngine()
        started = threading.Event()
        finished = threading.Event()

        def slow_synth(text, voice_id, speed, pitch):
            started.set()
            time.sleep(0.15)
            return _make_wav_bytes(duration=0.05)

        engine._synthesize = slow_synth
        engine._stream_pcm = MagicMock(side_effect=lambda *a, **kw: None)

        handle = engine.speak("test")
        started.wait(timeout=2.0)
        assert engine.is_speaking() is True

        # Wait for playback to complete
        deadline = time.monotonic() + 2.0
        while engine.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert engine.is_speaking() is False
        engine.shutdown()


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------

@requires_winsdk
class TestCancel:
    @pytest.mark.audio
    def test_cancel_stops_long_utterance(self):
        """Requires a real sounddevice -- mark audio for CI skip."""
        engine = WinRTEngine()

        synthesis_done = threading.Event()

        def slow_synth(text, voice_id, speed, pitch):
            # Generate 5 seconds of audio so cancel has time to fire
            wav = _make_wav_bytes(duration=5.0)
            synthesis_done.set()
            return wav

        engine._synthesize = slow_synth

        handle = engine.speak("long test phrase")
        synthesis_done.wait(timeout=5.0)

        # Give the stream a moment to start
        time.sleep(0.05)
        engine.cancel(handle)

        deadline = time.monotonic() + 0.5
        while engine.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.02)

        assert not engine.is_speaking(), "Engine still speaking 500 ms after cancel()"
        engine.shutdown()

    def test_cancel_sets_handle_state(self):
        engine = WinRTEngine()
        engine._synthesize = MagicMock(return_value=_make_wav_bytes())
        engine._stream_pcm = MagicMock()

        handle = engine.speak("short phrase")
        engine.cancel(handle)
        assert handle._state == "cancelled"
        engine.shutdown()


# ---------------------------------------------------------------------------
# on_done callback
# ---------------------------------------------------------------------------

@requires_winsdk
class TestOnDoneCallback:
    def test_on_done_called_after_playback(self):
        engine = WinRTEngine()
        engine._synthesize = MagicMock(return_value=_make_wav_bytes(duration=0.01))
        engine._stream_pcm = MagicMock()

        fired = threading.Event()
        engine.speak("callback test", on_done=fired.set)
        fired.wait(timeout=5.0)
        assert fired.is_set()
        engine.shutdown()

    def test_on_done_not_called_after_cancel(self):
        engine = WinRTEngine()
        started = threading.Event()

        def slow_synth(text, voice_id, speed, pitch):
            started.set()
            time.sleep(0.3)
            return _make_wav_bytes(duration=0.01)

        engine._synthesize = slow_synth
        engine._stream_pcm = MagicMock()

        callback_fired = threading.Event()
        handle = engine.speak("cancel me", on_done=callback_fired.set)
        started.wait(timeout=2.0)
        engine.cancel(handle)

        time.sleep(0.5)
        assert not callback_fired.is_set(), "on_done should not fire after cancel"
        engine.shutdown()


# ---------------------------------------------------------------------------
# Engine unavailable
# ---------------------------------------------------------------------------

@requires_winsdk
class TestEngineUnavailable:
    def test_missing_winsdk_raises_useful_message(self):
        with patch("samsara.tts.winrt_engine._import_winsdk") as mock_import:
            mock_import.side_effect = EngineUnavailableError(
                "WinRT speech requires the winsdk package."
            )
            with pytest.raises(EngineUnavailableError, match="winsdk"):
                WinRTEngine()
