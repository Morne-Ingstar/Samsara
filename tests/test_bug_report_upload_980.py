"""Synthetic-only HTTP/ZIP/privacy/Qt checks; never use the injected credential."""
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZIP_STORED, ZipFile

import pytest
from PySide6.QtCore import QThread
from samsara.bug_report import capture, coordinator, upload
from samsara.bug_report.preview_qt import ReportPreview


@pytest.fixture
def server():
    state = SimpleNamespace(status=200, delay=0, requests=[], redirect="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + body)
            fields, file = {}, None
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "file":
                    file = part.get_payload(decode=True)
                else:
                    fields[name] = part.get_content().rstrip("\r\n")
            # Deliberately do not retain or print the request credential.
            state.requests.append((fields, file))
            if state.delay:
                time.sleep(state.delay)
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            if state.redirect:
                self.send_header("Location", state.redirect)
            self.end_headers()
            try:
                self.wfile.write(b'{"id":"SR-abc123"}')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state.settings = upload.UploadSettings(secrets.token_hex(24), f"http://127.0.0.1:{httpd.server_port}/report")
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(1)


def make_bundle(tmp_path, **kwargs):
    return upload.build_bundle("Samsara problem report\nDiagnostics (last 200 lines; private values redacted):\n[EXIT] Goodbye!",
                               config={}, directory=tmp_path / ".review", **kwargs)


def wait_for(qapp, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert predicate()


def test_success_id_and_wire_fields_are_the_reviewed_zip(server, tmp_path):
    bundle = make_bundle(tmp_path, contact="reply@example.invalid", activity="Opening Settings")
    result = upload.send_bundle(bundle, server.settings, tmp_path / "support_bundles")
    assert result.report_id == "SR-abc123" and result.saved_path is None
    assert len(server.requests) == 1
    fields, data = server.requests[0]
    assert fields == bundle.fields()
    assert data == bundle.data == bundle.path.read_bytes()
    assert fields["bundle_sha256"] == sha256(data).hexdigest()
    with ZipFile(BytesIO(data)) as archive:
        assert all(info.compress_type == ZIP_STORED for info in archive.infolist())
        assert set(json.loads(archive.read("manifest.json"))["files"]) == set(archive.namelist())
        assert all(Path(name).suffix in (".json", ".txt", ".log") for name in archive.namelist())


@pytest.mark.parametrize("status", [413, 429, 507, 500, 401, 400, 503])
def test_http_failure_preserves_exact_bytes_without_retry(server, tmp_path, status):
    server.status = status
    bundle = make_bundle(tmp_path)
    directory = tmp_path / "profile" / "support_bundles"
    result = upload.send_bundle(bundle, server.settings, directory)
    assert not result.report_id and result.reason == f"Server returned {status}"
    assert result.saved_path.parent == directory
    assert result.saved_path.read_bytes() == bundle.data
    assert len(server.requests) == 1
    # A manual retry uses exactly the same ZIP and hash (server deduplication contract).
    server.status = 200
    assert upload.send_bundle(bundle, server.settings, directory).report_id == "SR-abc123"
    assert server.requests[0] == server.requests[1]


def test_timeout_is_single_attempt_and_same_zip_fallback(server, tmp_path):
    server.delay = 0.15
    bundle = make_bundle(tmp_path)
    result = upload.send_bundle(bundle, server.settings, tmp_path / "support_bundles", timeout=0.03)
    assert result.saved_path.read_bytes() == bundle.data
    assert len(server.requests) == 1 and not result.report_id
    assert upload.UPLOAD_TIMEOUT == 20


def test_offline_fallback_is_writable_and_exact(tmp_path):
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    settings = upload.UploadSettings(secrets.token_hex(24), f"http://127.0.0.1:{port}/report")
    bundle = make_bundle(tmp_path)
    result = upload.send_bundle(bundle, settings, tmp_path / "support_bundles", timeout=0.1)
    assert result.saved_path.read_bytes() == bundle.data
    with result.saved_path.open("ab"):
        pass


def test_redirect_does_not_forward_token_or_retry(server, tmp_path):
    server.status = 302
    server.redirect = server.settings.url + "/other"
    result = upload.send_bundle(make_bundle(tmp_path), server.settings, tmp_path / "support_bundles")
    assert result.saved_path.is_file() and len(server.requests) == 1


def test_sensitive_source_sentinels_absent_in_zip_fields_and_preview(tmp_path):
    sentinels = ("A synthetic dictated sentence stays private.", "synthetic-private@example.invalid",
                 r"C:\Users\SyntheticPrivate\Documents\personal.txt", "sk-syntheticPrivateCredential")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "samsara.log").write_text("\n".join([*sentinels, *["[INIT] " + text for text in sentinels], "[EXIT] Goodbye!"]), encoding="utf-8")
    config = {"model_size": sentinels[0], "language": sentinels[1], "device": sentinels[2],
              "compute_type": sentinels[3], "microphone": sentinels[0], "api_key": sentinels[3]}
    app = SimpleNamespace(config=config, _bug_report_log_dir=logs)
    report = capture.snapshot(app, capture.failure(sentinels[0]))
    bundle = upload.build_bundle(report, config=config, directory=tmp_path / ".review")
    evidence = bundle.data + json.dumps(bundle.fields()).encode() + bundle.preview().encode()
    assert "[EXIT] Goodbye!" in bundle.entries()["report.txt"]
    for sentinel in sentinels:
        assert sentinel.encode() not in evidence


def test_size_limit_drops_oldest_logs_and_explains_in_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "MAX_BUNDLE_BYTES", 2400)
    report = "Diagnostics (last 200 lines; private values redacted):\n" + "\n".join(
        f"oldest-{i:04d}" + "x" * 90 for i in range(100))
    bundle = upload.build_bundle(report, config={}, directory=tmp_path)
    files = bundle.entries()
    assert len(bundle.data) <= 2400
    assert "oldest-0000" not in files["report.txt"] and "oldest-0099" in files["report.txt"]
    assert "oldest diagnostic log lines" in files["truncation.txt"]
    assert upload.MAX_BUNDLE_BYTES == 2400


