"""Voice restart uses the tray's guarded Qt restart path."""

from types import SimpleNamespace

import pytest


def test_voice_restart_posts_existing_handoff_to_qt(monkeypatch):
    from plugins.commands import core_utils
    from samsara import app_restart
    from samsara.ui import qt_runtime
    from samsara.ui.tray_qt import SamsaraTrayQt

    posted = []
    events = []
    app = SimpleNamespace(
        _resolve_parked_draft_before_quit=lambda: events.append("draft") or True,
        quit_app=lambda **kwargs: events.append(("quit", kwargs)),
    )
    tray = SimpleNamespace(_app=app)
    tray._restart_samsara = lambda: SamsaraTrayQt._restart_samsara(tray)
    app.tray_icon = tray
    monkeypatch.setattr(qt_runtime, "post", posted.append)
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: True)
    monkeypatch.setattr(app_restart, "start_handoff", lambda argv, *, cwd: events.append(
        ("handoff", argv, cwd)))

    assert core_utils.restart_app(app) is True
    assert core_utils.restart_app(app) is True
    assert len(posted) == 2
    assert events == []
    for callback in posted:
        callback()
    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "draft", "handoff", "quit"
    ]
    assert events[-1] == ("quit", {"_restart_draft_resolved": True})


def test_voice_restart_cancelled_draft_can_retry(monkeypatch):
    from plugins.commands import core_utils
    from samsara import app_restart
    from samsara.ui import qt_runtime
    from samsara.ui.tray_qt import SamsaraTrayQt

    posted = []
    events = []
    decisions = iter((False, True))
    app = SimpleNamespace(
        _resolve_parked_draft_before_quit=lambda: next(decisions),
        quit_app=lambda **kwargs: events.append(("quit", kwargs)),
    )
    tray = SimpleNamespace(_app=app)
    tray._restart_samsara = lambda: SamsaraTrayQt._restart_samsara(tray)
    app.tray_icon = tray
    monkeypatch.setattr(qt_runtime, "post", posted.append)
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: True)
    monkeypatch.setattr(app_restart, "start_handoff", lambda *_a, **_k: events.append("handoff"))

    core_utils.restart_app(app)
    posted.pop(0)()
    assert events == []
    assert tray._restart_pending is False
    core_utils.restart_app(app)
    posted.pop(0)()
    assert events == ["handoff", ("quit", {"_restart_draft_resolved": True})]


def test_voice_restart_handoff_error_is_visible_and_retryable(monkeypatch):
    from plugins.commands import core_utils
    from samsara import app_restart
    from samsara.ui import qt_runtime
    from samsara.ui.tray_qt import SamsaraTrayQt

    posted = []
    events = []
    messages = []
    app = SimpleNamespace(
        _resolve_parked_draft_before_quit=lambda: True,
        quit_app=lambda **kwargs: events.append(("quit", kwargs)),
    )
    tray = SimpleNamespace(_app=app, _tray=SimpleNamespace(
        showMessage=lambda *args: messages.append(args)))
    tray._restart_samsara = lambda: SamsaraTrayQt._restart_samsara(tray)
    app.tray_icon = tray
    monkeypatch.setattr(qt_runtime, "post", posted.append)
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: True)

    def fail_handoff(*_args, **_kwargs):
        raise OSError("dummy helper unavailable")

    monkeypatch.setattr(app_restart, "start_handoff", fail_handoff)
    core_utils.restart_app(app)
    posted.pop(0)()
    assert events == []
    assert tray._restart_pending is False
    assert "dummy helper unavailable" in messages[0][1]

    monkeypatch.setattr(app_restart, "start_handoff", lambda *_a, **_k: events.append("handoff"))
    core_utils.restart_app(app)
    posted.pop(0)()
    assert events == ["handoff", ("quit", {"_restart_draft_resolved": True})]


def test_voice_restart_unavailable_qt_does_not_queue(monkeypatch):
    from plugins.commands import core_utils
    from samsara.ui import qt_runtime

    spoken = []
    app = SimpleNamespace(audio_coordinator=SimpleNamespace(
        speak=lambda text, **_kwargs: spoken.append(text)))
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: False)
    monkeypatch.setattr(qt_runtime, "post", lambda _cb: pytest.fail("Qt is unavailable"))
    assert core_utils.restart_app(app) is True
    assert spoken == ["Restart is unavailable right now."]
