"""Boot sequence contracts from perf_artifacts/boot_profile.md section 5.

Fix 1  OpenWakeWord is never loaded at boot with wake listening off; it loads
       on its own thread on first enable and publishes wake_ready.
Fix 2  The ACE engine starts before the model thread is kicked off.
Fix 3  The TTS engine is built on a worker after the shell is up.
Fix 4  A same-device calibration < 24 h old skips the boot recording.
Fix 8  [BOOT] stage timers keep one "last mark" per thread.

No audio, no models, no Qt: real DictationApp methods on __new__ instances.
"""
import ast
import inspect
import json
import textwrap
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

import dictation
import samsara.boot as samsara_boot


# ── Fix 8: per-thread stage timers ───────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_boot_timer_steps_are_per_thread(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(samsara_boot, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    lines = []
    timer = samsara_boot._BootStageTimer(log=lines.append)

    clock.now += 0.5
    timer("boot: step A")                     # boot thread: 500 ms since creation

    worker_started = threading.Event()
    worker_may_mark = threading.Event()

    def worker():
        timer.begin_thread()                  # model thread starts at +0.5 s
        worker_started.set()
        worker_may_mark.wait()
        timer("async: model")                 # marked at +3.0 s -> 2500 ms

    t = threading.Thread(target=worker, name="model-thread")
    t.start()
    worker_started.wait()
    clock.now += 2.0
    timer("boot: step B")                     # boot thread at +2.5 s -> 2000 ms
    clock.now += 0.5
    worker_may_mark.set()
    t.join()

    assert "[BOOT] boot: step A: 500ms" in lines[0]
    assert "[BOOT] boot: step B: 2000ms" in lines[1], (
        "boot thread's step absorbed the model thread's mark"
    )
    assert "[BOOT] async: model: 2500ms" in lines[2]
    assert "total 3000ms" in lines[2] and "thread=model-thread" in lines[2]


# ── Fix 2: ACE before the model thread; Fix 3: no TTS on the boot thread ─────

def _init_source():
    src = textwrap.dedent(inspect.getsource(dictation.DictationApp.__init__))
    return src, ast.parse(src)


def _first_call_line(tree, attr):
    lines = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == attr]
    assert lines, f"{attr}() not called from __init__"
    return min(lines)


def test_ace_engine_starts_before_model_load_is_kicked_off():
    _src, tree = _init_source()
    assert _first_call_line(tree, "_start_ace_engine") < _first_call_line(tree, "load_model_async")


def test_init_does_not_construct_tts_engines():
    src, tree = _init_source()
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"EdgeTTSEngine", "WinRTEngine", "AudioCoordinator"}
    assert "_tts_init_worker" in src


def test_init_does_not_record_calibration_without_cache_check():
    src, _tree = _init_source()
    assert "_run_calibration_if_auto(use_cache=True)" in src


# ── Fix 1: model worker never loads OpenWakeWord ─────────────────────────────

def _worker_app(wake_enabled):
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.splash = None
    app._splash_progress = 0
    app._splash_progress_lock = threading.Lock()
    app._startup_shell_ready = threading.Event()
    app._startup_shell_ready.set()
    app.config_path = Path("nonexistent-config-for-test.json")
    app.config = {"device": "cpu", "model_size": "tiny", "mode": "hold",
                  "wake_word_enabled": wake_enabled, "gesture": {"enabled": False}}
    app._boot_log = Mock()
    app._load_vad_model = Mock()
    app._load_oww_model = Mock(side_effect=AssertionError("OWW loaded on the model thread"))
    app._load_wake_profile_models = Mock(side_effect=AssertionError("profiles loaded on the model thread"))
    app.start_wake_word_mode = Mock()
    app._show_startup_error = Mock()
    app._schedule_ui = lambda callback, *args: callback(*args)
    return app


