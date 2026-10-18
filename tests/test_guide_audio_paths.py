"""Guide windows must not fight the AudioCaptureEngine for the microphone.

These tests pin the shared ring-read path (samsara.audio_engine.guide_capture)
and faulthandler being armed at boot (dictation.py source, never imported).
"""
import ast
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara.audio_engine import guide_capture  # noqa: E402
from samsara.audio_engine.frame import FRAME_SIZE, SAMPLE_RATE  # noqa: E402
from samsara.audio_engine.ring import FrameBus  # noqa: E402


class _FakeEngine:
    """A real FrameBus behind the engine's consumer API."""

    def __init__(self, running=True):
        self.bus = FrameBus()
        self._running = running
        self.registered = []
        self.unregistered = []

    def register_consumer(self, name):
        reader = self.bus.new_reader(name)
        self.registered.append(name)
        return reader

    def unregister_consumer(self, reader):
        self.unregistered.append(reader)
        reader.invalidate()

    def write_frames(self, n, value=3000):
        for _ in range(n):
            self.bus.write(np.full(FRAME_SIZE, value, dtype=np.int16), 0.0, 0)


# ---------------------------------------------------------------------------
# guide_capture
# ---------------------------------------------------------------------------

class TestGuideCapture:
    def test_running_engine_only_when_running(self):
        assert guide_capture.running_engine(types.SimpleNamespace(_ace_engine=_FakeEngine())) is not None
        assert guide_capture.running_engine(types.SimpleNamespace(_ace_engine=_FakeEngine(False))) is None
        assert guide_capture.running_engine(types.SimpleNamespace()) is None
        assert guide_capture.running_engine(None) is None

    def test_ring_level_meter_reads_new_frames_and_decays(self):
        engine = _FakeEngine()
        meter = guide_capture.RingLevelMeter(engine, "test-meter")
        engine.write_frames(2, value=3276)                    # ~0.1 full scale
        level = meter.read_rms()
        assert level == pytest.approx(3276 / 32767.0, rel=1e-3)
        assert meter.read_rms() == pytest.approx(level * guide_capture.LEVEL_DECAY)
        meter.close()
        meter.close()                                         # idempotent
        assert len(engine.unregistered) == 1

    def test_record_from_ring_returns_exactly_the_requested_16k_audio(self):
        engine = _FakeEngine()
        writes = iter(range(100))

        def sleep(_s):
            next(writes)
            engine.write_frames(10)                           # 1 s of audio per poll

        audio = guide_capture.record_from_ring(engine, 5.0, "test-rec", sleep=sleep)
        assert audio.dtype == np.float32
        assert audio.size == 5 * SAMPLE_RATE
        assert engine.unregistered, "the ring consumer is always released"

    def test_record_from_ring_raises_when_audio_stalls(self):
        engine = _FakeEngine()
        now = [0.0]

        def clock():
            return now[0]

        def sleep(s):
            now[0] += 1.0

        with pytest.raises(TimeoutError):
            guide_capture.record_from_ring(engine, 5.0, "test-rec", stall_timeout=2.0,
                                           clock=clock, sleep=sleep)
        assert engine.unregistered

    def test_to_model_rate(self):
        audio = np.ones(48000, dtype=np.float32)
        assert guide_capture.to_model_rate(audio, 48000).size == SAMPLE_RATE
        assert guide_capture.to_model_rate(audio[:16000], SAMPLE_RATE).size == SAMPLE_RATE

    def test_detect_capture_rate_uses_the_devices_own_rate(self, monkeypatch):
        from samsara import audio_devices
        monkeypatch.setattr(audio_devices, "get_device_info",
                            lambda device, kind=None: {"default_samplerate": 44100.0})
        assert audio_devices.detect_capture_rate(5) == 44100
        monkeypatch.setattr(audio_devices, "get_device_info",
                            Mock(side_effect=RuntimeError("gone")))
        from samsara.constants import DEFAULT_CAPTURE_RATE
        assert audio_devices.detect_capture_rate(5) == DEFAULT_CAPTURE_RATE


# ---------------------------------------------------------------------------
# faulthandler on boot (dictation.py read as source, never imported)
# ---------------------------------------------------------------------------

def _dictation_tree():
    source = (ROOT / "dictation.py").read_text(encoding="utf-8-sig")
    return ast.parse(source)


def _load_enable_faulthandler():
    # The function moved to samsara/boot.py in 27; the call stays in dictation.py.
    tree = ast.parse((ROOT / "samsara" / "boot.py").read_text(encoding="utf-8-sig"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_enable_faulthandler")
    namespace = {"os": os, "sys": sys, "time": time,
                 "migrate_before_boot": lambda: None}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "samsara/boot.py", "exec"), namespace)
    return namespace["_enable_faulthandler"]


class TestFaulthandlerOnBoot:
    def test_enable_writes_to_logs_faulthandler_log(self, tmp_path, monkeypatch):
        import faulthandler
        enable = Mock()
        monkeypatch.setattr(faulthandler, "enable", enable)
        fh = _load_enable_faulthandler()(tmp_path / "logs")
        try:
            assert fh is not None
            assert Path(fh.name) == tmp_path / "logs" / "faulthandler.log"
            enable.assert_called_once_with(file=fh, all_threads=True)
            assert "Samsara start pid=" in (tmp_path / "logs" / "faulthandler.log").read_text(encoding="utf-8")
        finally:
            fh.close()

    def test_failure_never_raises(self, monkeypatch):
        import faulthandler
        monkeypatch.setattr(faulthandler, "enable", Mock(side_effect=RuntimeError("no")))
        bad_dir = Mock()
        bad_dir.mkdir.side_effect = PermissionError("denied")
        assert _load_enable_faulthandler()(bad_dir) is None

    def test_armed_at_module_level_before_the_native_imports(self):
        tree = _dictation_tree()
        armed = next(n.lineno for n in tree.body if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", None) == "_FAULTHANDLER_FILE" for t in n.targets))
        native = next(n.lineno for n in tree.body if isinstance(n, ast.Import)
                      and any(a.name == "sounddevice" for a in n.names))
        assert armed < native
