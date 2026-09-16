"""Queue 48: the mic setup guide's wake calibration.

  * the preview stream is only ever stopped/closed by its own worker thread
    (closing it from the Qt thread under a blocking read was the access
    violation that killed the app on 2026-09-14, 20:38 and 21:02)
  * a worker-thread exception during calibration is logged and surfaced, and
    nothing propagates
  * a cancelled calibration writes nothing
  * too small a sample writes nothing and says so visibly, with frame count
    and duration; a good sample shows them too

Never imports dictation.py: calibrate_wake_mic is compiled from its source
with ast and bound onto a small harness.
"""
import ast
import logging
import math
import threading
import time as _real_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# calibrate_wake_mic, compiled from dictation.py without importing it
# ---------------------------------------------------------------------------

def _load_calibration():
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    module_consts = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1
                     and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "_NOISE_FLOOR_MIN"]
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    body = [n for n in cls.body
            if (isinstance(n, ast.FunctionDef) and n.name == "calibrate_wake_mic")
            or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id.startswith("_WAKE_CAL_"))]
    assert len(body) == 3, "calibrate_wake_mic and its two _WAKE_CAL_ constants must exist"
    harness = ast.ClassDef(name="Harness", bases=[], keywords=[], body=body, decorator_list=[])
    module = ast.Module(body=module_consts + [harness], type_ignores=[])
    ast.fix_missing_locations(module)
    return module