@pytest.mark.parametrize("wake_enabled", [False, True])
def test_model_worker_never_loads_openwakeword(monkeypatch, wake_enabled):
    app = _worker_app(wake_enabled)
    monkeypatch.setattr(dictation, "_create_whisper_model", Mock(return_value=object()))
    monkeypatch.setattr(dictation, "smart_corrections_warm_up", Mock())
    spawned = {}
    monkeypatch.setattr(dictation.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.setdefault(name, target))
    app.load_model_async()

    spawned["dictation.load"]()

    assert app._startup_failed is False
    app._load_oww_model.assert_not_called()
    app._load_wake_profile_models.assert_not_called()
    assert app.start_wake_word_mode.call_count == (1 if wake_enabled else 0)


# ── Queue 46: the Smart Corrections warm-up never blocks "Startup complete" ───

@pytest.mark.parametrize("sc_enabled", [False, True])
def test_smart_corrections_warm_up_is_off_the_startup_lane(monkeypatch, sc_enabled):
    """warm_up() resolves its backend with a synchronous Ollama HTTP probe
    (4.0 s measured per boot while Ollama is down). The model worker must
    reach Startup complete without calling it inline, spawn it on its own
    thread only when Smart Corrections is enabled, and never at all when off."""
    app = _worker_app(False)
    app.config["smart_corrections"] = {"enabled": sc_enabled}
    app._write_last_known_good = Mock()
    warm_up = Mock()
    monkeypatch.setattr(dictation, "_create_whisper_model", Mock(return_value=object()))
    monkeypatch.setattr(dictation, "smart_corrections_warm_up", warm_up)
    spawned = {}
    monkeypatch.setattr(dictation.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.setdefault(name, target))
    app.load_model_async()

    spawned["dictation.load"]()

    assert app._startup_failed is False
    warm_up.assert_not_called()                       # never inline on the startup lane
    if sc_enabled:
        spawned["dictation.smart_corrections_warm_up"]()
        warm_up.assert_called_once_with(app)
    else:
        assert "dictation.smart_corrections_warm_up" not in spawned


def test_tray_and_shell_ready_log_boot_markers(monkeypatch):
    """tools/boot_profile.py --full-boot reads these two [BOOT] lines."""
    src = inspect.getsource(dictation.DictationApp.create_tray_icon)
    assert '_boot_log("tray icon created")' in src
    assert '_boot_log("shell ready (tray and main window policy applied)")' in src


@pytest.mark.parametrize(
    ("config", "expected_callbacks"),
    [
        ({"first_run_complete": True, "microphone": "device-1", "start_hidden": True}, 1),
        ({"first_run_complete": False, "start_hidden": True}, 2),
    ],
)
def test_tray_startup_applies_hidden_window_policy(monkeypatch, config, expected_callbacks):
    from PySide6.QtCore import QTimer

    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config = config
    app._running = False
    app._startup_shell_ready = threading.Event()
    callbacks = []
    monkeypatch.setattr(QTimer, "singleShot", lambda _delay, _app, callback: callbacks.append(callback))

    app.create_tray_icon()

    assert len(callbacks) == expected_callbacks  # tray is always queued; wizard may force the window
    assert app._startup_shell_ready.is_set()


# ── Fix 1: lazy wake load, pending start, wake_ready ─────────────────────────

def _wake_app(monkeypatch):
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config = {"wake_word_enabled": True, "mode": "hold",
                  "wake_word_config": {"phrase": "jarvis"}}
    app._wake_models_lock = threading.Lock()
    app._wake_models_state = "off"
    app._wake_start_pending = False
    app.wake_ready = threading.Event()
    app._wake_detector = None
    app.model_loaded = True
    app.loading_model = False
    app.wake_word_active = False
    app.loaded = []
    app._load_oww_model = lambda: app.loaded.append("oww")
    app._load_wake_profile_models = lambda: app.loaded.append("profiles")
    app._publish_wake_state = Mock()
    app.activations = []
    spawned = []
    monkeypatch.setattr(dictation.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.append((name, target)))
    app.spawned = spawned
    return app


