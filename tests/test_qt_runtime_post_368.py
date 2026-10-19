import logging

from samsara.ui import qt_runtime
from samsara.ui.qt_runtime import post

# Capture the production function during collection. A manually driven fixture
# nested inside mock.patch can restore that outer mock during later teardown.
# These unit tests exercise post itself, even if another test leaked a dispatch mock.


def test_post_normal_path_is_silent_at_debug_level(monkeypatch, caplog):
    monkeypatch.setattr(qt_runtime, "_state", qt_runtime._RUNNING)
    monkeypatch.setattr(qt_runtime, "_app", object())
    calls = []
    monkeypatch.setattr(qt_runtime.QTimer, "singleShot", lambda *args: calls.append(args))

    with caplog.at_level(logging.DEBUG, logger="samsara.ui.qt_runtime"):
        post(lambda: None)

    assert len(calls) == 1
    assert not [record for record in caplog.records
                if record.name == "samsara.ui.qt_runtime" and record.levelno == logging.DEBUG]


def test_post_still_warns_when_runtime_is_not_running(monkeypatch, caplog):
    monkeypatch.setattr(qt_runtime, "_state", qt_runtime._IDLE)
    monkeypatch.setattr(qt_runtime, "_app", None)

    with caplog.at_level(logging.WARNING, logger="samsara.ui.qt_runtime"):
        post(lambda: None)

    assert "dropping callback" in caplog.text
