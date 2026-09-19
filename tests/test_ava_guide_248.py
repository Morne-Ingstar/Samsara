"""Queue 248: Ava setup copy, Ollama download, and pull decoding."""

from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtGui import QDesktopServices

from samsara.ui import ava_guide_qt as ag


def _window(qapp):
    app = SimpleNamespace(config={})
    window = ag._WizardWindow(app)
    window.show()
    qapp.processEvents()
    return window


def test_intro_does_not_say_ava_runs_by_default():
    text = ag._ava_intro_text({"cloud_llm": {"enabled": False}})

    assert "Ava is optional" in text
    assert "If you set her up, she runs on this computer" in text
    assert "nothing is sent anywhere unless you later choose the cloud option" in text
    assert "By default it runs entirely on your machine" not in text
    assert "—" not in text


def test_download_button_opens_ollama_url(qapp, monkeypatch):
    window = _window(qapp)
    opened = Mock()
    monkeypatch.setattr(QDesktopServices, "openUrl", opened)
    try:
        window._download_ollama_btn.click()
        assert opened.call_count == 1
        assert opened.call_args.args[0].toString() == ag._OLLAMA_DOWNLOAD
        assert window._ollama_recheck_timer.isActive()
    finally:
        window.close()


def test_download_auto_recheck_updates_status_and_stops(qapp, monkeypatch):
    window = _window(qapp)
    try:
        monkeypatch.setattr(window, "_check_ollama", lambda: window._on_ollama_status(True, ["llama3.2"]))
        window._ollama_recheck_timer.start()
        window._auto_recheck_ollama()
        assert window._ollama_status_lbl.text().startswith("Ollama is running")
        assert not window._ollama_recheck_timer.isActive()
    finally:
        window.close()


def test_download_auto_recheck_stops_at_ten_minute_limit(qapp, monkeypatch):
    window = _window(qapp)
    checks = []
    try:
        monkeypatch.setattr(window, "_check_ollama", lambda: checks.append(True))
        window._ollama_recheck_timer.start()
        for _ in range(ag._OLLAMA_RECHECK_LIMIT):
            window._auto_recheck_ollama()
        window._auto_recheck_ollama()
        assert len(checks) == ag._OLLAMA_RECHECK_LIMIT
        assert not window._ollama_recheck_timer.isActive()
    finally:
        window.close()


def test_pull_uses_utf8_and_cleans_progress_output(qapp, monkeypatch):
    window = _window(qapp)
    lines = []
    window._pull_line_sig.connect(lines.append)
    invalid_utf8_line = b"received \x8f\n".decode("utf-8", errors="replace")
    proc = SimpleNamespace(
        stdout=["pulling ⠋ 12%\r\x1b[2K", invalid_utf8_line],
        returncode=0,
        wait=Mock(),
    )
    popen = Mock(return_value=proc)
    monkeypatch.setattr(ag.subprocess, "Popen", popen)
    monkeypatch.setattr(ag.thread_registry, "spawn", lambda _name, fn, daemon: fn())
    try:
        window._pull_model()
        kwargs = popen.call_args.kwargs
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert any("pulling ⠋ 12%" in line for line in lines)
        assert any("received �" in line for line in lines)
        assert not any("Error:" in line for line in lines)
    finally:
        window.close()


def test_pull_exception_has_plain_reason_and_logs_raw_exception(qapp, monkeypatch):
    window = _window(qapp)
    lines = []
    window._pull_line_sig.connect(lines.append)
    monkeypatch.setattr(ag.subprocess, "Popen", Mock(side_effect=RuntimeError("raw details")))
    monkeypatch.setattr(ag.thread_registry, "spawn", lambda _name, fn, daemon: fn())
    try:
        window._pull_model()
        assert lines == ["Couldn't download the model: Ollama could not start the download."]
        assert "raw details" not in lines[0]
    finally:
        window.close()