def test_first_enable_spawns_loader_and_defers_listening(monkeypatch):
    app = _wake_app(monkeypatch)
    app._start_hands_free_idle_duck = Mock(side_effect=AssertionError("listening before wake_ready"))

    dictation.DictationApp.start_wake_word_mode(app)

    assert [name for name, _ in app.spawned] == ["dictation.wake_models_load"]
    assert app.wake_ready_state() == "loading"
    assert app._wake_start_pending is True
    assert app.loaded == [], "OpenWakeWord loaded on the calling thread"
    assert "(loading)" in app._get_mode_display()


def test_loader_publishes_wake_ready_and_carries_out_the_pending_start(monkeypatch):
    app = _wake_app(monkeypatch)
    app.start_wake_word_mode = Mock()
    assert app._request_wake_models(start_listening=True) is False

    _name, loader = app.spawned[0]
    loader()

    assert app.loaded == ["oww", "profiles"]
    assert app.wake_ready.is_set()
    assert app.wake_ready_state() == "ready"
    assert app._get_mode_display() == "Hold + Wake"
    app.start_wake_word_mode.assert_called_once_with()
    assert app._publish_wake_state.call_count == 2   # loading, ready


def test_disable_while_loading_cancels_the_pending_start(monkeypatch):
    app = _wake_app(monkeypatch)
    app.start_wake_word_mode = Mock()
    app._request_wake_models(start_listening=True)

    app._cancel_pending_wake_start()
    app.spawned[0][1]()

    assert app.wake_ready.is_set()
    app.start_wake_word_mode.assert_not_called()


def test_second_request_while_loading_does_not_spawn_twice(monkeypatch):
    app = _wake_app(monkeypatch)
    app.start_wake_word_mode = Mock()
    app._request_wake_models(start_listening=True)
    app._request_wake_models(start_listening=True)
    assert len(app.spawned) == 1
    app.spawned[0][1]()
    assert app._request_wake_models(start_listening=True) is True
    assert len(app.spawned) == 1


def test_phrase_change_during_load_is_applied(monkeypatch):
    app = _wake_app(monkeypatch)
    built = []

    class _Detector:
        def __init__(self, phrase, threshold=0.2, model_path=None):
            self._wake_phrase = phrase.lower().strip()
            built.append(self._wake_phrase)

    monkeypatch.setattr(dictation, "WakeWordDetector", _Detector)

    def load_old_phrase():
        app._wake_detector = _Detector("jarvis")
        app.config["wake_word_config"]["phrase"] = "alexa"   # edited mid-load

    app._load_oww_model = load_old_phrase
    app._request_wake_models(start_listening=False)
    app.spawned[0][1]()

    assert app._wake_detector._wake_phrase == "alexa"
    assert built == ["jarvis", "alexa"]


def test_update_config_does_not_load_openwakeword_before_wake_is_requested(monkeypatch):
    app = _wake_app(monkeypatch)
    app._config_lock = threading.Lock()
    app.save_config = Mock()
    monkeypatch.setattr(dictation, "WakeWordDetector",
                        Mock(side_effect=AssertionError("OWW loaded by a settings change")))

    dictation.DictationApp.update_config(
        app, {"wake_word_config": {"phrase": "alexa"}}, save=False)

    assert app.config["wake_word_config"]["phrase"] == "alexa"


# ── Fix 3: TTS worker ────────────────────────────────────────────────────────