def test_metadata_oversize_fails_without_truncating_it(tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "MAX_BUNDLE_BYTES", 100)
    with pytest.raises(ValueError, match="metadata"):
        make_bundle(tmp_path)


def test_preview_reads_all_entries_and_metadata_from_finished_zip(qapp, tmp_path, qt_widget_scope, server):
    with qt_widget_scope(ReportPreview("Safe diagnostics", config={}, directory=tmp_path,
                                    settings=server.settings)) as window:
        window.show()
        window.activity.setText("Opening Settings")
        window.contact.setText("reply@example.invalid")
        original = window.bundle
        text = window.body.toPlainText()
        assert text == original.preview()
        for name, content in original.entries().items():
            assert f"ZIP entry: {name}\n{content}" in text
        for name, value in original.fields().items():
            assert json.dumps(name) in text and value in text
        assert not server.requests
        spoken = []
        window.on_spoken = lambda text: spoken.append((text, QThread.currentThread()))
        assert window.send_report()
        assert not window.send_report()  # Repeated click/voice while pending is inert.
        assert not window.contact.isEnabled() and not window.activity.isEnabled()
        wait_for(qapp, lambda: not window.busy)
        assert window.status.text() == "Sent - report SR-abc123"
        assert spoken == [(window.status.text(), qapp.thread())]
        assert server.requests == [(original.fields(), original.data)]
        assert not window.upload_button.isEnabled()
        # 980b n08: a confirmed receipt ends sending and offers Close.
        assert window.upload_button.isHidden()
        assert window.cancel.text() == "Close"
        assert not window.send_report()
        window.cancel.click()
        assert not window.isVisible()


