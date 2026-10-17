"""Microphone calibration policy behind the DictationApp compatibility facade.

The app owns configuration, locks, device recovery and measurement callbacks.
Wrappers supply current patchable hardware, clock, logging and worker seams.
"""
import json
import math
import os
from pathlib import Path

import numpy as np

from samsara.constants import DEFAULT_SPEECH_THRESHOLD


def calibration_identity(app, *, sd) -> dict:
    from samsara.microphone_recovery import calibration_identity
    return calibration_identity(app, sd)


def calibration_cache_path(app) -> Path:
    return app.config_path.parent / app._CALIBRATION_CACHE_FILENAME


def load_fresh_calibration(app, now: "float | None" = None, *, clock, logger) -> "float | None":
    """Threshold from the calibration cache if it belongs to the current
    device and is younger than _CALIBRATION_MAX_AGE_S, else None."""
    path = app._calibration_cache_path()
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
    ident = app._calibration_identity()
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
        age = (clock() if now is None else now) - float(stored['calibrated_at'])
        threshold = float(stored['threshold'])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= age < app._CALIBRATION_MAX_AGE_S) or not math.isfinite(threshold):
        return None
    if app._calibration_saturated(threshold):
        return None
    return threshold


def calibration_saturated(threshold: float) -> bool:
    """True when calibrate_threshold clamped to CALIBRATION_CEILING: the
    "ambient" recording was speech or noise, not room tone (live log
    2026-09-13: ambient RMS 0.1589 -> 0.1500, then reused on the next
    boot). Such a value may serve the current session but must never be
    stored or reused -- re-measuring beats reviving a bad calibration."""
    from samsara.constants import CALIBRATION_CEILING
    return threshold >= CALIBRATION_CEILING


def save_calibration(app, threshold: float, *, clock, logger) -> None:
    """Persist a measured threshold with its device identity. Never raises.
    A ceiling-clamped (contaminated) threshold is not persisted."""
    if app._calibration_saturated(threshold):
        logger.info(f"[CAL] Not persisting calibration at the ceiling "
                    f"({threshold:.4f}); the room was not quiet")
        return
    path = app._calibration_cache_path()
    record = dict(app._calibration_identity(),
                  threshold=float(threshold), calibrated_at=clock())
    tmp = path.with_suffix('.json.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[CAL] Could not persist calibration: {e}")


def measure_speech_threshold(app, *, sd, measure_ambient_rms, calibrate_threshold, logger) -> "tuple[float, bool]":
    """Record ambient noise and derive a threshold. Returns (threshold,
    measured_ok); a failed or empty recording yields the default and
    False so it is never persisted as a real calibration."""
    multiplier = app.config.get('cal_multiplier', 3.0)
    from samsara.audio_engine.device_resolver import DeviceNotFound
    try:
        from samsara.audio_engine.device_resolver import select_input
        mic_id, _fallback = select_input(sd, app.config)
        rms_samples = measure_ambient_rms(mic_id, app.capture_rate)
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


def apply_speech_threshold(app, threshold: float) -> None:
    """Apply to wake word audio config (in memory; persisted by the next save)."""
    with app._config_lock:
        ww_config = app.config.get('wake_word_config', {})
        if 'audio' not in ww_config:
            ww_config['audio'] = {}
        ww_config['audio']['speech_threshold'] = threshold
        app.config['wake_word_config'] = ww_config


def run_calibration_if_auto(app, use_cache=False, *, sd, logger):
    """Run mic calibration if threshold_mode is 'auto'. Updates config in place.

    use_cache=True (boot only) reuses a fresh calibration of the same
    device instead of recording. Every other caller (manual recalibrate,
    microphone switch) always records. Returns 'manual', 'cached',
    'measured' or 'failed'.
    """
    from samsara.microphone_recovery import input_available
    if not input_available(app, sd):
        return 'unavailable'
    mode = app.config.get('threshold_mode', 'auto')
    if mode != 'auto':
        thresh = app.config.get('wake_word_config', {}).get('audio', {}).get(
            'speech_threshold', DEFAULT_SPEECH_THRESHOLD)
        logger.debug(f"[CAL] Threshold mode: manual ({thresh:.4f})")
        return 'manual'

    if use_cache:
        cached = app._load_fresh_calibration()
        if cached is not None:
            logger.info(f"[CAL] Reusing calibration from the last 24 h "
                        f"(threshold {cached:.4f}); refreshing after startup")
            app._apply_speech_threshold(cached)
            return 'cached'

    threshold, ok = app._measure_speech_threshold()
    if ok:
        app._save_calibration(threshold)
    app._apply_speech_threshold(threshold)
    return 'measured' if ok else 'failed'


def refresh_calibration_in_background(app, *, spawn, logger) -> None:
    """Re-measure after startup when boot reused a stored calibration, so
    a changed room still gets picked up. Skipped (and the stored value
    kept) if a hold-to-dictate recording is live at either end of the
    1.5 s window -- the user's own speech must not become "ambient"."""
    def _do():
        if app.config.get('threshold_mode', 'auto') != 'auto':
            return
        if getattr(app, 'recording', False):
            logger.info("[CAL] Background recalibration skipped: recording in progress")
            return
        threshold, ok = app._measure_speech_threshold()
        if not ok or getattr(app, 'recording', False) or app._calibration_saturated(threshold):
            logger.info("[CAL] Background recalibration discarded")
            return
        app._save_calibration(threshold)
        app._apply_speech_threshold(threshold)
        logger.info(f"[CAL] Background recalibration: threshold {threshold:.4f}")
    spawn("dictation.calibration_refresh", _do, daemon=True)


def recalibrate_mic(app, *, spawn):
    """Re-run calibration in background and update config."""
    def _do():
        app._run_calibration_if_auto()
        app.persist_config()
    spawn("dictation._do", _do, daemon=True)
