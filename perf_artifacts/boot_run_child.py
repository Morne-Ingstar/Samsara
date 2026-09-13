"""Child process for perf_artifacts/boot_run.py -- boots one Samsara instance.

argv: <code_dir> <results_json> <hold_test: 0|1>
Env:  SAMSARA_HOME_DIR must point at a temp profile (the parent enforces it).

Replicates dictation.py's __main__ block (instance lock, early UI scale,
splash, DictationApp(splash)) with the module imported normally, plus:
  * no global input hooks (pynput keyboard listener, low-level mouse hook,
    CapsLock hook, key macros) -- this instance must never react to the
    user's real keystrokes while the live Samsara is running;
  * no earcons and no main window pop-up (the tray icon still appears);
  * optional hold-to-dictate check: once the Whisper model is loaded, run
    the same start_recording()/stop_recording() pair the hold hotkey drives,
    with the presence gate forced open (so ambient audio reaches Whisper)
    and text output / command dispatch captured instead of executed.
Exits via os._exit once results are written.
"""
import json
import os
import sys
import threading
import time

CODE, RESULTS, HOLD = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
assert os.environ.get("SAMSARA_HOME_DIR"), "refusing to run against the live profile"
sys.path.insert(0, CODE)
os.chdir(CODE)

import dictation as d  # noqa: E402

out = {"code": CODE, "hold_test": HOLD}


class _NoListener:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def join(self, *a, **k):
        pass


d.pynput_keyboard.Listener = _NoListener
d.DictationApp._install_mouse_listener = lambda self: None
d.DictationApp._install_capslock_hook = lambda self: None
d.KeyMacroManager.start = lambda self: None
d.DictationApp.play_sound = lambda self, *a, **k: None
d.DictationApp.show_main_window = lambda self, *a, **k: None

captured = {"output": [], "commands": [], "decodes": []}
holder = {}


def _finish(code=0):
    try:
        with open(RESULTS, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
    finally:
        os._exit(code)


def _hold_test(app):
    """The hold hotkey's own calls (on_key_press -> start_recording,
    release -> stop_recording), driven directly."""
    real_decode = app._decode_hotkey_audio

    def decode(audio, params, duration, *, free_form=None):
        t = time.perf_counter()
        result = real_decode(audio, params, duration, free_form=free_form)
        captured["decodes"].append({"audio_s": round(float(duration), 2),
                                    "decode_ms": round((time.perf_counter() - t) * 1000),
                                    "result": repr(result)[:200]})
        return result

    app._decode_hotkey_audio = decode
    app._buffer_should_skip_decode = lambda *a, **k: False
    app._output_dictation = lambda text: captured["output"].append(text)
    app.command_executor.process_text = lambda text, *a, **k: captured["commands"].append(text) or False

    res = {"wake_state_at_press": app.wake_ready_state() if hasattr(app, "wake_ready_state") else None,
           "wake_ready_set_at_press": bool(getattr(app, "wake_ready", threading.Event()).is_set()),
           "model_loaded_at_press": app.model_loaded,
           "ace_running": app._ace_engine is not None}
    app.hotkey_pressed = True
    app.start_recording(streaming=False, play_earcon=False)
    res["recording_after_press"] = bool(app.recording)
    res["ace_dictation_active"] = bool(getattr(app, "_ace_dictation_active", False))
    time.sleep(1.5)
    app.hotkey_pressed = False
    app.stop_recording()
    deadline = time.monotonic() + 30
    while not captured["decodes"] and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    res["wake_state_after_decode"] = app.wake_ready_state() if hasattr(app, "wake_ready_state") else None
    res.update(captured)
    return res


def _driver():
    t0 = time.monotonic()
    while "app" not in holder:
        time.sleep(0.02)
    app = holder["app"]
    try:
        if HOLD:
            while not app.model_loaded and not getattr(app, "_startup_failed", False):
                if time.monotonic() - t0 > 240:
                    out["error"] = "model never loaded"
                    _finish(2)
                time.sleep(0.02)
        while getattr(app, "_splash_progress", 0) < 100 and not getattr(app, "_startup_failed", False):
            if time.monotonic() - t0 > 240:
                out["error"] = "startup never completed"
                _finish(2)
            time.sleep(0.05)
        out["startup_failed"] = bool(getattr(app, "_startup_failed", False))
        if HOLD and not out["startup_failed"]:
            out["hold"] = _hold_test(app)
        # Let post-ready background work (wake load, TTS, calibration
        # refresh) land in the log before the parent reads it.
        time.sleep(3.0)
        out["wake_state_final"] = app.wake_ready_state() if hasattr(app, "wake_ready_state") else None
        try:
            app._stop_ace_engine()
        except Exception as exc:
            out["stop_error"] = str(exc)
    except Exception as exc:
        import traceback
        out["error"] = traceback.format_exc()
        _finish(3)
    _finish(0)


_orig_tray = d.DictationApp.create_tray_icon


def _tray(self):
    holder["app"] = self
    _orig_tray(self)


d.DictationApp.create_tray_icon = _tray
threading.Thread(target=_driver, name="boot-run-driver", daemon=True).start()

d._acquire_instance_lock()
try:
    from samsara.ui_scale import apply_early_ui_scale
    apply_early_ui_scale(d.samsara_config_path())
except Exception:
    pass
from samsara.ui.splash_qt import SplashScreenQt  # noqa: E402

splash = SplashScreenQt()
splash.set_status("Initializing...")
d.DictationApp(splash)
