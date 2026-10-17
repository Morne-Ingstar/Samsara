"""Real port-0 HTTP, isolated profiles and fake speech; never import dictation."""
from http.client import HTTPConnection, HTTPResponse
from contextlib import closing
import errno
import io
import json
import logging
from pathlib import Path
import secrets
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from samsara import agent_server as agents


class FakeCoordinator:
    def __init__(self):
        self.calls = []
        self.pending = False
        self.noop = None

    def speak(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if not self.pending and self.noop is None:
            kwargs["on_done"]()
        return SimpleNamespace(utterance_id=self.noop or "fake-speech")


def fake_app(enabled=True):
    return SimpleNamespace(config={"command_packs": {"agents": enabled},
                                   "tts": {"feedback_level": "brief", "enabled": True},
                                   "ava": {"provider_policy": "local"}},
                           audio_coordinator=FakeCoordinator(), _running=True,
                           recording=False, wake_word_active=False, continuous_active=False,
                           _session_mode_manager=SimpleNamespace(mode=SimpleNamespace(value="ava")),
                           _schedule_ui=lambda callback: callback())


@pytest.fixture
def server(tmp_path):
    app = fake_app()
    service = agents.AgentServer(app, profile=tmp_path, port=0)
    assert service.start()
    try:
        yield service
    finally:
        service.stop()
        # Generated test credentials are transient, never retained as evidence.
        for path in tmp_path.rglob("token.json"):
            path.unlink()


@pytest.fixture
def memory_clipboard(monkeypatch):
    from PySide6.QtCore import QMimeData
    from samsara.ui.settings import agents_section

    class FakeClipboard:
        def __init__(self):
            self._mime = QMimeData()

        def mimeData(self):
            return self._mime

        def setMimeData(self, mime):
            self._mime = mime

        def text(self):
            return self._mime.text()

        def setText(self, text):
            self._mime = QMimeData()
            self._mime.setText(text)

        def clear(self):
            self._mime = QMimeData()

    clipboard = FakeClipboard()
    monkeypatch.setattr(agents_section, "QApplication", SimpleNamespace(clipboard=lambda: clipboard))
    return clipboard


def post(server, method="ping", params=None, *, headers=None, raw=None, notification=False,
         modern=False, path="/mcp", verb="POST"):
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notification:
        payload["id"] = 1
    request_headers = {"Content-Type": "application/json",
                       "Authorization": "Bearer " + server._token}
    if modern:
        payload["params"]["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": agents.VERSIONS[0],
            "io.modelcontextprotocol/clientInfo": {"name": "fake", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}
        request_headers.update({"MCP-Protocol-Version": agents.VERSIONS[0], "Mcp-Method": method})
        if method == "tools/call":
            request_headers["Mcp-Name"] = payload["params"]["name"]
    request_headers.update(headers or {})
    request_headers = {key: value for key, value in request_headers.items() if value is not None}
    body = json.dumps(payload).encode() if raw is None else raw
    with closing(HTTPConnection("127.0.0.1", server.port, timeout=4)) as connection:
        connection.request(verb, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data) if data else None


def call(server, name="speak", arguments=None, **kwargs):
    return post(server, "tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)


def test_off_by_default_without_profile_io(tmp_path):
    app = fake_app(False)
    service = agents.AgentServer(app, profile=tmp_path, port=0)
    assert not service.start()
    assert service.status == "off"
    assert service._thread is None
    assert not list(tmp_path.iterdir())
    from samsara.command_packs import default_pack_config
    assert default_pack_config()["agents"] is False


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic wrong", "Bearer "])
def test_authentication_refuses_without_speech(server, authorization):
    status, _ = call(server, arguments={"text": "Hello."}, headers={"Authorization": authorization})
    assert status == 401
    assert not server.app.audio_coordinator.calls


@pytest.mark.parametrize("origin", ["", "null", "https://example.invalid", "http://localhost"])
def test_any_origin_refused(server, origin):
    assert call(server, arguments={"text": "Hello."}, headers={"Origin": origin})[0] == 403
    assert not server.app.audio_coordinator.calls


@pytest.mark.parametrize("host", ["example.invalid", "127.0.0.1:1", "localhost", "127.0.0.1.evil:47832"])
def test_bad_host_refused(server, host):
    assert call(server, arguments={"text": "Hello."}, headers={"Host": host})[0] == 403
    assert not server.app.audio_coordinator.calls


def test_localhost_host_accepted(server):
    assert post(server, headers={"Host": "localhost:" + str(server.port)})[0] == 200


def test_legacy_initialize_notification_and_exact_tools(server):
    status, payload = post(server, "initialize", {"protocolVersion": "2025-11-25",
                          "capabilities": {}, "clientInfo": {"name": "fake", "version": "1"}})
    assert status == 200
    assert payload["result"]["protocolVersion"] == "2025-11-25"
    assert "tools" in payload["result"]["capabilities"]
    assert post(server, "notifications/initialized", notification=True) == (202, None)
    status, payload = post(server, "tools/list")
    assert status == 200
    tools = payload["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["get_status", "speak"]
    assert tools[1]["inputSchema"]["properties"]["text"]["maxLength"] == 400
    assert not tools[0]["inputSchema"]["additionalProperties"]
    assert post(server, "not-a-method")[1]["error"]["code"] == -32601


def test_current_stateless_discovery_and_tools(server):
    status, payload = post(server, "server/discover", modern=True)
    assert status == 200
    assert payload["result"]["supportedVersions"] == list(agents.VERSIONS)
    assert payload["result"]["resultType"] == "complete"
    assert post(server, "tools/list", modern=True)[0] == 200
    assert call(server, "get_status", modern=True)[0] == 200
    assert post(server, "unknown", modern=True)[0] == 404


@pytest.mark.parametrize("headers", [{"Mcp-Method": "ping"}, {"Mcp-Name": "speak"},
                                     {"MCP-Protocol-Version": "1900-01-01"}])
def test_protocol_header_mismatch_or_unsupported(server, headers):
    status, payload = call(server, "get_status", modern=True, headers=headers)
    assert status == 400
    assert payload["error"]["code"] in {-32020, -32022}


@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_feedback_levels(server, level):
    server.app.config["tts"]["feedback_level"] = level
    status, payload = call(server, arguments={"text": "Hello."})
    assert status == 200
    value = payload["result"]["structuredContent"]
    calls = server.app.audio_coordinator.calls
    if level == "silent":
        assert value == {"status": "suppressed", "reason": "silent"}
        assert not calls
    else:
        assert value["status"] == "spoken"
        assert len(calls) == 1
        assert calls[0][1]["category"] == "conversation"
        assert calls[0][1]["interruptible"] is True


@pytest.mark.parametrize("setting", ["mute", "tts_off", "missing_coordinator"])
def test_mute_or_voice_unavailable(server, setting):
    if setting == "mute":
        server.app.config["accessibility"] = {"ava_mute_audio": True}
    elif setting == "tts_off":
        server.app.config["tts"]["enabled"] = False
    else:
        server.app.audio_coordinator = None
    value = call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]
    assert value["status"] == "suppressed"
    assert server.app.audio_coordinator is None or not server.app.audio_coordinator.calls


def test_queue_rate_and_pending_limit(server):
    coordinator = server.app.audio_coordinator
    coordinator.pending = True
    clock = [100.0]
    server._clock = lambda: clock[0]
    value = call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]
    assert value["status"] == "queued"
    assert call(server, arguments={"text": "Again."})[1]["result"]["structuredContent"]["reason"] == "rate_limited"
    for _ in range(2):
        clock[0] += 2
        assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["status"] == "queued"
    clock[0] += 2
    assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["reason"] == "rate_limited"
    coordinator.calls[0][1]["on_done"]()
    coordinator.calls[0][1]["on_done"]()  # Idempotent completion.
    assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["status"] == "queued"


def test_coordinator_suppression_releases_pending(server):
    server.app.audio_coordinator.noop = "noop-ava-muted"
    value = call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]
    assert value == {"status": "suppressed", "reason": "muted"}
    assert not server._pending


def test_cancelled_speech_recovers_without_completion(server):
    coordinator = server.app.audio_coordinator
    coordinator.pending = True  # Simulates cancellation: on_done never fires.
    clock = [100.0]
    server._clock = lambda: clock[0]
    for _ in range(agents.MAX_PENDING):
        assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["status"] == "queued"
        clock[0] += agents.SPEAK_INTERVAL
    assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["reason"] == "rate_limited"
    clock[0] = 100.0 + agents.PENDING_TIMEOUT
    assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["status"] == "queued"
    assert len(coordinator.calls) == agents.MAX_PENDING + 1
    before = len(server._pending)
    coordinator.calls[0][1]["on_done"]()  # Expired completion cannot free the new slot.
    assert len(server._pending) == before


def test_stop_resets_pending_and_old_completion_cannot_release_new_slot(server):
    coordinator = server.app.audio_coordinator
    coordinator.pending = True
    for index in range(agents.MAX_PENDING):
        server._clock = lambda index=index: 100.0 + index * agents.SPEAK_INTERVAL
        assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["status"] == "queued"
    old_completion = coordinator.calls[0][1]["on_done"]
    server.stop()
    assert not server._pending
    assert server.start()
    assert call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]["status"] == "queued"
    old_completion()
    assert len(server._pending) == 1


