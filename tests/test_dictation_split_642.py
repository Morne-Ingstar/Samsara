"""642: compare the frozen pre-split implementation with the app facade.

The legacy methods below are copied verbatim from c9249546. Both paths use
identical fake devices, audio statistics, clocks and queued workers. No app
bootstrap, real audio, git command or external fixture is needed.
"""
import ast
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from samsara.calibration import calibrate_threshold
from samsara.constants import DEFAULT_SPEECH_THRESHOLD


LEGACY_SOURCE = r'''class LegacyCalibration:
    _CALIBRATION_CACHE_FILENAME = "mic_calibration.json"
    _CALIBRATION_MAX_AGE_S = 24 * 3600
    def _calibration_identity(self) -> dict:
        from samsara.microphone_recovery import calibration_identity
        return calibration_identity(self, sd)

    def _calibration_cache_path(self) -> Path:
        return self.config_path.parent / self._CALIBRATION_CACHE_FILENAME

    def _load_fresh_calibration(self, now: "float | None" = None) -> "float | None":
        """Threshold from the calibration cache if it belongs to the current
        device and is younger than _CALIBRATION_MAX_AGE_S, else None."""
        path = self._calibration_cache_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                stored = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning(f"[CAL] Ignoring unreadable calibration cache: {e}")
            return None
        if not isinstance(stored, dict):
            return None
        ident = self._calibration_identity()
        if ident['device_name'] and stored.get('device_name'):
            same_device = stored.get('device_name') == ident['device_name']
        else:
            same_device = stored.get('device_id') == ident['device_id']
        if not same_device:
            return None
        if stored.get('capture_rate') != ident['capture_rate']:
            return None
        if stored.get('multiplier') != ident['multiplier']:
            return None
        try:
            age = (time.time() if now is None else now) - float(stored['calibrated_at'])
            threshold = float(stored['threshold'])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= age < self._CALIBRATION_MAX_AGE_S) or not math.isfinite(threshold):
            return None
        if self._calibration_saturated(threshold):
            return None
        return threshold

    @staticmethod
    def _calibration_saturated(threshold: float) -> bool:
        """True when calibrate_threshold clamped to CALIBRATION_CEILING: the
        "ambient" recording was speech or noise, not room tone (live log
        2026-09-13: ambient RMS 0.1589 -> 0.1500, then reused on the next
        boot). Such a value may serve the current session but must never be
        stored or reused -- re-measuring beats reviving a bad calibration."""
        from samsara.constants import CALIBRATION_CEILING
        return threshold >= CALIBRATION_CEILING

    def _save_calibration(self, threshold: float) -> None:
        """Persist a measured threshold with its device identity. Never raises.
        A ceiling-clamped (contaminated) threshold is not persisted."""
        if self._calibration_saturated(threshold):
            logger.info(f"[CAL] Not persisting calibration at the ceiling "
                        f"({threshold:.4f}); the room was not quiet")
            return
        path = self._calibration_cache_path()
        record = dict(self._calibration_identity(),
                      threshold=float(threshold), calibrated_at=time.time())
        tmp = path.with_suffix('.json.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(record, f, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"[CAL] Could not persist calibration: {e}")

    def _measure_speech_threshold(self) -> "tuple[float, bool]":
        """Record ambient noise and derive a threshold. Returns (threshold,
        measured_ok); a failed or empty recording yields the default and
        False so it is never persisted as a real calibration."""
        multiplier = self.config.get('cal_multiplier', 3.0)
        from samsara.audio_engine.device_resolver import DeviceNotFound
        try:
            from samsara.audio_engine.device_resolver import select_input
            mic_id, _fallback = select_input(sd, self.config)
            rms_samples = measure_ambient_rms(mic_id, self.capture_rate)
            threshold = calibrate_threshold(rms_samples, multiplier=multiplier)
            ambient = float(np.median(rms_samples)) if rms_samples else 0.0
            logger.debug(f"[CAL] Ambient RMS: {ambient:.4f} | "
                  f"Multiplier: {multiplier}x | Threshold: {threshold:.4f}")
            return threshold, len(rms_samples) >= 3
        except DeviceNotFound:
            logger.info('[MIC] No microphone available; calibration deferred')
            return DEFAULT_SPEECH_THRESHOLD, False
        except Exception as e:
            threshold = DEFAULT_SPEECH_THRESHOLD
            logger.exception(f"[CAL] Calibration failed ({e}), using default {threshold:.4f}")
            return threshold, False

    def _apply_speech_threshold(self, threshold: float) -> None:
        """Apply to wake word audio config (in memory; persisted by the next save)."""
        with self._config_lock:
            ww_config = self.config.get('wake_word_config', {})
            if 'audio' not in ww_config:
                ww_config['audio'] = {}
            ww_config['audio']['speech_threshold'] = threshold
            self.config['wake_word_config'] = ww_config

    def _run_calibration_if_auto(self, use_cache=False):
        """Run mic calibration if threshold_mode is 'auto'. Updates config in place.

        use_cache=True (boot only) reuses a fresh calibration of the same
        device instead of recording. Every other caller (manual recalibrate,
        microphone switch) always records. Returns 'manual', 'cached',
        'measured' or 'failed'.
        """
        from samsara.microphone_recovery import input_available
        if not input_available(self, sd):
            return 'unavailable'
        mode = self.config.get('threshold_mode', 'auto')
        if mode != 'auto':
            thresh = self.config.get('wake_word_config', {}).get('audio', {}).get(
                'speech_threshold', DEFAULT_SPEECH_THRESHOLD)
            logger.debug(f"[CAL] Threshold mode: manual ({thresh:.4f})")
            return 'manual'

        if use_cache:
            cached = self._load_fresh_calibration()
            if cached is not None:
                logger.info(f"[CAL] Reusing calibration from the last 24 h "
                            f"(threshold {cached:.4f}); refreshing after startup")
                self._apply_speech_threshold(cached)
                return 'cached'

        threshold, ok = self._measure_speech_threshold()
        if ok:
            self._save_calibration(threshold)
        self._apply_speech_threshold(threshold)
        return 'measured' if ok else 'failed'

    def _refresh_calibration_in_background(self) -> None:
        """Re-measure after startup when boot reused a stored calibration, so
        a changed room still gets picked up. Skipped (and the stored value
        kept) if a hold-to-dictate recording is live at either end of the
        1.5 s window -- the user's own speech must not become "ambient"."""
        def _do():
            if self.config.get('threshold_mode', 'auto') != 'auto':
                return
            if getattr(self, 'recording', False):
                logger.info("[CAL] Background recalibration skipped: recording in progress")
                return
            threshold, ok = self._measure_speech_threshold()
            if not ok or getattr(self, 'recording', False) or self._calibration_saturated(threshold):
                logger.info("[CAL] Background recalibration discarded")
                return
            self._save_calibration(threshold)
            self._apply_speech_threshold(threshold)
            logger.info(f"[CAL] Background recalibration: threshold {threshold:.4f}")
        thread_registry.spawn("dictation.calibration_refresh", _do, daemon=True)

    def recalibrate_mic(self):
        """Re-run calibration in background and update config."""
        def _do():
            self._run_calibration_if_auto()
            self.persist_config()
        thread_registry.spawn("dictation._do", _do, daemon=True)
'''


