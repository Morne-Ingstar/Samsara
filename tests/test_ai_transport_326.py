"""B01 regressions; adapter simulator adapted from audit 313/redirect_privacy.py."""
import ast
import io
import socket
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from requests.adapters import BaseAdapter

from samsara import ai_preferences, ai_transport, ava_readiness, cloud_llm
from samsara import smart_corrections as corrections
from samsara.ava_edit import model as edit_model
from samsara.vision import VisionBridge


class RedirectAdapter(BaseAdapter):
    def __init__(self, status=307):
        self.status = status
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request.url, request.method, len(request.body or b""), kwargs))
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.raw = io.BytesIO()
        response.status_code = self.status if request.method == "POST" else 200
        response.headers["Location"] = "http://example.invalid/collect"
        response._content = b'{"models":[],"message":{"content":"example text"}}'
        return response

    def close(self):
        pass


@pytest.fixture
def adapter(monkeypatch):
    transport = RedirectAdapter()
    session_factory = requests.Session
    sessions = []

    def session():
        client = session_factory()
        client.mount("http://", transport)
        client.mount("https://", transport)
        sessions.append(client)
        return client

    monkeypatch.setattr(ai_transport.requests, "Session", session)
    transport.sessions = sessions
    return transport


@pytest.fixture
def app(monkeypatch):
    from plugins.commands import ask_ollama

    config = {"ollama": {"host": "http://127.0.0.1:11434", "model": "test-model"}}
    consent = ai_preferences.accept_consent(
        config, features=("ava", "editing"), provider=ai_preferences.provider_for(config, "local"),
        accepted_at="2026-09-26T12:00:00Z")
    config = ai_preferences.apply_preferences(
        config, policy="local", ava=True, editing=True, consent=consent, local_model="test-model")
    config["smart_corrections"].update(enabled=True, min_words=0)
    config["vision"] = {"enabled": True}
    instance = SimpleNamespace(config=config, _show_outcome_chip=Mock(), spoken=Mock())
    monkeypatch.setattr(ask_ollama, "speak", instance.spoken)
    monkeypatch.setattr(corrections, "_probe_cache", {})
    return instance


@contextmanager
def server(status=200, location=None):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(len(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(b'{"message":{"content":"ok"}}')

        def log_message(self, *args):
            pass

    listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: listener.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield listener.server_port, received
    finally:
        listener.shutdown()
        listener.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("payload", [
    {"messages": [{"content": "example text"}]},
    {"messages": [{"content": "describe", "images": ["ZXhhbXBsZQ=="]}]},
])
@pytest.mark.parametrize("status", [301, 302, 303, 304, 307, 308])
def test_local_redirect_never_reaches_second_server(monkeypatch, payload, status):
    real_lookup = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        return real_lookup("127.0.0.1" if host == "example.invalid" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with server() as (target_port, target_calls):
        with server(status, f"http://example.invalid:{target_port}/collect") as (source_port, source_calls):
            with pytest.raises(ai_transport.AITransportRefused,
                               match="local AI service tried to redirect - refused"):
                ai_transport.post_local(f"http://127.0.0.1:{source_port}/api/chat", payload, 2)
            assert len(source_calls) == 1 and source_calls[0] > 0
            assert target_calls == []


def test_success_passes_without_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with server() as (port, calls):
        response = ai_transport.post_local(f"http://localhost:{port}/api/chat", {"messages": []}, 2)
        assert response.status_code == 200
        assert response.json()["message"]["content"] == "ok"
        assert len(calls) == 1


@pytest.mark.parametrize("url", [
    "http://example.invalid/api/chat", "http://192.0.2.1/api/chat", "http://localhost.example.invalid",
    "http://127.0.0.1@example.invalid", "http://user@127.0.0.1", "file:///tmp/input",
    "http://127.0.0.1:0", "http://127.0.0.1:invalid", "http://127.0.0.1#fragment",
    "http://127.0.0.1\\@example.invalid", "http://2130706433", "http://127.1",
])
def test_invalid_local_target_is_refused_before_a_session(monkeypatch, url):
    factory = Mock(side_effect=AssertionError("request must not start"))
    monkeypatch.setattr(ai_transport.requests, "Session", factory)
    with pytest.raises(ai_transport.AITransportRefused):
        ai_transport.post_local(url, {}, 1)
    factory.assert_not_called()


@pytest.mark.parametrize("url", [
    "https://example.invalid/v1", "https://api.openai.com.example.invalid/v1",
    "http://api.openai.com/v1", "https://api.openai.com:8443/v1",
    "https://user@api.openai.com/v1",
])
def test_cloud_refuses_non_provider_origin_before_a_session(monkeypatch, url):
    factory = Mock(side_effect=AssertionError("request must not start"))
    monkeypatch.setattr(ai_transport.requests, "Session", factory)
    with pytest.raises(ai_transport.AITransportRefused):
        ai_transport.post_cloud(url, {}, 1)
    factory.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_cloud_redirect_is_refused_and_environment_is_disabled(adapter, status):
    adapter.status = status
    with pytest.raises(ai_transport.AITransportRefused, match="cloud AI service tried to redirect"):
        ai_transport.post_cloud("https://api.openai.com/v1/chat/completions", {}, 1)
    assert len(adapter.calls) == 1
    assert adapter.calls[0][3]["proxies"] == {}
    assert all(client.trust_env is False for client in adapter.sessions)


def test_localhost_is_pinned_without_dns_lookup(adapter, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("unexpected DNS")))
    adapter.status = 200
    ai_transport.post_local("http://localhost:11434/api/chat", {}, 1)
    assert adapter.calls[0][0] == "http://127.0.0.1:11434/api/chat"


def test_configured_alias_must_resolve_only_to_loopback(adapter, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 11434))])
    adapter.status = 200
    ai_transport.post_local("http://ollama.internal:11434/api/chat", {}, 1,
                            ollama_host="http://ollama.internal:11434")
    assert adapter.calls[0][0] == "http://127.0.0.1:11434/api/chat"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 11434))])
    with pytest.raises(ai_transport.AITransportRefused):
        ai_transport.post_local("http://ollama.internal:11434/api/chat", {}, 1,
                                ollama_host="http://ollama.internal:11434")
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("feature", ["corrections", "vision", "editing", "warmup", "ava"])
def test_production_local_sinks_refuse_replay_and_notify(app, adapter, monkeypatch, feature):
    cloud = Mock(side_effect=AssertionError("local refusal must not fall back to cloud"))
    monkeypatch.setattr(cloud_llm, "send", cloud)
    monkeypatch.setattr(cloud_llm, "send_ex", cloud)
    if feature == "corrections":
        assert corrections.smart_correct("example text", app) == "example text"
    elif feature == "vision":
        assert VisionBridge(app).describe("ZXhhbXBsZQ==", "describe") is None
    elif feature == "editing":
        assert edit_model._call_ollama(app, "example text", 1)[0] is None
    elif feature == "warmup":
        assert ava_readiness.warm_configured_provider(app).failure_kind == ava_readiness.UNREACHABLE
    else:
        from plugins.commands import ask_ollama

        app._ava_memory = Mock()
        app._ava_memory.get_messages.return_value = [{"role": "user", "content": "example text"}]
        assert ask_ollama.ask_model("example text", app, system="example system").failure_kind == ava_readiness.UNREACHABLE
        app._ava_memory.pop_last_if_user.assert_called_once()
    assert sum(method == "POST" for _, method, _, _ in adapter.calls) == 1
    assert all(url.startswith("http://127.0.0.1:") for url, _, _, _ in adapter.calls)
    app._show_outcome_chip.assert_called_once_with(
        "local AI service tried to redirect - refused", "error")
    assert app.spoken.call_count == 1
    cloud.assert_not_called()