def test_status_closed_public_fields(server):
    server.app._last_dictation_text = secrets.token_hex(20)
    server.app.config["cloud_llm"] = {"api_key": secrets.token_hex(20)}
    value = call(server, "get_status")[1]["result"]["structuredContent"]
    assert set(value) == {"app_version", "listening", "current_mode", "feedback_level", "speech_muted", "ava_route"}
    assert value["current_mode"] == "ava"
    assert value["ava_route"] == "local"
    assert value["listening"] is False
    server.app.wake_word_active = True
    assert call(server, "get_status")[1]["result"]["structuredContent"]["listening"] is True


@pytest.mark.parametrize("text", ["", " " * 4, "x" * 401, "<speak>Hello</speak>", "```code```", "a\x00b", "a\nb", 123])
def test_invalid_text_never_speaks(server, text):
    assert call(server, arguments={"text": text})[1]["error"]["code"] == -32602
    assert not server.app.audio_coordinator.calls


def test_length_boundary_and_extra_arguments(server):
    assert call(server, arguments={"text": "x" * 400})[0] == 200
    assert call(server, arguments={"text": "Hello.", "voice": "other"})[1]["error"]["code"] == -32602


def test_oversize_malformed_and_unsupported_transport(server):
    assert post(server, raw=b"x" * (agents.MAX_BODY + 1))[0] == 413
    assert post(server, raw=b"{")[1]["error"]["code"] == -32700
    assert post(server, raw=b"[]")[1]["error"]["code"] == -32600
    assert post(server, headers={"Content-Type": "text/plain"})[0] == 415
    assert post(server, headers={"Transfer-Encoding": "chunked"})[0] == 400
    assert post(server, path="/other")[0] == 404
    assert post(server, verb="GET")[0] == 405
    assert post(server, verb="DELETE")[0] == 405