def test_tts_worker_waits_for_shell_then_publishes_and_announces(monkeypatch):
    import samsara.tts as tts_pkg

    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config = {"tts": {"enabled": True, "engine": "edge"}, "audio_coordinator": {}}
    app.output_device = None
    app.tts_engine = None
    app.audio_coordinator = None
    app.tts_ready = threading.Event()
    app._startup_shell_ready = threading.Event()
    order = []

    class _Engine:
        def __init__(self, output_device=None):
            order.append("engine")

    class _Coordinator:
        def __init__(self, app_, engine, config):
            order.append("coordinator")
            assert app_.audio_coordinator is None, "coordinator published before construction"

    monkeypatch.setattr(tts_pkg, "EdgeTTSEngine", _Engine)
    monkeypatch.setattr(tts_pkg, "AudioCoordinator", _Coordinator)
    app._maybe_announce_ava_command_session_migration = lambda: order.append(
        ("announce", app.audio_coordinator is not None))

    t = threading.Thread(target=app._tts_init_worker)
    t.start()
    t.join(timeout=0.2)
    assert t.is_alive() and order == [], "TTS built before the shell was ready"

    app._startup_shell_ready.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert order == ["engine", "coordinator", ("announce", True)]
    assert app.tts_ready.is_set()
    assert isinstance(app.tts_engine, _Engine)


def test_tts_worker_failure_leaves_guards_none_and_ready_set(monkeypatch):
    import samsara.tts as tts_pkg

    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config = {"tts": {"enabled": True, "engine": "winrt"}}
    app.output_device = None
    app.tts_engine = None
    app.audio_coordinator = None
    app.tts_ready = threading.Event()
    app._maybe_announce_ava_command_session_migration = Mock()
    monkeypatch.setattr(tts_pkg, "WinRTEngine", Mock(side_effect=RuntimeError("no winsdk")))

    app._tts_init_worker()

    assert app.tts_engine is None and app.audio_coordinator is None
    assert app.tts_ready.is_set()
    app._maybe_announce_ava_command_session_migration.assert_called_once_with()


# ── Fix 4: persisted calibration ─────────────────────────────────────────────

def _cal_app(tmp_path, monkeypatch, measured=0.02, samples=10):
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config_path = tmp_path / "config.json"
    app.config = {"threshold_mode": "auto", "microphone": 3,
                  "microphone_name": "Focusrite USB", "cal_multiplier": 3.0,
                  "wake_word_config": {"audio": {}}}
    app._config_lock = threading.Lock()
    app.capture_rate = 44100
    app.recording = False
    app.measure_calls = 0

    def fake_measure(device_id, capture_rate):
        app.measure_calls += 1
        return [measured / 3.0] * samples

    monkeypatch.setattr(dictation, "measure_ambient_rms", fake_measure)
    return app


def _threshold(app):
    return app.config["wake_word_config"]["audio"]["speech_threshold"]


def test_first_boot_records_and_persists(tmp_path, monkeypatch):
    app = _cal_app(tmp_path, monkeypatch)
    assert app._run_calibration_if_auto(use_cache=True) == "measured"
    assert app.measure_calls == 1
    stored = json.loads((tmp_path / "mic_calibration.json").read_text())
    assert stored["device_name"] == "Focusrite USB" and stored["capture_rate"] == 44100
    assert stored["threshold"] == pytest.approx(_threshold(app))


def test_fresh_same_device_calibration_skips_the_recording(tmp_path, monkeypatch):
    _cal_app(tmp_path, monkeypatch)._run_calibration_if_auto(use_cache=True)
    app = _cal_app(tmp_path, monkeypatch, measured=0.5)

    assert app._run_calibration_if_auto(use_cache=True) == "cached"
    assert app.measure_calls == 0
    assert _threshold(app) == pytest.approx(0.02, rel=0.2)


