"""Queue 234: recording-media pause policy stays off real Windows media."""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from samsara.media_pause import MediaSessionUnavailable, RecordingMediaPauser


class _Session:
    def __init__(self, app_id: str, status: str = "playing", *, accepts_pause: bool = True):
        self.app_id = app_id
        self.status = status
        self.accepts_pause = accepts_pause
        self.pause_calls = 0
        self.resume_calls = 0
        self.observers = []

    def emit(self):
        for callback in list(self.observers):
            callback()


class _Layer:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_sessions(self):
        return self.sessions

    def playback_status(self, session):
        return session.status

    def pause(self, session):
        session.pause_calls += 1
        if not session.accepts_pause:
            return False
        session.status = "paused"
        session.emit()
        return True

    def resume(self, session):
        session.resume_calls += 1
        session.status = "playing"
        session.emit()
        return True

    def observe(self, session, callback):
        session.observers.append(callback)
        return lambda: session.observers.remove(callback)

    def app_id(self, session):
        return session.app_id


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def _run(sessions):
    layer = _Layer(sessions)
    pauser = RecordingMediaPauser(layer_factory=lambda: layer)
    pauser.start()
    _wait_for(lambda: all(s.status != "playing" or s.pause_calls for s in sessions))
    return pauser


def test_pauses_only_playing_sessions_and_resumes_exactly_them():
    playing, paused, stopped = _Session("music"), _Session("podcast", "paused"), _Session("video", "stopped")
    pauser = _run([playing, paused, stopped])
    assert (playing.pause_calls, paused.pause_calls, stopped.pause_calls) == (1, 0, 0)
    pauser.finish()
    pauser.wait()
    assert (playing.resume_calls, paused.resume_calls, stopped.resume_calls) == (1, 0, 0)


def test_changed_session_is_left_paused():
    music = _Session("music")
    pauser = _run([music])
    # A later status notification makes the user's/app's intent ambiguous.
    music.emit()
    pauser.finish()
    pauser.wait()
    assert music.resume_calls == 0


def test_pause_refusal_uses_duck_fallback():
    fallback = Mock()
    stream = _Session("stream", accepts_pause=False)
    pauser = RecordingMediaPauser(layer_factory=lambda: _Layer([stream]), fallback=fallback)
    pauser.start()
    _wait_for(lambda: fallback.called)
    pauser.finish()
    pauser.wait()
    assert stream.resume_calls == 0


def test_missing_api_uses_duck_fallback():
    fallback = Mock()
    pauser = RecordingMediaPauser(
        layer_factory=lambda: (_ for _ in ()).throw(MediaSessionUnavailable()),
        fallback=fallback,
    )
    pauser.start()
    _wait_for(lambda: fallback.called)
    pauser.finish()
    pauser.wait()


def test_short_recording_never_leaves_media_paused():
    music = _Session("music")
    pauser = RecordingMediaPauser(layer_factory=lambda: _Layer([music]))
    pauser.start()
    pauser.finish()
    pauser.wait()
    assert music.status == "playing"


def _migration_app(config):
    source = ast.parse((Path(__file__).parents[1] / "dictation.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "DictationApp")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_migrate_recording_media_mode")
    ns = {"logger": Mock()}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "dictation.py", "exec"), ns)
    app = SimpleNamespace(config=config, save_config=Mock())
    app._migrate_recording_media_mode = MethodType(ns[method.name], app)
    return app


def test_migration_uses_legacy_enabled_value_when_hold_duck_is_off():
    enabled = _migration_app({"capture_duck_hold_enabled": False, "ducking": {"enabled": True}})
    disabled = _migration_app({"capture_duck_hold_enabled": False, "ducking": {"enabled": False}})
    enabled._migrate_recording_media_mode()
    disabled._migrate_recording_media_mode()
    assert enabled.config["ducking"]["recording_mode"] == "duck"
    assert disabled.config["ducking"]["recording_mode"] == "off"


def test_migration_preserves_the_default_hold_duck():
    app = _migration_app({"ducking": {"enabled": False}})
    app._migrate_recording_media_mode()
    assert app.config["ducking"]["recording_mode"] == "duck"
