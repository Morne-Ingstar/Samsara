"""MCP speak keeps a full agent answer available to Samsara follow-ups."""
from contextlib import closing
from http.client import HTTPConnection
import json
import logging
from types import SimpleNamespace

import pytest

from samsara import agent_server


class Coordinator:
    def __init__(self):
        self.calls = []

    def speak(self, text, **kwargs):
        self.calls.append((text, kwargs))
        done = kwargs.get("on_done")
        if done:
            done()
        return SimpleNamespace(utterance_id="fake-speech")


@pytest.fixture
def service(tmp_path):
    app = SimpleNamespace(
        config={"command_packs": {"agents": True}, "tts": {"feedback_level": "brief", "enabled": True},
                "ava": {"provider_policy": "off"}},
        audio_coordinator=Coordinator(), _running=True, recording=False,
        wake_word_active=False, continuous_active=False,
        _session_mode_manager=SimpleNamespace(mode=SimpleNamespace(value="dictate")),
        _schedule_ui=lambda callback: callback(),
    )
    server = agent_server.AgentServer(app, profile=tmp_path, port=0)
    assert server.start()
    yield server
    server.stop()
    for token in tmp_path.rglob("token.json"):
        token.unlink()


def call(server, text, full_text=None, *, client_name="Speak test client"):
    args = {"text": text}
    if full_text is not None:
        args["full_text"] = full_text
    params = {"name": "speak", "arguments": args,
              "_meta": {"io.modelcontextprotocol/protocolVersion": agent_server.VERSIONS[0],
                        "io.modelcontextprotocol/clientInfo": {"name": client_name, "version": "1"},
                        "io.modelcontextprotocol/clientCapabilities": {}}}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + server._token,
               "MCP-Protocol-Version": agent_server.VERSIONS[0], "Mcp-Method": "tools/call",
               "Mcp-Name": "speak"}
    with closing(HTTPConnection("127.0.0.1", server.port, timeout=4)) as connection:
        connection.request("POST", "/mcp", body=json.dumps(payload), headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())


def test_speak_keeps_full_text_for_followups_and_returns_contract(service):
    status, payload = call(service, "Short spoken answer.",
                           "Short spoken answer. Complete answer with details.")
    assert status == 200
    assert payload["result"]["structuredContent"] == {
        "spoken": True, "full_text_kept": True,
        "follow_ups": ["repeat that", "read the rest"],
    }
    assert service.app.audio_coordinator.calls[0][0] == "Short spoken answer."
    state = service.app._cli_agent_voice_state
    assert state["last"] == "Short spoken answer."
    assert state["rest"] == "Complete answer with details."


def test_speak_without_full_text_keeps_existing_reply(service):
    _, payload = call(service, "Short spoken answer.")
    assert payload["result"]["structuredContent"] == {"status": "spoken", "reason": "completed"}
    assert not hasattr(service.app, "_cli_agent_voice_state")


def test_spoken_text_is_capped_at_60_words_and_logs_client(service, caplog):
    text = "w " * 70
    with caplog.at_level(logging.WARNING, logger=agent_server.__name__):
        call(service, text, "Full answer.", client_name="Voice client")
    spoken = service.app.audio_coordinator.calls[0][0]
    assert len(spoken.split()) == 60
    assert "client=Voice client" in caplog.text


def test_silent_feedback_captions_full_text_without_speaking(service):
    service.app.config["tts"]["feedback_level"] = "silent"
    _, payload = call(service, "Short spoken answer.", "Entire answer for the caption.")
    assert payload["result"]["structuredContent"]["spoken"] is False
    assert payload["result"]["structuredContent"]["full_text_kept"] is True
    assert service.app.audio_coordinator.calls[0][0] == "Entire answer for the caption."
    assert "on_done" not in service.app.audio_coordinator.calls[0][1]


@pytest.mark.parametrize("full_text", ["", "bad\x00text", "x" * (agent_server.MAX_FULL_TEXT + 1)])
def test_invalid_full_text_is_refused_before_speech(service, full_text):
    _, payload = call(service, "Short spoken answer.", full_text)
    assert payload["error"]["code"] == -32602
    assert not service.app.audio_coordinator.calls


def test_full_text_can_preserve_lists_and_code_for_followups(service):
    full_text = "Short spoken answer.\n\n- item\n\n```python\nprint('hi')\n```"
    _, payload = call(service, "Short spoken answer.", full_text)
    assert payload["result"]["structuredContent"]["full_text_kept"] is True
    assert service.app._cli_agent_voice_state["rest"] == "- item\n\n```python\nprint('hi')\n```"
