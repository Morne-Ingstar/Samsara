"""Local job lifecycle, verbatim command routing, and UI-to-API import (no GPU)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.agent_channel.comfy import (ComfyClient, ComfyError, JobCancelled,
                                         JobTimeout, ServerDown, local_url)
from tools.comfy_import_workflow import convert_workflow


@pytest.fixture
def server():
    state = SimpleNamespace(posts=[], gets=[], history_calls=0, finish_after=3,
        submitted=threading.Event(), stop=threading.Event(), websocket=False,
        filename="Klein_001.png", interrupt=False, cloud=False,
        running="job-320", pending=[], status="success", ws_sent=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, data, status=200, binary=False):
            data = data if binary else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "image/png" if binary else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state.gets.append(self.path)
            if self.path.startswith("/ws"):
                if not state.websocket:
                    return self.reply({}, 404)
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                state.submitted.wait(2)
                for prompt_id, value in (("someone-else", 99), ("job-320", 40)):
                    payload = json.dumps({"type": "progress", "data": {
                        "prompt_id": prompt_id, "value": value, "max": 100}}).encode()
                    header = bytes([0x81, len(payload)]) if len(payload) < 126 else b"\x81\x7e" + struct.pack("!H", len(payload))
                    self.wfile.write(header + payload)
                    self.wfile.flush()
                state.ws_sent.set()
                return
            if self.path == "/system_stats":
                return self.reply({"system": {"comfyui_version": "0.36.0"}})
            if self.path == "/object_info":
                return self.reply({"CLIPTextEncode": {"api_node": state.cloud}})
            if self.path.startswith("/history/"):
                state.history_calls += 1
                if state.finish_after is None or state.history_calls < state.finish_after:
                    return self.reply({})
                return self.reply({"job-320": {"status": {"completed": True, "status_str": state.status},
                    "outputs": {"90": {"images": [{"filename": state.filename,
                        "subfolder": "", "type": "output"}]}}}})
            if self.path == "/queue":
                return self.reply({"queue_running": [[0, state.running]] if state.running else [],
                                   "queue_pending": [[1, p] for p in state.pending]})
            if self.path.startswith("/view?"):
                return self.reply(b"\x89PNG\r\n\x1a\nfixture", binary=True)
            return self.reply({}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            state.posts.append((self.path, body))
            if self.path == "/prompt":
                state.submitted.set()
                return self.reply({"prompt_id": "job-320", "number": 0, "node_errors": {}})
            if self.path == "/interrupt":
                state.interrupt = True
                return self.reply({})
            if self.path == "/queue":
                state.pending = [p for p in state.pending if p not in body.get("delete", [])]
                return self.reply({})
            return self.reply({}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield state
    finally:
        state.stop.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)


@pytest.fixture
def config(tmp_path):
    graph = {
        "74": {"class_type": "CLIPTextEncode", "inputs": {"text": "template"}},
        "73": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
        "68": {"class_type": "PrimitiveInt", "inputs": {"value": 768}},
        "69": {"class_type": "PrimitiveInt", "inputs": {"value": 768}},
    }
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return {"comfy": {"auto_open": False, "workflows": {"klein_simple": {
        "path": str(path), "prompt": ["74", "inputs", "text"],
        "seed": ["73", "inputs", "noise_seed"], "width": ["68", "inputs", "value"],
        "height": ["69", "inputs", "value"],
    }}}}


def client(server, config, tmp_path, **kwargs):
    return ComfyClient(server.url, config=config, home=tmp_path, poll_interval=.015,
                       progress_interval=.01, request_timeout=.5, **kwargs)


def test_http_job_verbatim_prompt_outputs_and_index(server, config, tmp_path):
    text = 'Teal "snake";  Keep CASE.\nTwo lines.'
    spoken = []
    async def run():
        async with client(server, config, tmp_path) as api:
            job = await api.submit(text, overrides={"seed": 123, "width": 1024})
            record = await api.poll(job, on_progress=lambda job: spoken.append(job.state))
            return await api.fetch_outputs(job, record)
    result = asyncio.run(run())
    graph = next(body["prompt"] for route, body in server.posts if route == "/prompt")
    assert graph["74"]["inputs"]["text"] == text
    assert graph["73"]["inputs"]["noise_seed"] == 123
    assert graph["68"]["inputs"]["value"] == 1024
    assert spoken and result.elapsed > 0
    assert result.paths[0].is_relative_to(tmp_path / "images")
    assert result.paths[0].read_bytes().startswith(b"\x89PNG")
    assert "Two lines." in (tmp_path / "images" / "index.md").read_text()
    assert json.loads((tmp_path / "workflow.json").read_text())["74"]["inputs"]["text"] == "template"


def test_websocket_progress_filters_other_jobs_and_http_fallback(server, config, tmp_path):
    server.websocket = True
    server.finish_after = 8
    progress = []
    async def run():
        async with client(server, config, tmp_path) as api:
            job = await api.submit("fixture")
            await api.poll(job, on_progress=lambda j: progress.append(j.percent))
    asyncio.run(run())
    assert server.ws_sent.is_set()
    assert 40 in progress and 99 not in progress
    assert server.history_calls >= 8


def test_timeout_does_not_claim_cancel_or_interrupt(server, config, tmp_path):
    server.finish_after = None
    async def run():
        async with client(server, config, tmp_path) as api:
            job = await api.submit("fixture")
            with pytest.raises(JobTimeout):
                await api.poll(job, timeout_s=.045)
            assert job.state == "timed out"
    asyncio.run(run())
    assert not server.interrupt


@pytest.mark.parametrize("running", ["job-320", "other-job"])
def test_cancel_only_own_job(server, config, tmp_path, running):
    server.running = running
    server.pending = ["job-320", "other-job"]
    async def run():
        async with client(server, config, tmp_path) as api:
            job = await api.submit("fixture")
            stop = threading.Event()
            stop.set()
            with pytest.raises(JobCancelled):
                await api.poll(job, cancel=stop)
    asyncio.run(run())
    assert server.pending == ["other-job"]
    assert server.interrupt == (running == "job-320")
    assert all(body == {"prompt_id": "job-320"} for route, body in server.posts if route == "/interrupt")


def test_server_down():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    async def run():
        async with ComfyClient(f"http://127.0.0.1:{port}", request_timeout=.2) as api:
            with pytest.raises(ServerDown, match="isn't running"):
                await api.health()
    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.1:8001", "http://127.0.0.1/evil", "http://user:pass@127.0.0.1"])
def test_only_loopback_urls(url):
    with pytest.raises(ComfyError):
        local_url(url)


def test_cloud_nodes_and_output_traversal_refused(server, config, tmp_path):
    async def run():
        async with client(server, config, tmp_path) as api:
            server.cloud = True
            with pytest.raises(ComfyError, match="Cloud"):
                await api.submit("fixture")
            assert not server.submitted.is_set()
            server.cloud = False
            job = await api.submit("fixture")
            server.filename = "../elsewhere.png"
            record = await api.poll(job)
            with pytest.raises(ComfyError, match="path"):
                await api.fetch_outputs(job, record)
    asyncio.run(run())
    assert not list(tmp_path.rglob("*.png"))


def workflow_fixture(named):
    nodes = [
        {"id": 1, "type": "CLIPTextEncode", "inputs": [], "widgets_values": ["fixture"]},
        {"id": 2, "type": "RandomNoise", "inputs": [], "widgets_values": [9, "randomize"]},
        {"id": 3, "type": "EmptyLatentImage", "inputs": [], "widgets_values": [768, 512, 1]},
    ]
    info = {
        "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {}]}}},
        "RandomNoise": {"input": {"required": {"noise_seed": ["INT", {"control_after_generate": True}]}}},
        "EmptyLatentImage": {"input": {"required": {"width": ["INT", {}], "height": ["INT", {}], "batch_size": ["INT", {}]}}},
    }
    if named:
        nodes[0]["widgets_values_named"] = {"text": "fixture"}
        nodes[1]["widgets_values_named"] = {"noise_seed": 9, "control_after_generate": "randomize"}
        nodes[2]["widgets_values_named"] = {"width": 768, "height": 512, "batch_size": 1}
    return {"nodes": nodes, "links": []}, info


@pytest.mark.parametrize("named", [False, True])
def test_import_detects_fields_and_skips_ui_seed_control(named):
    ui, info = workflow_fixture(named)
    graph, fields = convert_workflow(ui, info)
    assert fields == {"prompt": ["1", "inputs", "text"], "seed": ["2", "inputs", "noise_seed"],
                      "width": ["3", "inputs", "width"], "height": ["3", "inputs", "height"]}
    assert graph["2"]["inputs"] == {"noise_seed": 9}
    assert "class_type" in graph["1"] and "widgets_values" not in graph["1"]


def test_import_rejects_unsupported_node():
    ui, info = workflow_fixture(True)
    ui["nodes"][0]["mode"] = 4
    with pytest.raises(ValueError, match="unsupported"):
        convert_workflow(ui, info)


def fake_app(config):
    speaker = Mock()
    speaker.speak.side_effect = lambda text, **kw: SimpleNamespace(utterance_id="voice")
    return SimpleNamespace(config=config, audio_coordinator=speaker, _show_outcome_chip=Mock())


def test_command_worker_feedback_auto_open_and_followups(server, config, tmp_path, monkeypatch):
    from plugins.commands import comfy, web_shortcuts
    config["comfy"]["auto_open"] = True
    app = fake_app(config)
    tasks, opened = [], []
    monkeypatch.setattr(comfy.thread_registry, "spawn", lambda name, fn: tasks.append(fn))
    monkeypatch.setattr(comfy, "ComfyClient", lambda *_a, **_kw: client(server, config, tmp_path))
    monkeypatch.setattr(comfy.os, "startfile", lambda path: opened.append(path))
    comfy.make_image(app, "a fixture.")
    assert len(tasks) == 1
    tasks.pop()()
    assert len(opened) == 1
    spoken = [call.args[0] for call in app.audio_coordinator.speak.call_args_list]
    assert spoken[0] == "Sending to ComfyUI."
    assert any("one image" in text and "seconds" in text and "It's open." in text
               and "Say open it" not in text for text in spoken)
    assert comfy._state(app).opened is True
    comfy.where_image(app, "")
    where_spoken = app.audio_coordinator.speak.call_args.args[0]
    assert "Samsara images folder" in where_spoken
    assert "Say open it" not in where_spoken
    assert all("a fixture" not in text for text in spoken)
    assert "Klein_001.png" in app._show_outcome_chip.call_args.args[0]
    comfy.open_image(app, "")
    web_shortcuts.open_site(app, "")
    assert len(opened) == 3
    comfy.make_amended(app, "Teal; keep punctuation.")
    assert len(tasks) == 1
    tasks.pop()()
    prompts = [b["prompt"]["74"]["inputs"]["text"] for route, b in server.posts if route == "/prompt"]
    assert prompts == ["a fixture.", "a fixture.\nTeal; keep punctuation."]
    assert comfy._state(app).job.state == "complete"


def test_registry_preserves_prompt_terminators_and_case():
    from plugins.commands import comfy
    from samsara.command_registry import CommandMatcher
    from samsara.plugin_commands import _REGISTRY
    from samsara.plugin_commands import _reinstall_module_commands
    _reinstall_module_commands(comfy)
    matcher = CommandMatcher()
    matcher.load_plugins({k: v for k, v in _REGISTRY.items() if v["source"] == comfy.__name__})
    matcher.freeze()
    assert all(len(phrase.split()) > 1 for phrase, entry in _REGISTRY.items()
               if entry["source"] == comfy.__name__)
    assert matcher.match("render a fixture")[0] is None
    for phrase in ("give me an image of", "render an image of", "make it again but"):
        entry, text = matcher.match(phrase + ' "Teal"; line.  ')
        assert entry.handler.verbatim_argument
        assert text == '"Teal"; line.  '


def test_plugin_server_down_speaks_without_beep(config, monkeypatch):
    from plugins.commands import comfy
    from samsara.agent_channel.comfy import ServerDown
    app = fake_app(config)
    monkeypatch.setattr(comfy.thread_registry, "spawn", lambda name, fn: fn())
    async def down(self, *_a, **_kw):
        raise ServerDown("ComfyUI isn't running")
    monkeypatch.setattr(ComfyClient, "submit", down)
    comfy.make_image(app, "fixture")
    assert app.audio_coordinator.speak.call_args.args[0] == "ComfyUI isn't running"
    assert not comfy._state(app).active
    assert app._show_outcome_chip.call_args.args[1] == "warning"


def test_real_policy_submit_is_read_and_needs_no_confirmation(config):
    from plugins.commands import comfy
    from samsara import execution_policy as policy, plugin_commands
    plugin_commands._reinstall_module_commands(comfy)
    app = fake_app(config)
    inv = policy.Invocation("make an image of", {"remainder": "fixture"}, policy.Route.EXACT,
                            policy.capture_generation(app))
    decision = policy.authorize(inv, app=app)
    assert isinstance(decision, policy.Allowed)
    assert decision.risk == "read"


def test_make_another_and_auto_open_false(server, config, tmp_path, monkeypatch):
    from plugins.commands import comfy
    app = fake_app(config)
    opened = Mock()
    monkeypatch.setattr(comfy.thread_registry, "spawn", lambda name, fn: fn())
    monkeypatch.setattr(comfy, "ComfyClient", lambda *_a, **_kw: client(server, config, tmp_path))
    monkeypatch.setattr(comfy.os, "startfile", opened)
    comfy.make_image(app, "fixture")
    comfy.make_another(app, "")
    graphs = [body["prompt"] for route, body in server.posts if route == "/prompt"]
    assert len(graphs) == 2
    assert graphs[0]["73"]["inputs"] != graphs[1]["73"]["inputs"]
    assert graphs[0]["74"]["inputs"] == graphs[1]["74"]["inputs"]
    opened.assert_not_called()
    assert comfy._state(app).opened is False
    comfy.where_image(app, "")
    assert "Say open it" in app.audio_coordinator.speak.call_args.args[0]


def test_failed_history_is_not_completion(server, config, tmp_path):
    server.status = "error"
    async def run():
        async with client(server, config, tmp_path) as api:
            job = await api.submit("fixture")
            with pytest.raises(ComfyError, match="could not render"):
                await api.poll(job)
    asyncio.run(run())


def test_import_resolves_shared_primitive_dimensions():
    ui, info = workflow_fixture(True)
    info["PrimitiveInt"] = {"input": {"required": {"value": ["INT", {}]}}}
    for node_id, value in ((4, 768), (5, 512)):
        ui["nodes"].append({"id": node_id, "type": "PrimitiveInt", "inputs": [],
                            "widgets_values_named": {"value": value}})
    ui["nodes"][2]["inputs"] = [{"name": "width", "link": 7}, {"name": "height", "link": 8}]
    ui["links"] = [[7, 4, 0, 3, 0, "INT"], [8, 5, 0, 3, 1, "INT"]]
    graph, fields = convert_workflow(ui, info)
    assert graph["3"]["inputs"]["width"] == ["4", 0]
    assert fields["width"] == ["4", "inputs", "value"]
    assert fields["height"] == ["5", "inputs", "value"]


@pytest.mark.parametrize("enabled", [True, False])
def test_ava_conversation_routes_image_verbatim_without_calling_model(config, tmp_path, monkeypatch, enabled):
    from plugins.commands import comfy, ask_ollama
    from samsara import plugin_commands
    from samsara.commands import CommandExecutor
    plugin_commands._reinstall_module_commands(comfy)
    config["command_packs"] = {"ai": enabled}
    app = fake_app(config)
    commands = tmp_path / "commands.json"
    commands.write_text('{"commands": {}}')
    app.command_executor = CommandExecutor(commands, app=app)
    work = []
    monkeypatch.setattr(comfy.thread_registry, "spawn", lambda name, fn, **kw: work.append((name, fn)))
    monkeypatch.setattr(ask_ollama, "_ai_state", lambda app: SimpleNamespace(allowed=True))
    model = Mock(side_effect=AssertionError("image text must not reach a model"))
    monkeypatch.setattr(ask_ollama, "ask_ollama", model)
    done = Mock()
    result = ask_ollama.handle_ask_ava(app, 'give me an image of Teal;  CASE.\nTwo lines.', on_done=done)
    model.assert_not_called()
    done.assert_called_once()
    if enabled:
        assert result.state.value == "queued"
        assert [name for name, _fn in work] == ["comfy.image"]
        # The actual render function retains the untouched argument in its closure.
        captured = []
        monkeypatch.setattr(comfy, "_start", lambda app, text: captured.append(text) or comfy._result())
        ask_ollama.handle_ask_ava(app, 'give me an image of Teal;  CASE.\nTwo lines.')
        assert captured == ['Teal;  CASE.\nTwo lines.']
    else:
        assert result.state.value == "rejected"
        assert work == []
        assert "unavailable" in app.audio_coordinator.speak.call_args.args[0]


def test_unknown_submission_retains_identity_and_never_reposts(server, config, tmp_path, monkeypatch):
    from samsara.agent_channel.comfy import SubmissionUnknown
    async def run():
        async with client(server, config, tmp_path) as api:
            original = api._request
            async def lost(method, path, **kw):
                reply = await original(method, path, **kw)
                if path == "/prompt":
                    raise SubmissionUnknown("Lost response")
                return reply
            monkeypatch.setattr(api, "_request", lost)
            with pytest.raises(SubmissionUnknown):
                await api.submit("fixture")
            assert api.job.prompt_id == server.posts[0][1]["prompt_id"]
            assert len([p for p in server.posts if p[0] == "/prompt"]) == 1
    asyncio.run(run())


def test_staged_context_image_request_is_claimed_without_guessing_or_model(config):
    from plugins.commands import comfy
    app = fake_app(config)
    app.command_executor = Mock()
    result = comfy.try_voice_command(app, "STAGED TEXT:\nfixture\n\ngive me an image of that shape")
    assert result.state.value == "rejected"
    app.command_executor.execute_canonical.assert_not_called()
