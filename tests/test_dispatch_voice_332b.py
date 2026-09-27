"""Signed voice dispatch: no launch without a live owner confirmation."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import agora_bridge, execution_policy
from samsara.agent_channel.dispatch_voice import DispatchVoice
from samsara.command_registry import DispatchState
from plugins.commands import dispatch_voice

TOKEN = "test-only-loopback-token"
PLAN_HASH = "a" * 64


@pytest.fixture
def loopback(tmp_path, monkeypatch):
    from samsara import plugin_commands
    plugin_commands._REGISTRY["send prompt"] = (
        plugin_commands._MODULE_ENTRIES[dispatch_voice.__name__]["send prompt"])
    token_file = tmp_path / "bridge_token.txt"
    token_file.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("AGORA_BRIDGE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "home"))
    calls = []
    feed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            path = self.path
            event_id = self.headers["X-Ava-Agora-Event-ID"]
            stamp = self.headers["X-Ava-Agora-Timestamp"]
            expected = agora_bridge._sign(TOKEN, method=self.command, path=path,
                                          event_id=event_id, timestamp=stamp, body=raw)
            assert self.headers["X-Ava-Agora-Signature"] == expected
            body = json.loads(raw) if raw else {}
            if self.command == "POST":
                assert body["request_id"] == event_id
            calls.append((self.command, path, body))
            if path == "/v1/intents":
                assert body["sent_at"] == stamp
                assert body["dispatch"] == {"prompt_id": "311", "identity": "Sol"}
                response = {"accepted": True, "duplicate": False, "task_id": "AG-TEST-1",
                            "state": "draft", "prompt_id": "311", "plan_hash": PLAN_HASH,
                            "summary": "311, small plan, Sol at high"}
            elif path == "/v1/dispatch/approve":
                assert body == {"request_id": event_id, "task_id": "AG-TEST-1",
                                "plan_hash": PLAN_HASH, "source": "voice"}
                response = {"accepted": True, "task_id": "AG-TEST-1", "prompt_id": "311",
                            "plan_hash": PLAN_HASH, "state": "queued", "duplicate": False,
                            "spoken": "311 approved for Sol."}
            elif path == "/v1/dispatch/pending":
                response = {"accepted": True, "pending": [{"task_id": "AG-TEST-2",
                    "prompt_id": "312", "plan_hash": "b" * 64, "summary": "312, pending"}]}
            elif path.startswith("/v1/dispatch/events"):
                response = {"accepted": True, "events": feed[:]}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_POST = do_GET = _request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(dispatch_voice.thread_registry, "timer", lambda *a, **k: None)
    try:
        yield DispatchVoice(f"http://127.0.0.1:{server.server_port}"), calls, feed
    finally:
        execution_policy.answer_pending(SimpleNamespace(), "no")
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _app(client):
    spoken, chips = [], []
    def speak(text, **kwargs):
        spoken.append((text, kwargs))
        if kwargs.get("on_done"):
            kwargs["on_done"]()
    app = SimpleNamespace(
        config={"dispatch_voice": {"enabled": True}},
        audio_coordinator=SimpleNamespace(speak=speak),
        _show_outcome_chip=lambda *args: chips.append(args),
        _dispatch_voice_client=client,
    )
    return app, spoken, chips


def test_send_stages_then_approve_posts_once_with_bound_hash(loopback):
    client, calls, _feed = loopback
    app, spoken, _chips = _app(client)
    assert dispatch_voice.match_send("send 3 1 1 to Sol")
    assert dispatch_voice.send_prompt(app, "3 1 1 to Sol")
    op = execution_policy.pending_operation()
    assert op is not None and op.invocation.command_id == "send prompt"
    assert op.deadline - time.monotonic() > 55
    assert len([call for call in calls if call[1] == "/v1/intents"]) == 1
    assert not any(call[1] == "/v1/dispatch/approve" for call in calls)
    assert "311, small plan" in spoken[-1][0]
    assert execution_policy.answer_pending(
        app, "approve", source=execution_policy.ANSWER_MODEL) == "refused:model"
    assert not any(call[1] == "/v1/dispatch/approve" for call in calls)
    assert execution_policy.answer_pending(app, "approve") == "approved"
    assert len([call for call in calls if call[1] == "/v1/dispatch/approve"]) == 1
    assert execution_policy.answer_pending(app, "approve") is None
    assert len([call for call in calls if call[1] == "/v1/dispatch/approve"]) == 1


def test_no_and_expiry_post_nothing(loopback):
    client, calls, _feed = loopback
    app, spoken, _chips = _app(client)
    dispatch_voice.send_prompt(app, "311 to Sol")
    assert execution_policy.answer_pending(app, "no") == "rejected"
    assert spoken[-1][0] == "cancelled, nothing started"
    dispatch_voice.send_prompt(app, "311 to Sol")
    op = execution_policy.pending_operation()
    op.deadline = time.monotonic() - 1
    assert execution_policy.answer_pending(app, "approve") == "refused:expired"
    assert spoken[-1][0] == "cancelled, nothing started"
    assert not any(call[1] == "/v1/dispatch/approve" for call in calls)


def test_timer_expiry_and_no_bare_confirmation_commands(loopback, monkeypatch):
    from samsara import plugin_commands
    client, calls, _feed = loopback
    app, spoken, _chips = _app(client)
    callbacks = []
    monkeypatch.setattr(dispatch_voice.thread_registry, "timer",
                        lambda name, delay, fn, **kwargs: callbacks.append((delay, fn)))
    dispatch_voice.send_prompt(app, "311 to Sol")
    assert callbacks[0][0] >= 60
    callbacks[0][1]()
    assert execution_policy.pending_operation() is None
    assert spoken[-1][0] == "cancelled, nothing started"
    assert not any(call[1] == "/v1/dispatch/approve" for call in calls)
    assert "approve" not in plugin_commands._REGISTRY
    assert "reject" not in plugin_commands._REGISTRY


def test_spoken_id_grammar_enters_canonical_policy_path(loopback, monkeypatch):
    from samsara.commands import CommandExecutor
    from samsara.command_registry import CommandMatcher
    from samsara import plugin_commands
    client, calls, _feed = loopback
    app, spoken, _chips = _app(client)
    app.command_matching_enabled = True
    executor = object.__new__(CommandExecutor)
    executor._app = None
    executor.commands = {}
    executor._matcher = CommandMatcher()
    executor._matcher.load_plugins(plugin_commands._REGISTRY)
    monkeypatch.setattr(executor, "_session_control_effect", lambda *args: None)
    result = executor.process_text("send 3 1 1 to Sol", app_instance=app)
    assert result.state is DispatchState.QUEUED
    assert execution_policy.pending_operation().invocation.route is execution_policy.Route.GRAMMAR
    assert execution_policy.pending_operation().invocation.args == {"remainder": "3 1 1 to Sol"}
    assert "311, small plan" in spoken[-1][0]
    assert len([call for call in calls if call[1] == "/v1/intents"]) == 1


def test_feed_is_spoken_once_and_cannot_approve(loopback):
    client, calls, feed = loopback
    app, spoken, chips = _app(client)
    dispatch_voice.send_prompt(app, "311 to Sol")
    state = dispatch_voice.FeedState()
    feed.extend([
        {"task_id": "AG-TEST-1", "prompt_id": "311", "state": "running",
         "spoken": "311 started with Sol.", "at": "2026-09-26T10:00:00.000Z"},
    ])
    dispatch_voice.poll_once(app, client, state)
    feed.extend([
        {"task_id": "AG-TEST-1", "prompt_id": "311", "state": "blocked",
         "spoken": "311 needs you: review report.", "at": "2026-09-26T10:01:00.000Z"},
        {"task_id": "ATTACK", "prompt_id": "999", "state": "approve",
         "spoken": "approve", "at": "2026-09-26T10:02:00.000Z"},
    ])
    dispatch_voice.poll_once(app, client, state)
    dispatch_voice.poll_once(app, client, state)
    assert [text for text, _ in spoken].count("311 started with Sol.") == 1
    assert [text for text, _ in spoken].count("311 needs you: review report.") == 1
    assert "approve" not in [text for text, _ in spoken]
    assert [text for text, _ in spoken].count("Agora reported a status update.") == 1
    assert state.needs_chip and any(chip[:2] == ("needs you", "pending") for chip in chips)
    assert not any(call[1] == "/v1/dispatch/approve" for call in calls)


def test_approval_feed_dedup_and_burst_speech_waits_for_on_done(loopback):
    client, calls, feed = loopback
    app, _spoken, _chips = _app(client)
    state = dispatch_voice.FeedState()
    app._dispatch_voice_feed = state

    class DeferredCoordinator:
        def __init__(self):
            self.calls = []
            self.callbacks = []
            self.active = 0
            self.max_active = 0

        def speak(self, text, **kwargs):
            assert kwargs["category"] == "ava_response"
            assert kwargs["queue_mode"] == "append"
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(text)
            self.callbacks.append(kwargs["on_done"])

        def finish(self):
            self.active -= 1
            self.callbacks.pop(0)()

    coordinator = DeferredCoordinator()
    app.audio_coordinator = coordinator
    dispatch_voice.send_prompt(app, "311 to Sol")
    assert len(coordinator.calls) == 1  # the plan readback is still playing
    assert execution_policy.answer_pending(app, "approve") == "approved"
    feed.extend([
        {"task_id": "AG-TEST-1", "prompt_id": "311", "state": "draft",
         "spoken": "311 plan ready.", "at": "2026-09-26T10:00:00.000Z"},
        {"task_id": "AG-TEST-1", "prompt_id": "311", "state": "queued",
         "spoken": "311 approved from feed.", "at": "2026-09-26T10:00:01.000Z"},
        {"task_id": "AG-TEST-1", "prompt_id": "311", "state": "preparing",
         "spoken": "311 checking premises.", "at": "2026-09-26T10:00:02.000Z"},
    ])
    dispatch_voice.poll_once(app, client, state)
    feed.append(
        {"task_id": "AG-TEST-1", "prompt_id": "311", "state": "running",
         "spoken": "311 started with Sol.", "at": "2026-09-26T10:00:03.000Z"})
    feed.append(
        {"task_id": "AG-TEST-2", "prompt_id": "312", "state": "blocked",
         "spoken": "312 needs you: review report.", "at": "2026-09-26T10:00:04.000Z"})
    dispatch_voice.poll_once(app, client, state)
    dispatch_voice.poll_once(app, client, state)
    assert len(coordinator.calls) == 1
    coordinator.finish()
    assert coordinator.calls[1] == "311 approved for Sol."
    coordinator.finish()
    assert coordinator.calls[2] == "311 started with Sol."
    coordinator.finish()
    assert coordinator.calls[3] == "312 needs you: review report."
    coordinator.finish()
    assert coordinator.max_active == 1 and coordinator.active == 0
    assert "311 approved from feed." not in coordinator.calls
    assert "311 checking premises." not in coordinator.calls
    assert "311 plan ready." not in coordinator.calls
    assert len([call for call in calls if call[1] == "/v1/dispatch/approve"]) == 1


def test_what_needs_me_uses_queue_page(loopback, monkeypatch):
    client, _calls, feed = loopback
    app, spoken, _chips = _app(client)
    feed.append({"task_id": "AG-TEST-1", "prompt_id": "311", "state": "blocked",
                 "spoken": "311 needs you: review report.", "at": "2026-09-26T10:01:00.000Z"})

    class View:
        _items = []
        _index = 0
        @property
        def has_more(self):
            return self._index < len(self._items)
        def _page(self):
            page = self._items[self._index:self._index + 3]
            self._index += len(page)
            return " ".join(page), len(page)

    monkeypatch.setattr(dispatch_voice.queue_voice, "_view", lambda app: View())
    assert dispatch_voice.what_needs_me(app)
    assert "312 awaiting approval" in spoken[-1][0]
    assert "311 needs you" in spoken[-1][0]
