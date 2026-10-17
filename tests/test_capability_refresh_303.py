"""Queue 303: capability surfaces follow live Ava readiness."""

import threading
import types

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer, Signal

from samsara import ai_preferences, ava_readiness
from samsara.ui import home_signals, qt_runtime, voice_help_qt


def _app():
    config = {"ollama": {"enabled": True, "host": "http://127.0.0.1:11434"}}
    provider = ai_preferences.provider_for(config, "local")
    consent = ai_preferences.accept_consent(
        config, features=("ava",), provider=provider, cloud=False,
        accepted_at="2026-10-17T00:00:00Z")
    config = ai_preferences.apply_preferences(
        config, policy="local", ava=True, consent=consent, local_model="fixture-model")
    return types.SimpleNamespace(
        config=config,
        available_mics=[],
        snoozed=False,
    )


def _drain(qapp):
    loop = QEventLoop()
    QTimer.singleShot(40, loop.quit)
    loop.exec()
    qapp.processEvents()


class _QtDispatcher(QObject):
    posted = Signal(object)

    def __init__(self):
        super().__init__()
        self.posted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _run(callback):
        callback()


def test_voice_help_tracks_readiness_and_unsubscribes_after_destroy(
        qapp, monkeypatch):
    tracker = ava_readiness.ReadinessTracker()
    tracker.record_probe("ollama", ava_readiness.UNREACHABLE)
    monkeypatch.setattr(ava_readiness, "tracker", tracker)
    dispatcher = _QtDispatcher()
    monkeypatch.setattr(qt_runtime, "post", dispatcher.posted.emit)

    page = voice_help_qt.VoiceHelpPage(_app())
    assert page.capability_texts()[home_signals.AVA] == "Ava: not working"

    worker = threading.Thread(
        target=lambda: tracker.record_probe("ollama", None), daemon=True)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    _drain(qapp)

    assert page.capability_texts()[home_signals.AVA] == "Ava: working"

    page.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert tracker._listeners == []

    # A late update after destruction is harmless and has no widget callback.
    tracker.record_probe("ollama", ava_readiness.UNREACHABLE)