@pytest.mark.parametrize("change", ["stale", "other_device", "other_rate"])
def test_stale_or_different_device_records_again(tmp_path, monkeypatch, change):
    _cal_app(tmp_path, monkeypatch)._run_calibration_if_auto(use_cache=True)
    path = tmp_path / "mic_calibration.json"
    if change == "stale":
        stored = json.loads(path.read_text())
        stored["calibrated_at"] -= 25 * 3600
        path.write_text(json.dumps(stored))
    app = _cal_app(tmp_path, monkeypatch)
    if change == "other_device":
        app.config["microphone_name"] = "Laptop Mic"
    if change == "other_rate":
        app.capture_rate = 48000

    assert app._run_calibration_if_auto(use_cache=True) == "measured"
    assert app.measure_calls == 1


def test_manual_recalibrate_always_records(tmp_path, monkeypatch):
    _cal_app(tmp_path, monkeypatch)._run_calibration_if_auto(use_cache=True)
    app = _cal_app(tmp_path, monkeypatch)
    assert app._run_calibration_if_auto() == "measured"
    assert app.measure_calls == 1


def test_failed_recording_is_not_persisted(tmp_path, monkeypatch):
    app = _cal_app(tmp_path, monkeypatch, samples=0)
    assert app._run_calibration_if_auto(use_cache=True) == "failed"
    assert not (tmp_path / "mic_calibration.json").exists()


def test_corrupt_calibration_file_records_again(tmp_path, monkeypatch):
    (tmp_path / "mic_calibration.json").write_text("{not json")
    app = _cal_app(tmp_path, monkeypatch)
    assert app._run_calibration_if_auto(use_cache=True) == "measured"


def test_ceiling_clamped_calibration_is_applied_but_never_persisted(tmp_path, monkeypatch):
    """Queue 46: an "ambient" recording loud enough to hit CALIBRATION_CEILING
    is speech or noise, not room tone (live log 2026-09-13: ambient RMS 0.1589
    -> 0.1500, reused on the next boot). Use it this session, never store it."""
    from samsara.constants import CALIBRATION_CEILING
    app = _cal_app(tmp_path, monkeypatch, measured=1.0)
    assert app._run_calibration_if_auto(use_cache=True) == "measured"
    assert _threshold(app) == pytest.approx(CALIBRATION_CEILING)
    assert not (tmp_path / "mic_calibration.json").exists()


def test_stored_ceiling_calibration_is_not_reused(tmp_path, monkeypatch):
    """A cache written before the guard (threshold at the ceiling) records again."""
    from samsara.constants import CALIBRATION_CEILING
    _cal_app(tmp_path, monkeypatch)._run_calibration_if_auto(use_cache=True)
    path = tmp_path / "mic_calibration.json"
    stored = json.loads(path.read_text())
    stored["threshold"] = CALIBRATION_CEILING
    path.write_text(json.dumps(stored))
    app = _cal_app(tmp_path, monkeypatch)

    assert app._run_calibration_if_auto(use_cache=True) == "measured"
    assert app.measure_calls == 1


def test_background_refresh_does_not_persist_a_ceiling_measurement(tmp_path, monkeypatch):
    _cal_app(tmp_path, monkeypatch)._run_calibration_if_auto(use_cache=True)
    before = json.loads((tmp_path / "mic_calibration.json").read_text())
    app = _cal_app(tmp_path, monkeypatch, measured=1.0)
    monkeypatch.setattr(dictation.thread_registry, "spawn",
                        lambda name, target, daemon=True: target())
    app._refresh_calibration_in_background()
    assert json.loads((tmp_path / "mic_calibration.json").read_text()) == before


def test_background_refresh_skips_while_recording(tmp_path, monkeypatch):
    app = _cal_app(tmp_path, monkeypatch)
    app.recording = True
    monkeypatch.setattr(dictation.thread_registry, "spawn",
                        lambda name, target, daemon=True: target())
    app._refresh_calibration_in_background()
    assert app.measure_calls == 0


def test_background_refresh_updates_threshold_and_cache(tmp_path, monkeypatch):
    app = _cal_app(tmp_path, monkeypatch, measured=0.03)
    monkeypatch.setattr(dictation.thread_registry, "spawn",
                        lambda name, target, daemon=True: target())
    app._refresh_calibration_in_background()
    assert app.measure_calls == 1
    stored = json.loads((tmp_path / "mic_calibration.json").read_text())
    assert stored["threshold"] == pytest.approx(_threshold(app))