def test_two_megabyte_body_gets_413_without_reset(server):
    assert post(server, raw=b"x" * (2 * 1024 * 1024)) == (413, None)
    assert not server.app.audio_coordinator.calls
    assert post(server)[0] == 200


@pytest.mark.parametrize("refusal,status", [
    ("origin", 403), ("host", 403), ("auth", 401), ("stopping", 503),
    ("disabled", 503), ("path", 404), ("content_type", 415), ("method", 405),
    ("transfer_encoding", 400), ("length", 400), ("oversize", 413),
    ("unknown_method", 501),
])
def test_refusal_delivered_before_remaining_body_arrives(server, refusal, status):
    headers = {"Host": "127.0.0.1:" + str(server.port),
               "Authorization": "Bearer " + server._token,
               "Content-Type": "application/json", "Content-Length": "32768"}
    path, method = "/mcp", "POST"
    if refusal == "origin":
        headers["Origin"] = "null"
    elif refusal == "host":
        headers["Host"] = "example.invalid"
    elif refusal == "auth":
        headers.pop("Authorization")
    elif refusal == "stopping":
        server._stopping.set()
    elif refusal == "disabled":
        server.app.config["command_packs"]["agents"] = False
    elif refusal == "path":
        path = "/other"
    elif refusal == "content_type":
        headers["Content-Type"] = "text/plain"
    elif refusal == "method":
        method = "GET"
    elif refusal == "unknown_method":
        method = "UNKNOWN"
    elif refusal == "transfer_encoding":
        headers["Transfer-Encoding"] = "chunked"
    elif refusal == "length":
        headers["Content-Length"] = "invalid"
    request = (method + " " + path + " HTTP/1.1\r\n"
               + "".join(key + ": " + value + "\r\n" for key, value in headers.items())
               + "\r\n").encode()
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as connection:
        connection.sendall(request + b"x")
        with closing(HTTPResponse(connection)) as response:
            response.begin()
            assert response.status == status
            assert response.read() == b""
            # The write half has closed, but late request bytes can still be
            # discarded. A premature full close/reset fails sendall or recv.
            connection.sendall(b"x" * 32767)
            connection.shutdown(socket.SHUT_WR)
            assert connection.recv(1) == b""
    server._stopping.clear()
    server.app.config["command_packs"]["agents"] = True
    assert not server.app.audio_coordinator.calls
    assert post(server)[0] == 200


