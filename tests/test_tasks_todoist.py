import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import tasks
from samsara import tasks_store, todoist_client


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_store, "_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks_store, "_data", {"tasks": [], "next_id": 1})
    return tasks_store


def app(config):
    return SimpleNamespace(config=config, audio_coordinator=None, tts_engine=None)


def immediate(monkeypatch):
    from samsara.runtime import thread_registry
    monkeypatch.setattr(thread_registry, "spawn", lambda name, target, args=(), **kw: target(*args))


def test_default_and_missing_token_never_call_network(store, monkeypatch):
    called = Mock()
    monkeypatch.setattr(todoist_client, "quick_add", called)
    tasks.handle_add_to_list(app({}), remainder="local")
    tasks.handle_add_to_list(app({"tasks": {"todoist": {"enabled": True}}}), remainder="still local")
    assert not called.called and len(store.get_active()) == 2


def test_active_posts_text_and_records_id(store, monkeypatch):
    immediate(monkeypatch)
    seen = {}
    def fake(token, text): seen.update(token=token, text=text); return "remote-7"
    monkeypatch.setattr(todoist_client, "quick_add", fake)
    tasks.handle_add_to_list(app({"tasks": {"todoist": {"enabled": True, "api_token": "secret"}}}), remainder="buy milk")
    task = store.get_active()[0]
    assert seen == {"token": "secret", "text": "buy milk"}
    assert task["todoist_id"] == "remote-7" and not task["todoist_pending"]


def test_failure_keeps_local_task_pending_and_sync_clears(store, monkeypatch):
    immediate(monkeypatch)
    monkeypatch.setattr(todoist_client, "quick_add", lambda *a: (_ for _ in ()).throw(OSError("offline")))
    cfg = {"tasks": {"todoist": {"enabled": True, "api_token": "secret"}}}
    tasks.handle_add_to_list(app(cfg), remainder="call doctor")
    assert store.get_active()[0]["todoist_pending"]
    monkeypatch.setattr(todoist_client, "quick_add", lambda *a: "remote-8")
    tasks.handle_sync_tasks(app(cfg))
    assert not store.get_active()[0]["todoist_pending"]


def test_client_uses_v1_quick_add_bearer_and_today(monkeypatch, caplog):
    requests = []
    class Response:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def open_url(request, timeout):
        requests.append((request, timeout))
        return Response(b'{"id":"x"}' if request.full_url == todoist_client.QUICK_ADD_URL else b'{"results":[{"content":"today item"}]}')
    monkeypatch.setattr("urllib.request.urlopen", open_url)
    assert todoist_client.quick_add("secret-token", "buy milk") == "x"
    assert todoist_client.today("secret-token") == ["today item"]
    req, timeout = requests[0]
    assert req.full_url == todoist_client.QUICK_ADD_URL and req.get_header("Authorization") == "Bearer secret-token"
    assert json.loads(req.data) == {"text": "buy milk"} and timeout == 10
    assert "secret-token" not in caplog.text
