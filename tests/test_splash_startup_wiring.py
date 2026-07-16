"""Focused coverage for truthful, monotonic splash startup wiring."""

import threading
from unittest.mock import Mock

import dictation


class _RichSplash:
    def __init__(self):
        self.events = []
        self.reached_finalizing = threading.Event()

    def set_status(self, text):
        self.events.append(("status", text))

    def set_progress(self, value):
        self.events.append(("progress", value))
        if value == 96:
            self.reached_finalizing.set()

    def set_detail(self, text):
        self.events.append(("detail", text))

    def set_error(self, text, detail=""):
        self.events.append(("error", text, detail))

    def complete(self, text="Samsara ready", detail="Startup complete."):
        self.events.append(("complete", text, detail))

    def close(self):
        self.events.append(("close",))


class _LegacySplash:
    def __init__(self):
        self.statuses = []

    def set_status(self, text):
        self.statuses.append(text)


def _startup_app(splash):
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.splash = splash
    app._splash_progress = 0
    app._splash_progress_lock = threading.Lock()
    app._startup_shell_ready = threading.Event()
    app.config = {
        "device": "cpu",
        "model_size": "tiny",
        "mode": "hold",
        "wake_word_enabled": False,
        "gesture": {"enabled": False},
    }
    app._boot_log = Mock()
    app._load_vad_model = Mock()
    app._load_oww_model = Mock()
    app._load_wake_profile_models = Mock()
    app._show_startup_error = Mock()
    app._schedule_ui = lambda callback, *args: callback(*args)
    return app


def _capture_model_worker(monkeypatch, app):
    captured = {}
    monkeypatch.setattr(
        dictation.thread_registry,
        "spawn",
        lambda name, target, daemon: captured.setdefault("target", target),
    )
    app.load_model_async()
    return captured["target"]


def test_update_splash_remains_compatible_and_ignores_stale_phase():
    legacy = _LegacySplash()
    app = _startup_app(legacy)

    app.update_splash("Loading model", 70, "local model")
    app.update_splash("Late audio update", 60, "stale")

    assert legacy.statuses == ["Loading model"]
    assert app._splash_progress == 70


def test_model_worker_waits_for_shell_before_completion(monkeypatch):
    splash = _RichSplash()
    app = _startup_app(splash)
    monkeypatch.setattr(dictation, "_create_whisper_model", Mock(return_value=object()))
    monkeypatch.setattr(dictation, "smart_corrections_warm_up", Mock())
    worker = _capture_model_worker(monkeypatch, app)

    thread = threading.Thread(target=worker)
    thread.start()
    assert splash.reached_finalizing.wait(timeout=2.0)

    # Model, VAD, and wake setup are done, but the synchronous UI/audio lane
    # has not yet reported ready, so there must be no fake completion/close.
    assert not any(event[0] == "complete" for event in splash.events)
    assert not any(event[0] == "close" for event in splash.events)

    app._startup_shell_ready.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()

    kinds = [event[0] for event in splash.events]
    progress = [event[1] for event in splash.events if event[0] == "progress"]
    assert progress == sorted(progress)
    assert progress[-1] == 100
    assert kinds.index("complete") < kinds.index("close")
    assert app._startup_failed is False
    app._load_vad_model.assert_called_once_with()
    app._load_oww_model.assert_called_once_with()
    app._load_wake_profile_models.assert_called_once_with()


def test_startup_failure_keeps_splash_visible_in_error_state(monkeypatch):
    splash = _RichSplash()
    app = _startup_app(splash)
    monkeypatch.setattr(
        dictation,
        "_create_whisper_model",
        Mock(side_effect=RuntimeError("model unavailable")),
    )
    worker = _capture_model_worker(monkeypatch, app)

    worker()

    assert app._startup_failed is True
    assert (
        "error", "Startup could not finish", "model unavailable",
    ) in splash.events
    assert not any(event[0] == "complete" for event in splash.events)
    assert not any(event[0] == "close" for event in splash.events)