def test_lingering_close_discards_with_a_byte_budget():
    reads = []
    events = []

    class UnendingBody:
        def read1(self, size):
            reads.append(size)
            return b"x" * size

    handler = object.__new__(agents._Handler)
    handler.wfile = SimpleNamespace(flush=lambda: events.append("flush"))
    handler.connection = SimpleNamespace(shutdown=lambda how: events.append(how))
    handler.rfile = UnendingBody()
    handler._linger_close()
    assert events == ["flush", socket.SHUT_WR]
    assert sum(reads) == agents.MAX_DRAIN
    assert max(reads) <= agents.MAX_BODY


def test_lingering_close_preserves_the_existing_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(agents, "time", SimpleNamespace(monotonic=lambda: now[0]))
    timeouts = []
    reads = []

    def receive(buffer):
        reads.append(len(buffer))
        buffer[0] = ord("x")
        now[0] += agents.REQUEST_TIMEOUT / 4
        return 1

    connection = SimpleNamespace(shutdown=lambda how: None, settimeout=timeouts.append,
                                 recv_into=receive)
    raw = agents._DeadlineReader(connection)
    # Consume almost the entire original deadline before refusing the request.
    now[0] += agents.REQUEST_TIMEOUT * 0.75
    handler = object.__new__(agents._Handler)
    handler.connection = connection
    handler.wfile = SimpleNamespace(flush=lambda: None)
    with io.BufferedReader(raw) as reader:
        handler.rfile = reader
        handler._linger_close()
    assert timeouts == [agents.REQUEST_TIMEOUT / 4]
    assert len(reads) == 1
    assert now[0] == raw.deadline


def test_token_persists_and_rotates_without_rebind(server):
    old = server._token
    port = server.port
    assert server.new_token()
    assert server.port == port
    assert not hmac_equal(old, server._token)
    assert post(server, headers={"Authorization": "Bearer " + old})[0] == 401
    replacement = agents.AgentServer(server.app, profile=server._profile, port=0)
    replacement._load_token()
    assert hmac_equal(server._token, replacement._token)


@pytest.mark.parametrize("damaged", [b"{", b"[]", b'null', b'{"token":"invalid","version":1}',
                                    b'{"version":1}', b'\xff'])
def test_corrupt_token_is_regenerated_on_restart(server, damaged):
    server.stop()
    path = server._token_path()
    path.write_bytes(damaged)
    old = server._token
    assert server.start()
    assert server.status == "listening"
    assert not hmac_equal(old, server._token)
    assert post(server)[0] == 200
    assert server.new_token()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EBUSY, errno.EIO])