def test_failure_status_folder_and_manual_retry_keep_hash(qapp, tmp_path, qt_widget_scope, server, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened, remembered = [], []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    server.status = 507
    with qt_widget_scope(ReportPreview("Safe diagnostics", config={}, directory=tmp_path,
                                     settings=server.settings, on_contact=remembered.append)) as window:
        window.show()
        window.contact.setText("reply@example.invalid")
        digest, data = window.bundle.digest, window.bundle.data
        window.upload_button.click()
        wait_for(qapp, lambda: not window.busy)
        assert remembered[-1] == "reply@example.invalid"
        assert window.saved_path.read_bytes() == data
        assert str(window.saved_path) in window.status.text()
        assert "No automatic retry" in window.status.text()
        assert window.upload_button.isVisible() and window.upload_button.isEnabled()
        assert window.upload_button.text() == "Retry send"
        window.open_folder.click()
        assert [Path(path) for path in opened] == [tmp_path]
        assert window.send.isEnabled() and window.email.isEnabled()
        for button in (window.upload_button, window.open_folder, window.send, window.email, window.cancel):
            assert window.rect().contains(button.geometry())
        server.status = 200
        window.upload_button.click()
        wait_for(qapp, lambda: not window.busy)
        assert window.bundle.digest == digest
        assert server.requests[0] == server.requests[1]
        # 980b n08: Retry must disappear after a successful manual retry too.
        assert window.status.text() == "Sent - report SR-abc123"
        assert window.upload_button.isHidden() and not window.upload_button.isEnabled()
        assert window.cancel.text() == "Close"
        assert not window.send_report()
        window.activity.setText("Opening Settings")
        assert window.upload_button.isVisible() and window.upload_button.isEnabled()
        assert window.upload_button.text() == "Send report"
        assert window.cancel.text() == "Cancel"


def test_send_hidden_without_build_file_and_user_config_token_ignored(qapp, tmp_path, monkeypatch, qt_widget_scope):
    monkeypatch.setattr(upload, "BUILD_CONFIG", tmp_path / "missing.json")
    with qt_widget_scope(ReportPreview("Safe diagnostics", config={"report_upload_token": secrets.token_hex(24)},
                                     directory=tmp_path)) as window:
        window.show()
        assert window.upload_button.isHidden() and window.consent.isHidden()
        assert not window.send_report()
        assert window.send.isVisible() and window.email.isVisible()


def test_build_config_loader_uses_only_build_token_and_fixed_destination(tmp_path):
    path = tmp_path / "injection.json"
    token = secrets.token_hex(24)
    path.write_text(json.dumps({"token": token, "url": "https://example.invalid/ignored"}))
    settings = upload.load_settings(path)
    assert settings.token == token and settings.url == upload.REPORT_UPLOAD_URL
    assert token not in repr(settings)
    path.write_text("[]")
    assert upload.load_settings(path) is None
    path.write_text('{"token":"bad\\nheader"}')
    assert upload.load_settings(path) is None


def test_public_source_release_gate_excludes_private_build_file():
    """Negative release gate: run on the public export BEFORE private build injection."""
    root = Path(__file__).resolve().parents[1]
    assert not (root / "samsara/data/report_upload.json").exists(), "Private build credential present in public tree"
    assert "/samsara/data/report_upload.json" in (root / ".gitignore").read_text().splitlines()
    # No fixed credential in tests: fake servers generate ephemeral values at runtime.
    from samsara.config_schema import SETTINGS_SCHEMA
    assert "report_upload_token" not in SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["report_upload_contact"]["default"] == ""


@pytest.mark.parametrize("phrase", ["send report", "Send that report!", " send   report "])
def test_voice_send_routes_only_command_lane_and_open_preview(monkeypatch, phrase):
    from samsara.session_modes import SessionModeManager, CommandDispatchResult, SessionMode, UtteranceSignals
    invoked = []
    monkeypatch.setattr(coordinator, "send_preview", lambda: invoked.append(1) or True)
    manager = SessionModeManager(abort_phrases=[], foreground_exe_resolver=lambda: "notepad.exe",
                                inject_fn=Mock(return_value=True), remove_chars_fn=Mock(),
                                command_dispatch_fn=Mock(return_value=CommandDispatchResult(matched=False)),
                                agent_dispatch_fn=Mock())
    assert manager.dispatch_command_utterance(phrase).kind == "command_executed"
    assert invoked == [1]
    manager.force_mode(SessionMode.DICTATE)
    manager.dispatch_utterance("send report", UtteranceSignals(True))
    assert invoked == [1]


def test_coordinator_posts_to_existing_visible_preview_only(monkeypatch):
    import weakref
    from samsara.ui import qt_runtime
    sends, posted = [], []
    window = SimpleNamespace(isVisible=lambda: True, send_report=lambda: sends.append(1))
    class App:
        _bug_report_windows = [window]
    app = App()
    monkeypatch.setattr(coordinator, "_active", weakref.ref(app))
    monkeypatch.setattr(qt_runtime, "post", posted.append)
    assert coordinator.send_preview() and not sends
    posted.pop()()
    assert sends == [1]
    app._bug_report_windows = []
    assert not coordinator.send_preview()


def test_modified_bytes_cannot_be_sent_under_reviewed_hash(server, tmp_path):
    from dataclasses import replace
    bundle = make_bundle(tmp_path)
    tampered = replace(bundle, data=bundle.data + b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        upload.send_bundle(tampered, server.settings, tmp_path)
    assert not server.requests


def test_edits_replace_zip_and_hash_before_send(qapp, tmp_path, qt_widget_scope, server):
    with qt_widget_scope(ReportPreview("Safe diagnostics", directory=tmp_path,
                                     settings=server.settings)) as window:
        original = window.bundle
        window.contact.setText("reply@example.invalid")
        assert window.bundle.digest != original.digest
        assert not original.path.exists()
        assert window.body.toPlainText() == window.bundle.preview()
        assert window.bundle.fields()["contact"] == "reply@example.invalid"
        assert server.settings.token not in window.body.toPlainText()


def test_closing_during_upload_still_finishes_single_fallback(qapp, tmp_path, server):
    server.status, server.delay = 500, 0.08
    window = ReportPreview("Safe diagnostics", directory=tmp_path, settings=server.settings)
    window.show()
    bundle = window.bundle
    assert window.send_report()
    window.close()
    destination = tmp_path / ("report-" + bundle.digest + ".zip")
    wait_for(qapp, lambda: destination.exists() and destination.read_bytes() == bundle.data)
    assert len(server.requests) == 1