def test_revoked_local_consent_prevents_any_transport(app, adapter):
    app.config["ava"]["provider_policy"] = "off"
    assert corrections.smart_correct("example text", app) == "example text"
    assert VisionBridge(app).describe("ZXhhbXBsZQ==", "describe") is None
    assert adapter.calls == []


@pytest.mark.parametrize("method", ["send", "send_json", "search", "probe"])
def test_cloud_sinks_refuse_redirects_and_notify(app, adapter, method):
    app.config["cloud_llm"] = {"enabled": True, "api_key": "unused", "provider": "openai"}
    if method == "send":
        result = cloud_llm.send("example system", "example text", app)
        assert "tried to redirect - refused" in result
    elif method == "send_json":
        result = cloud_llm.send_json("example system", "example text", app)
        assert "tried to redirect - refused" in result
    elif method == "search":
        result = cloud_llm.send_web_search("example system", [], app)
        assert result.error_kind == "unreachable"
    else:
        adapter.send = Mock(return_value=_redirect_response())
        assert cloud_llm.check_available(app)[0] is False
    app._show_outcome_chip.assert_called_once_with(
        "cloud AI service tried to redirect - refused", "error")
    app.spoken.assert_called_once()
    assert len(adapter.calls) <= 1


def _redirect_response():
    response = requests.Response()
    response.status_code = 307
    response._content = b""
    response.raw = io.BytesIO()
    return response


def test_health_probes_refuse_redirects(app):
    response = requests.Response()
    response.status_code = 307
    response._content = b""
    response.raw = io.BytesIO()
    probe = Mock(return_value=response)
    assert ava_readiness.probe_configured_provider(app, http_get=probe) == (
        "ollama", ava_readiness.UNREACHABLE)
    assert probe.call_args.kwargs["allow_redirects"] is False
    app._show_outcome_chip.assert_called_once()


def test_call_sites_do_not_bypass_shared_transport():
    root = Path(__file__).resolve().parents[1]
    paths = ("samsara/smart_corrections.py", "samsara/ava_edit/model.py", "samsara/ava_readiness.py",
             "samsara/cloud_llm.py", "samsara/vision/__init__.py", "plugins/commands/ask_ollama.py")
    for path in paths:
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                       and node.value.id == "requests" and node.attr in {"post", "get", "request", "Session"}
                       for node in ast.walk(tree)), path
    assert isinstance(corrections._session, ai_transport.LocalClient)
    assert "dictation" not in sys.modules