def test_transient_token_read_error_preserves_pairing(server, monkeypatch, error_number):
    server.stop()
    path = server._token_path()
    old = server._token
    saved = path.read_bytes()
    read_text = Path.read_text

    def locked_read(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError(error_number, "Transient token read failure")
        return read_text(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", locked_read)
        patch.setattr(server, "_save_token", lambda token: pytest.fail("Read error rotated token"))
        assert not server.start()
        assert server.status == "unavailable"
        assert hmac_equal(old, server._token)
        assert bool(saved == path.read_bytes())
        assert server._thread is None
    assert server.start()
    assert hmac_equal(old, server._token)
    assert post(server)[0] == 200


def hmac_equal(left, right):
    import hmac
    return hmac.compare_digest(left, right)


def test_port_busy_is_one_safe_line_and_status(server, caplog):
    other = agents.AgentServer(server.app, profile=server._profile, port=server.port)
    with caplog.at_level(logging.WARNING, logger=agents.__name__):
        assert not other.start()
    assert other.status == "unavailable"
    records = [record for record in caplog.records if record.name == agents.__name__]
    assert len(records) == 1
    assert records[0].getMessage() == "[AGENTS] outcome=start_unavailable"
    assert other._thread is None


def test_logs_carry_no_request_payload_token_or_unknown_names(server, caplog):
    private = secrets.token_hex(24)
    with caplog.at_level(logging.INFO, logger=agents.__name__):
        call(server, arguments={"text": private})
        post(server, private)
        call(server, private)
        post(server, path="/" + private)
    messages = "\n".join(record.getMessage() for record in caplog.records if record.name == agents.__name__)
    assert bool(private not in messages)
    assert bool(server._token not in messages)
    assert "method=tools/call" in messages


def test_delayed_ui_timeout_cancels_late_speech(server, monkeypatch):
    callbacks = []
    server.app._schedule_ui = callbacks.append
    monkeypatch.setattr(agents, "UI_TIMEOUT", 0.02)
    value = call(server, arguments={"text": "Hello."})[1]["result"]["structuredContent"]
    assert value == {"status": "suppressed", "reason": "ui_unavailable"}
    callbacks[0]()
    assert not server.app.audio_coordinator.calls


def test_quit_callback_stops_thread_and_releases_port(server, monkeypatch):
    callbacks = []
    monkeypatch.setattr(agents.thread_registry, "on_shutdown", callbacks.append)
    monkeypatch.setattr(agents, "AgentServer", lambda app: server)
    assert agents.for_app(server.app) is server
    assert agents.for_app(server.app) is server
    assert len(callbacks) == 1
    thread, port = server._thread, server.port
    server.app._running = False
    assert call(server, arguments={"text": "Hello."})[0] == 503
    callbacks[0]()
    assert not thread.is_alive()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


def test_partial_request_shutdown_is_bounded(server):
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as connection:
        connection.sendall(b"POST /mcp HTTP/1.1\r\n")
        deadline = time.monotonic() + 1
        while server._connection is None and time.monotonic() < deadline:
            time.sleep(0.005)
        thread = server._thread
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < agents.REQUEST_TIMEOUT + 0.5
        assert not thread.is_alive()


@pytest.mark.parametrize("partial_body", [False, True])
def test_slow_sender_cannot_extend_request_deadline(server, monkeypatch, partial_body):
    monkeypatch.setattr(agents, "REQUEST_TIMEOUT", 0.3)
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as connection:
        if partial_body:
            prefix = ("POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:" + str(server.port)
                      + "\r\nAuthorization: Bearer " + server._token
                      + "\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
            connection.sendall(prefix.encode())
        else:
            connection.sendall(b"P")
        started = time.monotonic()
        # Drip within the recv timeout for longer than the fixed deadline.
        for _ in range(7):
            time.sleep(0.08)
            try:
                connection.sendall(b" ")
            except OSError:
                break
        try:
            closed = connection.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            closed = True  # Windows can reset after discarding the partial request.
        assert closed
        assert time.monotonic() - started < 1.5
        assert post(server)[0] == 200


@pytest.mark.parametrize("modern", [False, True])
def test_cancelled_notification_is_accepted_without_speech(server, modern):
    assert post(server, "notifications/cancelled", {"requestId": 9},
                notification=True, modern=modern) == (202, None)
    assert not server.app.audio_coordinator.calls


def test_unsupported_version_reports_requested_and_supported(server):
    status, payload = post(server, headers={"MCP-Protocol-Version": "1900-01-01"})
    assert status == 400
    assert payload["error"]["data"] == {"requested": "1900-01-01", "supported": list(agents.VERSIONS)}


def test_switch_off_closes_endpoint_and_reenables_same_instance(server):
    server.app.config["command_packs"]["agents"] = False
    assert not server.sync()
    assert server.status == "off"
    assert server.connection_details() is None
    server.app.config["command_packs"]["agents"] = True
    assert server.sync()
    assert server.status == "listening"


def test_boot_plugin_uses_existing_lifecycle_hook(monkeypatch):
    from plugins.commands import agent_server as plugin
    app, calls = fake_app(), []
    monkeypatch.setattr(agents, "for_app", lambda value: SimpleNamespace(sync=lambda: calls.append(value)))
    plugin.start_services(app)
    assert calls == [app]


def test_settings_switch_copy_rotation_and_status(qapp, monkeypatch, memory_clipboard):
    from PySide6.QtWidgets import QCheckBox
    from samsara.ui.settings import agents_section
    calls = []
    state = SimpleNamespace(status="off", port=47832, sync=lambda: calls.append("sync"),
                            connection_details=lambda: "local endpoint details",
                            new_token=lambda: calls.append("rotate") or True)
    monkeypatch.setattr(agents_section, "for_app", lambda app: state)
    monkeypatch.setattr(agents_section.QTimer, "singleShot", lambda *args: None)
    app = fake_app(False)
    app.save_config = lambda: calls.append("save")
    enabled = QCheckBox()
    controls = agents_section.AgentsControls(SimpleNamespace(app=app), enabled)
    enabled.setChecked(True)
    assert app.config["command_packs"]["agents"] is True
    assert calls == ["save", "sync"]
    state.status = "listening"
    controls.refresh()
    controls.copy_details()
    assert memory_clipboard.text() == "local endpoint details"
    controls.new_token()
    assert calls[-1] == "rotate"
    state.status = "unavailable"
    controls.refresh()
    assert not controls.copy.isEnabled()
    assert "Could not start" in controls.status.text()
    controls.deleteLater()


def test_connection_clipboard_excludes_history_and_expires_without_clobbering(qapp, monkeypatch, memory_clipboard):
    from PySide6.QtWidgets import QCheckBox
    from samsara.ui.settings import agents_section
    timers = []
    monkeypatch.setattr(agents_section.QTimer, "singleShot", lambda delay, callback: timers.append((delay, callback)))
    state = SimpleNamespace(status="listening", port=47832, connection_details=lambda: "local endpoint details")
    monkeypatch.setattr(agents_section, "for_app", lambda app: state)
    controls = agents_section.AgentsControls(SimpleNamespace(app=fake_app()), QCheckBox())
    clipboard = memory_clipboard
    try:
        controls.copy_details()
        assert clipboard.mimeData().hasFormat(agents_section._EXCLUDE_HISTORY)
        assert timers[-1][0] == 60_000
        clipboard.setText("replacement clipboard")
        timers[-1][1]()
        assert clipboard.text() == "replacement clipboard"
        controls.copy_details()
        first_expiry = timers[-1][1]
        controls.copy_details()
        first_expiry()  # Recopy gets its own full 60 seconds.
        assert clipboard.text() == "local endpoint details"
        # The callback continues to work after a Settings visit has ended.
        controls.deleteLater()
        timers[-1][1]()
        assert clipboard.text() == ""
    finally:
        clipboard.clear()