class _FakeClock:
    """monotonic() advances 50 ms per call; sleep() is instant."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        self.now += 0.05
        return self.now

    def sleep(self, _s):
        pass

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _harness_class(clock):
    namespace = {"np": np, "math": math, "time": clock}
    exec(compile(_load_calibration(), str(ROOT / "dictation.py"), "exec"), namespace)
    return namespace["Harness"]


class _Frame:
    def __init__(self, level=0.01):
        self.pcm = np.full(1600, int(level * 32767), dtype=np.int16)


class _Engine:
    """Delivers `frames` frames, one per read, then EMPTY forever. Optional
    hook(n) runs before the n-th read."""

    _running = True

    def __init__(self, frames, hook=None):
        from samsara.audio_engine.ring import EMPTY
        self._empty = EMPTY
        self._left = frames
        self._hook = hook
        self.reads = 0
        self.unregistered = []

    def register_consumer(self, name):
        assert name == "wake-calibration"
        return self

    def read_next(self):
        self.reads += 1
        if self._hook:
            self._hook(self.reads)
        if self._left <= 0:
            return self._empty
        self._left -= 1
        return _Frame()

    def unregister_consumer(self, reader):
        self.unregistered.append(reader)


def _app(engine, clock=None):
    cls = _harness_class(clock or _FakeClock())
    app = cls()
    app._ace_engine = engine
    app._wake_noise_floor = 0.07
    app.config = {"wake_word_config": {"audio": {"measured_noise_floor": 0.07}}}
    app._config_lock = threading.Lock()
    app.save_config = Mock()
    return app


def test_insufficient_frames_write_nothing_and_explain_why(caplog):
    engine = _Engine(frames=5)
    app = _app(engine)

    with caplog.at_level(logging.WARNING):
        result = app.calibrate_wake_mic(seconds=3.0)

    assert result is None
    assert app._wake_noise_floor == 0.07
    assert app.config["wake_word_config"]["audio"]["measured_noise_floor"] == 0.07
    app.save_config.assert_not_called()
    report = app.last_wake_calibration
    assert report["status"] == "insufficient"
    assert (report["frames"], report["expected_frames"], report["min_frames"]) == (5, 30, 24)
    assert report["seconds"] == pytest.approx(0.5)
    assert "only 5 of the 30 expected 100 ms frames" in report["message"]
    assert "Nothing was saved" in report["message"]
    assert any("Calibration refused" in r.getMessage() for r in caplog.records)
    assert engine.unregistered == [engine]


def test_minimum_sample_is_80_percent_and_never_below_20_frames():
    cls = _harness_class(_FakeClock())
    assert cls._WAKE_CAL_MIN_FRACTION == 0.8 and cls._WAKE_CAL_MIN_FRAMES == 20
    app = _app(_Engine(frames=23))
    assert app.calibrate_wake_mic(seconds=3.0) is None        # 23 < ceil(30 * 0.8) = 24
    app = _app(_Engine(frames=19))
    assert app.calibrate_wake_mic(seconds=2.0) is None        # 20 expected -> 20 minimum
    assert app.last_wake_calibration["min_frames"] == 20


def test_full_sample_writes_floor_and_reports_frames_and_duration():
    app = _app(_Engine(frames=30))

    floor = app.calibrate_wake_mic(seconds=3.0)

    assert floor == pytest.approx(int(0.01 * 32767) / 32767.0)     # int16 frames
    assert app._wake_noise_floor == floor
    assert app.config["wake_word_config"]["audio"]["measured_noise_floor"] == floor
    app.save_config.assert_called_once()
    report = app.last_wake_calibration
    assert report["status"] == "ok" and report["frames"] == 30 and report["seconds"] == pytest.approx(3.0)


def test_cancelled_mid_run_writes_nothing():
    cancel = threading.Event()
    engine = _Engine(frames=30, hook=lambda n: cancel.set() if n == 12 else None)
    app = _app(engine)

    result = app.calibrate_wake_mic(seconds=3.0, cancel_event=cancel)

    assert result is None
    assert 0 < engine.reads < 30
    assert app._wake_noise_floor == 0.07
    assert app.config["wake_word_config"]["audio"]["measured_noise_floor"] == 0.07
    app.save_config.assert_not_called()
    assert app.last_wake_calibration["status"] == "cancelled"
    assert engine.unregistered == [engine]


# ---------------------------------------------------------------------------
# The guide
# ---------------------------------------------------------------------------

class _GuideApp:
    def __init__(self, calibrate):
        self.config = {"microphone": 1, "microphone_name": "Mic",
                       "wake_word_config": {"phrase": "jarvis", "oww_threshold": 0.2}}
        self._calibrate = calibrate

    def get_available_microphones(self):
        return [{"id": 1, "name": "Mic"}]

    def calibrate_wake_mic(self, *, seconds, cancel_event):
        return self._calibrate(self, seconds, cancel_event)


def _guide(monkeypatch, calibrate):
    from samsara.ui import mic_setup_wizard_qt as wizard
    monkeypatch.setattr(wizard._WizardWindow, "_ensure_audio_running", lambda self: None)
    window = wizard._WizardWindow(_GuideApp(calibrate))
    window._setup_oww_test = Mock()
    window._current_step = window._STEP_WAKE
    return wizard, window


def test_worker_exception_during_calibration_is_logged_surfaced_and_survived(qapp, monkeypatch, caplog):
    def boom(_app, _seconds, _cancel):
        raise RuntimeError("engine ring gone")

    wizard, window = _guide(monkeypatch, boom)
    spawned = {}
    monkeypatch.setattr(wizard.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.update(target=target))
    window._begin_wake_calibration()

    worker = threading.Thread(target=spawned["target"])     # a real worker thread
    with caplog.at_level(logging.WARNING):
        worker.start()
        worker.join(5)
        deadline = _real_time.monotonic() + 5
        while "failed" not in window._oww_result_lbl.text() and _real_time.monotonic() < deadline:
            qapp.processEvents()

    assert not worker.is_alive()
    text = window._oww_result_lbl.text()
    assert "Background calibration failed" in text and "engine ring gone" in text and "Nothing was saved" in text
    assert any("Wake calibration failed" in r.getMessage() and r.levelno >= logging.ERROR for r in caplog.records)
    window._setup_oww_test.assert_called_once()              # the guide carries on


def test_refused_sample_is_shown_in_the_guide_with_counts(qapp, monkeypatch):
    def refuse(app, _seconds, _cancel):
        app.last_wake_calibration = {"status": "insufficient", "frames": 5, "expected_frames": 30,
                                     "min_frames": 24, "seconds": 0.5, "floor": None,
                                     "message": "Calibration refused: only 5 of the 30 expected 100 ms frames "
                                                "arrived (0.5 s of audio; at least 24 are needed). Nothing was saved."}
        return None

    wizard, window = _guide(monkeypatch, refuse)
    spawned = {}
    monkeypatch.setattr(wizard.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.update(target=target))
    window._begin_wake_calibration()
    spawned["target"]()

    text = window._oww_result_lbl.text()
    assert "only 5 of the 30" in text and "0.5 s" in text and "Nothing was saved" in text
    assert wizard._WARNING.lower() in window._oww_result_lbl.styleSheet().lower()


def test_successful_calibration_shows_floor_frames_and_seconds(qapp, monkeypatch):
    def ok(app, _seconds, _cancel):
        app.last_wake_calibration = {"status": "ok", "frames": 30, "expected_frames": 30, "min_frames": 24,
                                     "seconds": 3.0, "floor": 0.0061, "message": ""}
        return 0.0061

    wizard, window = _guide(monkeypatch, ok)
    spawned = {}
    monkeypatch.setattr(wizard.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.update(target=target))
    window._begin_wake_calibration()
    spawned["target"]()

    assert "floor 0.0061 from 30 frames (3.0 s)" in window._oww_result_lbl.text()


class _BlockingStream:
    """read() blocks ~40 ms like a real 100 ms block; records which thread
    stops/closes it."""

    def __init__(self):
        self.closed_by = []
        self.stopped_by = []

    def start(self):
        pass

    def read(self, blocksize):
        _real_time.sleep(0.04)
        return np.zeros((blocksize, 1), dtype=np.float32), False

    def stop(self):
        self.stopped_by.append(threading.current_thread().name)

    def close(self):
        self.closed_by.append(threading.current_thread().name)


def test_stop_audio_never_closes_the_stream_from_the_calling_thread(qapp, monkeypatch):
    from samsara.ui import mic_setup_wizard_qt as wizard
    monkeypatch.setattr(wizard._WizardWindow, "_ensure_audio_running", lambda self: None)
    window = wizard._WizardWindow(_GuideApp(lambda *a: None))
    stream = _BlockingStream()
    monkeypatch.setattr(wizard.sd, "InputStream", lambda **kwargs: stream)
    monkeypatch.setattr(wizard, "_detect_capture_rate", lambda device: 16000)

    stop = threading.Event()
    window._audio_stop = stop
    window._wizard_active = True
    worker = threading.Thread(target=window._audio_worker, args=(stop,), name="wizard-audio-test")
    window._audio_thread = worker
    worker.start()
    deadline = _real_time.monotonic() + 3
    while window._stream is None and _real_time.monotonic() < deadline:
        _real_time.sleep(0.01)
    assert window._stream is stream

    assert window._stop_audio(join=True) is True

    assert stop.is_set() and not worker.is_alive()
    assert stream.closed_by == ["wizard-audio-test"], "only the worker may close its stream"
    assert stream.stopped_by == ["wizard-audio-test"]


def test_a_restarted_preview_does_not_revive_a_stopping_worker(qapp, monkeypatch):
    from samsara.ui import mic_setup_wizard_qt as wizard
    monkeypatch.setattr(wizard._WizardWindow, "_ensure_audio_running", lambda self: None)
    window = wizard._WizardWindow(_GuideApp(lambda *a: None))
    old_stop = threading.Event()
    old_stop.set()                       # this worker was told to stop...
    window._wizard_active = True         # ...and a new preview flipped the shared flag back
    opened = []
    monkeypatch.setattr(wizard.sd, "InputStream", lambda **kwargs: opened.append(1) or _BlockingStream())
    monkeypatch.setattr(wizard, "_detect_capture_rate", lambda device: 16000)

    window._audio_worker(old_stop)       # returns immediately

    assert opened == []


def test_guide_source_has_no_stream_close_outside_the_worker():
    """A structural guard: _stop_audio must not call stop()/close() on a stream."""
    source = (ROOT / "samsara" / "ui" / "mic_setup_wizard_qt.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_WizardWindow")
    stop_audio = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_stop_audio")
    calls = {n.func.attr for n in ast.walk(stop_audio) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not {"close", "abort"} & calls
    assert "stop" not in {n.func.attr for n in ast.walk(stop_audio)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                          and isinstance(n.func.value, ast.Name) and n.func.value.id == "stream"}