# ── Queue 276: per-user startup command and hidden-start policy ──────────────

def test_autostart_command_is_quoted_for_frozen_and_source_runs():
    from samsara.autostart import compose_run_command

    assert compose_run_command(
        frozen=True, executable=r"C:\Program Files\Samsara\Samsara.exe"
    ) == r'"C:\Program Files\Samsara\Samsara.exe"'

    frozen_with_home = compose_run_command(
        frozen=True,
        executable=r"C:\Program Files\Samsara\Samsara.exe",
        home_dir=r"C:\Users\Morne\.samsara-daily",
        comspec=r"C:\Windows\System32\cmd.exe",
    )
    assert frozen_with_home.startswith(r'"C:\Windows\System32\cmd.exe" /d /s /c ')
    assert 'SAMSARA_HOME_DIR=C:\\Users\\Morne\\.samsara-daily' in frozen_with_home
    assert 'start "" /b' in frozen_with_home
    assert 'C:\\Program Files\\Samsara\\Samsara.exe' in frozen_with_home

    source = compose_run_command(
        frozen=False,
        executable=r"F:\envs\sami\python.exe",
        pythonw_executable=r"F:\envs\sami\pythonw.exe",
        script_path=r"C:\Users\Morne\Projects\Samsara-dev\dictation.py",
    )
    assert source == (
        r'"F:\envs\sami\pythonw.exe" '
        r'"C:\Users\Morne\Projects\Samsara-dev\dictation.py"'
    )


def test_start_hidden_policy_keeps_wizard_visible():
    from samsara.autostart import should_show_main_window

    ready = {"first_run_complete": True, "microphone": "device-1", "start_hidden": True}
    assert should_show_main_window(ready) is False
    assert should_show_main_window({**ready, "start_hidden": False}) is True
    assert should_show_main_window({"start_hidden": True}) is True
    assert should_show_main_window({**ready, "microphone": None}) is False


def test_run_entry_update_and_removal_use_only_injected_registry():
    from samsara import autostart

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Registry:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1
        KEY_SET_VALUE = 2

        def __init__(self):
            self.values = {"OtherApp": "keep"}
            self.key = _Key()

        def CreateKey(self, root, path):
            assert root is self.HKEY_CURRENT_USER
            assert path == autostart.RUN_KEY
            return self.key

        def OpenKey(self, root, path, reserved, access):
            assert root is self.HKEY_CURRENT_USER
            assert path == autostart.RUN_KEY
            assert access == self.KEY_SET_VALUE
            return self.key

        def SetValueEx(self, key, name, reserved, value_type, value):
            assert key is self.key and value_type == self.REG_SZ
            self.values[name] = value

        def DeleteValue(self, key, name):
            assert key is self.key
            del self.values[name]

    registry = _Registry()
    autostart.sync_run_entry(True, command='"C:\\Program Files\\Samsara.exe"', registry=registry)
    assert registry.values["Samsara"] == '"C:\\Program Files\\Samsara.exe"'
    autostart.sync_run_entry(True, command='"D:\\Updated\\Samsara.exe"', registry=registry)
    assert registry.values["Samsara"] == '"D:\\Updated\\Samsara.exe"'
    autostart.sync_run_entry(False, registry=registry)
    assert registry.values == {"OtherApp": "keep"}


def test_run_entry_registry_failure_is_reported_to_the_caller():
    from samsara import autostart

    class _Denied:
        HKEY_CURRENT_USER = object()

        def CreateKey(self, *_args):
            raise PermissionError("policy locked")

    with pytest.raises(PermissionError):
        autostart.sync_run_entry(True, command="app.exe", registry=_Denied())
