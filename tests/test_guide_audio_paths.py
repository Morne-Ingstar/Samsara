"""Guide windows must not fight the AudioCaptureEngine for the microphone.

2026-09-13 live log: voice training opened its own 16 kHz PortAudio stream
on a Focusrite that ACE already held -> PortAudioError -9997, a silent 0%
meter. These tests pin the ring-read path (samsara.audio_engine.guide_capture),
voice training's use of it, visible errors, real buttons, and faulthandler
being armed at boot (checked from dictation.py's source, never imported).
"""
import ast
import os
import sys
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
# Voice training
# ---------------------------------------------------------------------------

def _vt_stub(engine=None, monitoring=True):
    from samsara.ui import voice_training_qt as vt

    stub = types.SimpleNamespace()
    stub._tr = types.SimpleNamespace(
        app=types.SimpleNamespace(config={"microphone": 7}, _ace_engine=engine),
        _monitoring=monitoring,
    )
    stub._level_sig = Mock()
    stub._monitor_error_sig = Mock()
    for name in ("_monitor_worker", "_record_test_audio"):
        setattr(stub, name, getattr(vt._TrainingWindow, name).__get__(stub))
    return vt, stub


class TestVoiceTrainingAudio:
    def test_monitor_reads_the_ace_ring_and_never_opens_a_stream(self, monkeypatch):
        engine = _FakeEngine()
        vt, stub = _vt_stub(engine)
        monkeypatch.setattr(vt.sd, "InputStream", Mock(side_effect=AssertionError("second stream")))
        # A reader starts at the write head, so audio must arrive after the
        # monitor registers -- as it does live.
        register = engine.register_consumer

        def register_then_capture(name):
            reader = register(name)
            engine.write_frames(3, value=3276)
            return reader
        engine.register_consumer = register_then_capture

        def emit(level):
            stub._tr._monitoring = False                      # one tick is enough
        stub._level_sig.emit.side_effect = emit
        monkeypatch.setattr(vt.time, "sleep", lambda _s: None)

        stub._monitor_worker()

        assert stub._level_sig.emit.call_count == 1
        assert stub._level_sig.emit.call_args.args[0] > 0
        stub._monitor_error_sig.emit.assert_not_called()
        assert engine.registered == ["voice-training-monitor"]
        assert len(engine.unregistered) == 1

    def test_without_ace_the_fallback_stream_uses_the_device_rate(self, monkeypatch):
        vt, stub = _vt_stub(engine=None)
        opened = []

        def fake_stream(**kwargs):
            opened.append(kwargs)
            raise vt.sd.PortAudioError("Invalid sample rate [-9997]")

        monkeypatch.setattr(vt, "detect_capture_rate", lambda device: 48000)
        monkeypatch.setattr(vt.sd, "InputStream", fake_stream)

        stub._monitor_worker()

        assert opened and opened[0]["samplerate"] == 48000 and opened[0]["device"] == 7
        stub._monitor_error_sig.emit.assert_called_once()
        assert "-9997" in stub._monitor_error_sig.emit.call_args.args[0]

    def test_stream_error_is_shown_in_the_window_not_a_silent_zero(self, qapp):
        from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton
        from samsara.ui import voice_training_qt as vt

        stub = types.SimpleNamespace(
            _tr=types.SimpleNamespace(_monitoring=True),
            _monitor_btn=QPushButton("Stop Monitoring"),
            _level_bar=QProgressBar(),
            _level_label=QLabel("Volume: 0%"),
        )
        vt._TrainingWindow._on_monitor_error(stub, "Invalid sample rate [-9997]")
        assert "Invalid sample rate" in stub._level_label.text()
        assert vt._ERROR in stub._level_label.styleSheet()
        assert stub._tr._monitoring is False
        assert stub._monitor_btn.text() == "Start Monitoring"

    def test_test_phrase_records_five_seconds_from_the_ring(self, monkeypatch):
        engine = _FakeEngine()
        vt, stub = _vt_stub(engine)
        monkeypatch.setattr(vt.sd, "rec", Mock(side_effect=AssertionError("sd.rec with ACE running")))
        calls = []

        def fake_record(eng, seconds, name, **kw):
            calls.append((eng, seconds, name))
            return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)

        monkeypatch.setattr(vt.guide_capture, "record_from_ring", fake_record)
        audio = stub._record_test_audio(5.0)
        assert calls == [(engine, 5.0, "voice-training-test")]
        assert audio.size == 5 * SAMPLE_RATE

    def test_test_phrase_fallback_records_at_device_rate_and_resamples(self, monkeypatch):
        vt, stub = _vt_stub(engine=None)
        monkeypatch.setattr(vt, "detect_capture_rate", lambda device: 48000)
        rec = Mock(return_value=np.zeros((5 * 48000, 1), dtype=np.float32))
        monkeypatch.setattr(vt.sd, "rec", rec)
        monkeypatch.setattr(vt.sd, "wait", lambda: None)
        audio = stub._record_test_audio(5.0)
        assert rec.call_args.kwargs["samplerate"] == 48000
        assert audio.size == 5 * SAMPLE_RATE

    def test_monitor_and_test_buttons_are_real_secondary_buttons(self, qapp):
        from PySide6.QtWidgets import QPushButton, QTabWidget
        from samsara.ui import voice_training_qt as vt

        stub = types.SimpleNamespace(
            _tabs=QTabWidget(),
            _section=vt._TrainingWindow._section,
            _toggle_monitoring=lambda: None,
            _test_phrase=lambda phrase, idx: None,
        )
        vt._TrainingWindow._build_calibration_tab(stub)
        assert stub._monitor_btn.property("class") == "secondary"
        tests = [b for b in stub._tabs.widget(0).findChildren(QPushButton) if b.text() == "Test"]
        assert len(tests) == 5
        assert all(b.property("class") == "secondary" for b in tests)
        from samsara.ui import theme
        assert all(b.styleSheet() == theme._SECONDARY_BUTTON_QSS for b in tests + [stub._monitor_btn])


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
    namespace = {"os": os, "sys": sys}
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