def _facade_source():
    source = (Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(source).body
               if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    legacy_cls = ast.parse(LEGACY_SOURCE).body[0]
    names = {n.name for n in legacy_cls.body if isinstance(n, ast.FunctionDef)}
    cls.bases = []
    cls.body = [n for n in cls.body if
                (isinstance(n, ast.FunctionDef) and n.name in names) or
                (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id.startswith("_CALIBRATION_"))]
    assert {n.name for n in cls.body if isinstance(n, ast.FunctionDef)} == names
    return ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda message: self.lines.append((level, message))


class _Lock:
    def __init__(self):
        self.real = threading.Lock()
        self.entries = 0

    def __enter__(self):
        assert self.real.acquire(blocking=False)
        self.entries += 1

    def __exit__(self, *exc):
        self.real.release()


NOW = 100000.0


def _build(source, root, scenario):
    root.mkdir()
    devices = [{"name": "Test input", "max_input_channels": 1,
                "hostapi": 0, "default_samplerate": 44100}]
    if scenario == "unavailable":
        devices = []

    def query_devices(device=None, kind=None):
        if device is None and kind is None:
            return devices
        if not devices:
            raise RuntimeError("no test input")
        return devices[device or 0]

    sd = SimpleNamespace(query_devices=query_devices,
                         query_hostapis=lambda: [{"name": "Windows WASAPI"}],
                         default=SimpleNamespace(device=(0 if devices else -1, -1)))
    measured, workers, spawned, writes = [], [], [], []
    log = _Log()
    samples = {"empty": [], "short": [0.005, 0.006],
               "saturated": [0.2] * 10}.get(scenario, [0.005] * 9 + [0.5])

    def measure(device, rate):
        measured.append((device, rate))
        if scenario == "error":
            raise RuntimeError("synthetic measurement failure")
        if scenario == "busy_after":
            app.recording = True
        return samples[:]

    def spawn(name, target, *, daemon):
        spawned.append((name, daemon))
        workers.append(target)

    namespace = dict(Path=Path, json=json, math=math, os=os, np=np, sd=sd,
                     logger=log, measure_ambient_rms=measure,
                     calibrate_threshold=calibrate_threshold,
                     time=SimpleNamespace(time=lambda: NOW),
                     thread_registry=SimpleNamespace(spawn=spawn),
                     DEFAULT_SPEECH_THRESHOLD=DEFAULT_SPEECH_THRESHOLD)
    exec(compile(source, "<isolated calibration methods>", "exec"), namespace)
    cls = namespace.get("LegacyCalibration", namespace.get("DictationApp"))
    app = cls()
    app.config_path = root / "config.json"
    app.config = {"threshold_mode": "auto", "microphone": 0,
                  "microphone_name": "Test input", "cal_multiplier": 3.0,
                  "wake_word_config": {"audio": {"speech_threshold": 0.01},
                                       "keep": "untouched"}, "unrelated": {"keep": [1, 2]}}
    app.capture_rate = 44100
    app.recording = scenario == "busy_before"
    app._config_lock = _Lock()

    def persist():
        writes.append(deepcopy(app.config))

    app.persist_config = persist
    if scenario == "manual":
        app.config["threshold_mode"] = "manual"
    if scenario == "missing_audio":
        app.config["wake_word_config"].pop("audio")
    if scenario == "missing_wake_config":
        app.config.pop("wake_word_config")
    if scenario == "multiplier":
        app.config["cal_multiplier"] = 4.5
    cache = app._calibration_cache_path()
    record = {"device_id": 0, "device_name": "Test input", "capture_rate": 44100,
              "multiplier": 3.0, "threshold": 0.025, "calibrated_at": NOW - 10}
    changes = {
        "cached": {}, "manual": {}, "stale": {"calibrated_at": NOW - 86401},
        "expired": {"calibrated_at": NOW - 86400},
        "fresh_boundary": {"calibrated_at": NOW - 86399.99},
        "future": {"calibrated_at": NOW + 1},
        "other_device": {"device_name": "Other input"},
        "same_name_new_id": {"device_id": 9},
        "legacy_id": {"device_name": ""},
        "other_rate": {"capture_rate": 48000},
        "other_multiplier": {"multiplier": 5.0},
        "nan": {"threshold": float("nan")}, "inf": {"threshold": float("inf")},
        "bad_threshold": {"threshold": "invalid"},
        "ceiling_cache": {"threshold": 0.15},
        "missing_timestamp": {},
    }
    if scenario in changes:
        record.update(changes[scenario])
        if scenario == "missing_timestamp":
            record.pop("calibrated_at")
        cache.write_text(json.dumps(record), encoding="utf-8")
    if scenario == "corrupt":
        cache.write_text("{invalid", encoding="utf-8")
    if scenario == "non_object":
        cache.write_text("[]", encoding="utf-8")
    return SimpleNamespace(app=app, namespace=namespace, log=log, measured=measured,
                           workers=workers, spawned=spawned, writes=writes, root=root)


def _snapshot(harness, result):
    app = harness.app
    cache = app._calibration_cache_path()
    return dict(result=result, config=deepcopy(app.config), writes=harness.writes,
                cache=cache.read_text() if cache.exists() else None,
                measured=harness.measured, workers=harness.spawned,
                locks=app._config_lock.entries, recording=app.recording,
                device=(getattr(app, "_capture_device", None),
                        getattr(app, "_using_default_mic", None)),
                log=[(level, line.replace(str(harness.root), "<root>"))
                     for level, line in harness.log.lines])


def _run_pair(tmp_path, scenario, action):
    harnesses = [_build(LEGACY_SOURCE, tmp_path / "old", scenario),
                 _build(_facade_source(), tmp_path / "new", scenario)]
    snapshots = []
    for h in harnesses:
        if action == "boot":
            result = h.app._run_calibration_if_auto(use_cache=True)
        elif action == "refresh":
            result = h.app._refresh_calibration_in_background()
        elif action == "manual":
            result = h.app.recalibrate_mic()
        else:
            result = h.app._measure_speech_threshold()
        if h.workers:
            # Scheduling itself has not measured, changed config or saved it.
            assert h.measured == [] and h.writes == []
            for target in h.workers:
                target()
        snapshots.append(_snapshot(h, result))
    assert snapshots[0] == snapshots[1]
    return snapshots[1]


@pytest.mark.parametrize("scenario, expected", [
    ("measured", "measured"), ("cached", "cached"), ("manual", "manual"),
    ("unavailable", "unavailable"), ("empty", "failed"), ("short", "failed"),
    ("saturated", "measured"), ("error", "failed"), ("corrupt", "measured"),
    ("non_object", "measured"), ("stale", "measured"), ("expired", "measured"),
    ("fresh_boundary", "cached"), ("future", "measured"),
    ("other_device", "measured"), ("same_name_new_id", "cached"),
    ("legacy_id", "cached"), ("other_rate", "measured"),
    ("other_multiplier", "measured"), ("nan", "measured"), ("inf", "measured"),
    ("bad_threshold", "measured"), ("ceiling_cache", "measured"),
    ("missing_timestamp", "measured"), ("multiplier", "measured"),
    ("missing_audio", "measured"), ("missing_wake_config", "measured"),
])
def test_boot_old_and_new_results_config_cache_and_logs_match(tmp_path, scenario, expected):
    result = _run_pair(tmp_path, scenario, "boot")
    assert result["result"] == expected
    assert result["writes"] == []  # boot calibration never rotates config backups
    if scenario in {"empty", "short", "saturated", "error", "unavailable"}:
        assert result["cache"] is None
    if expected == "cached":
        assert result["measured"] == []
        assert result["config"]["wake_word_config"]["audio"]["speech_threshold"] == 0.025


@pytest.mark.parametrize("scenario, applied", [
    ("measured", True), ("cached", True), ("manual", False),
    ("busy_before", False), ("busy_after", False), ("short", False),
    ("error", False), ("saturated", False), ("unavailable", False),
])
def test_background_old_and_new_preserve_scheduling_and_recording_guards(tmp_path, scenario, applied):
    result = _run_pair(tmp_path, scenario, "refresh")
    assert result["workers"] == [("dictation.calibration_refresh", True)]
    assert result["locks"] == int(applied)
    assert result["writes"] == []


@pytest.mark.parametrize("scenario", ["cached", "measured", "manual", "error", "unavailable"])
def test_manual_recalibration_old_and_new_persist_once_after_worker(tmp_path, scenario):
    result = _run_pair(tmp_path, scenario, "manual")
    assert result["workers"] == [("dictation._do", True)]
    assert result["writes"] == [result["config"]]
    if scenario == "cached":
        assert result["measured"] == [(0, 44100)]  # manual bypasses the cache


@pytest.mark.parametrize("scenario", ["unavailable", "empty", "short", "error", "measured"])
def test_measurement_old_and_new_return_identical_stats_and_failure_flags(tmp_path, scenario):
    result = _run_pair(tmp_path, scenario, "measure")
    assert result["result"][1] == (scenario == "measured")


def test_cache_explicit_now_and_save_failure_are_identical(tmp_path, monkeypatch):
    old = _build(LEGACY_SOURCE, tmp_path / "old", "cached")
    new = _build(_facade_source(), tmp_path / "new", "cached")
    for h in (old, new):
        assert h.app._load_fresh_calibration(now=NOW) == 0.025
        assert h.app._load_fresh_calibration(now=NOW + 86400) is None
    def fail_replace(*args):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(os, "replace", fail_replace)
    for h in (old, new):
        h.app._save_calibration(0.02)
    assert _snapshot(old, None) == _snapshot(new, None)
    assert old.log.lines[-1] == ("warning", "[CAL] Could not persist calibration: synthetic replace failure")
