"""Prompt 310: subscription CLI invocation, lifecycle, and voice routing."""
from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from samsara.agent_channel import cli_agents
from plugins.commands import ask_agent


FAKE = textwrap.dedent(r'''
    import json, os, subprocess, sys, time
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_AGENT_MODE", "ok")
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        open(os.environ["FAKE_PID_FILE"], "w").write(str(child.pid))
        time.sleep(30)
    if mode == "fail":
        print("authentication required", file=sys.stderr, flush=True)
        sys.exit(2)
    if "exec" in args:
        prompt = sys.stdin.read()
        print(json.dumps({"type":"thread.started", "thread_id":"11111111-1111-1111-1111-111111111111"}), flush=True)
        print(json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"Four. Extra detail."}}), flush=True)
    else:
        prompt = args[-1]
        print(json.dumps({"type":"system", "session_id":"22222222-2222-2222-2222-222222222222"}), flush=True)
        print(json.dumps({"type":"result", "session_id":"22222222-2222-2222-2222-222222222222", "result":"Four. Extra detail.", "is_error":False}), flush=True)
    if os.environ.get("FAKE_PROMPT_FILE"):
        open(os.environ["FAKE_PROMPT_FILE"], "w", encoding="utf-8").write(prompt)
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE, encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(home))
    return script, home


def _agent(name, script):
    return cli_agents.CliAgent(name,
        cli_agents.codex_argv if name == "gpt" else cli_agents.claude_argv,
        cli_agents.parse_codex if name == "gpt" else cli_agents.parse_claude,
        [sys.executable, str(script)])


@pytest.mark.parametrize("name", ["gpt", "claude"])
def test_success_resume_and_read_only(fake, monkeypatch, name):
    script, home = fake
    prompt_file = home.parent / "prompt.txt"
    monkeypatch.setenv("FAKE_PROMPT_FILE", str(prompt_file))
    agent = _agent(name, script)
    first = agent.ask("what is two plus two", {"cwd": str(home.parent)}, home=home)
    assert first.text == "Four. Extra detail."
    assert cli_agents.DEFAULT_PREAMBLE in prompt_file.read_text(encoding="utf-8")
    saved = json.loads((home / "agent_sessions.json").read_text(encoding="utf-8"))
    assert saved[name] == first.session_id
    assert "Four. Extra detail." in (home / "agent_transcripts" / f"{name}.md").read_text()
    if name == "gpt":
        assert "read-only" in first.argv and "--sandbox" in first.argv
    else:
        assert "--restricted" in first.argv and "Read,Glob,Grep,WebSearch" in first.argv
        assert "--permission-mode" in first.argv
    second = agent.ask("continue", {"cwd": str(home.parent)}, home=home)
    assert first.session_id in second.argv
    if name == "gpt":
        assert "sandbox_mode=read-only" in second.argv
    agent.new_conversation(home)
    assert name not in json.loads((home / "agent_sessions.json").read_text())


def test_timeout_kills_child_tree(fake, monkeypatch):
    script, home = fake
    pid_file = home.parent / "child.pid"
    monkeypatch.setenv("FAKE_AGENT_MODE", "hang")
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    with pytest.raises(TimeoutError):
        _agent("gpt", script).ask("wait", {"cwd": str(home.parent), "timeout_s": 0.5}, home=home)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    for _ in range(30):
        if not psutil.pid_exists(pid):
            break
        time.sleep(0.05)
    assert not psutil.pid_exists(pid)


def test_nonzero_is_spoken_failure(fake, monkeypatch):
    script, home = fake
    monkeypatch.setenv("FAKE_AGENT_MODE", "fail")
    with pytest.raises(RuntimeError, match="authentication required") as exc:
        _agent("gpt", script).ask("hello", {"cwd": str(home.parent)}, home=home)
    assert ask_agent._failure("gpt", exc.value) == "Codex isn't signed in"


def test_spoken_shape_and_followups(fake, monkeypatch):
    script, home = fake
    spoken = []
    class Audio:
        def speak(self, text, **kwargs):
            spoken.append((text, kwargs))
        def cancel_speech(self):
            spoken.append(("cancel", {}))
    app = SimpleNamespace(config={"agent_channel": {"cwd": str(home.parent)}},
                          audio_coordinator=Audio(), listening_indicator=None)
    monkeypatch.setattr(cli_agents, "agent", lambda name: _agent(name, script))
    original_spawn = ask_agent.thread_registry.spawn
    monkeypatch.setattr(ask_agent.thread_registry, "spawn",
                        lambda name, fn, **kw: fn() if name.startswith("agent.")
                        else original_spawn(name, fn, **kw))
    ask_agent.ask_cli(app, "gpt", "what is two plus two")
    assert spoken[0][0] == "Four. Extra detail."
    assert spoken[0][1]["category"] == "ava_response"
    assert cli_agents.followup(app, "repeat that")
    assert spoken[-1][0] == "Four. Extra detail."


def test_claude_no_window_uses_cli(fake, monkeypatch):
    from plugins.commands import message_claude
    script, home = fake
    app = SimpleNamespace(config={"agent_channel": {"cwd": str(home.parent)}},
                          audio_coordinator=SimpleNamespace(speak=lambda *a, **k: None),
                          listening_indicator=None)
    monkeypatch.setattr(message_claude.ClaudeUiaAdapter, "resolve_window", lambda self: None)
    monkeypatch.setattr(cli_agents, "agent", lambda name: _agent(name, script))
    original_spawn = ask_agent.thread_registry.spawn
    monkeypatch.setattr(ask_agent.thread_registry, "spawn",
                        lambda name, fn, **kw: fn() if name.startswith("agent.")
                        else original_spawn(name, fn, **kw))
    ask_agent.ask_claude(app, "what is two plus two")
    assert app._cli_agent_last.text == "Four. Extra detail."


def test_spoken_claude_alias_handoff_preserves_desktop_command():
    from samsara import plugin_commands
    from plugins.commands import message_claude
    old = plugin_commands._REGISTRY.get("ask claude")
    desktop = plugin_commands._MODULE_ENTRIES[message_claude.__name__]["claude message prepare"]
    agent_entries = plugin_commands._MODULE_ENTRIES[ask_agent.__name__]
    old_agent_entry = agent_entries.get("ask claude")
    aliases = list(desktop["aliases"])
    try:
        ask_agent.start_services(None)
        assert plugin_commands._REGISTRY["ask claude"]["func"] is ask_agent.ask_claude
        assert "ask claude" not in desktop["aliases"]
        from samsara.command_registry import CommandMatcher
        matcher = CommandMatcher()
        matcher.load_plugins({"ask claude": plugin_commands._REGISTRY["ask claude"],
                              "claude message prepare": desktop})
        matcher.freeze()
        entry, remainder = matcher.match("ask claude hello")
        assert entry.handler is ask_agent.ask_claude and remainder == "hello"
    finally:
        desktop["aliases"] = aliases
        if old is not None:
            plugin_commands._REGISTRY["ask claude"] = old
        else:
            plugin_commands._REGISTRY.pop("ask claude", None)
        if old_agent_entry is not None:
            agent_entries["ask claude"] = old_agent_entry
        else:
            agent_entries.pop("ask claude", None)
