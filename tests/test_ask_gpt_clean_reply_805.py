"""805: isolated CLI questions, narrow footer cleanup and agent History labels."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.commands import ask_agent
from samsara.agent_channel import cli_agents as cli
from samsara.ui import history_view as history


@pytest.mark.parametrize("session", [None, "saved-session"])
def test_codex_disables_project_instructions_on_new_and_resumed_turns(session):
    argv = cli.codex_argv(["fake-codex"], "question", session, Path.cwd(), False)
    assert "project_doc_max_bytes=0" in argv
    assert "--sandbox" in argv if session is None else "sandbox_mode=read-only" in argv


@pytest.mark.parametrize("session", [None, "saved-session"])
def test_claude_disables_personal_rules_without_disabling_subscription_auth(session):
    argv = cli.claude_argv(["fake-claude"], "question", session, Path.cwd(), False)
    assert "--safe-mode" in argv
    assert "--bare" not in argv  # bare mode does not read subscription credentials.
    assert "--restricted" in argv and "--permission-mode" in argv


@pytest.mark.parametrize("footer", ["PROMPT 805", "PROMPT: CLEAR INTRODUCTION",
    "FINAL LINE: PROMPT 805", "MODEL: GPT-6.1-SOL"])
def test_only_trailing_uppercase_metadata_is_removed(footer):
    body = "Keep this  spacing.\n\nPROMPT IN THE BODY\nMore text.  "
    assert cli.clean_reply(body + "\r\n\r\n" + footer + "\r\n") == body
    assert cli.AgentReply(body + "\n" + footer, None, (), 0).text == body


@pytest.mark.parametrize("body", ["PROMPT: say hello", "Model: GPT", "MODEL: gpt-6.1-sol",
    "A PROMPT 805", "Use PROMPT 805 in your terminal.", "FINAL LINE without colon",
    "```text\nPROMPT 805\n```", "```text\nPROMPT 805", "PROMPT 805\nKeep this.",
    "  Keep this spacing.  \n\n"])
def test_body_and_code_are_untouched(body):
    assert cli.clean_reply(body) == body


def test_stacked_footers_are_removed():
    assert cli.clean_reply("Answer.\nMODEL: GPT\nFINAL LINE: PROMPT 805\nPROMPT 805") == "Answer."


@pytest.mark.parametrize("name", ["gpt", "claude"])
def test_fake_process_receives_isolation_and_saves_only_clean_answer(tmp_path, monkeypatch, name):
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "auth.json").write_text("{}")
    (personal / "AGENTS.md").write_text("Personal instructions must stay outside the child home.")
    (personal / "config.toml").write_text("# personal config")
    monkeypatch.setenv("CODEX_HOME", str(personal))
    monkeypatch.setattr(cli, "_closing", __import__("threading").Event())
    monkeypatch.setattr(cli.thread_registry, "spawn", lambda name, fn, args=(): fn(*args))
    calls = []

    class Input(io.StringIO):
        def close(self):
            pass

    class Process:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))
            self.stdin = Input()
            text = "Keep the answer.\nPROMPT: CLEAR INTRODUCTION"
            event = ({"type": "item.completed", "item": {"type": "agent_message", "text": text}}
                     if name == "gpt" else {"type": "result", "result": text})
            event["session_id"] = "clean-session"
            prefix = json.dumps({"type": "thread.started", "thread_id": "clean-session"}) + "\n"
            self.stdout = io.StringIO(prefix + json.dumps(event) + "\n")
            self.stderr = io.StringIO()

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(cli.subprocess, "Popen", Process)
    home = tmp_path / "samsara"
    home.mkdir()
    (home / "agent_sessions.json").write_text(json.dumps({name: "legacy-session"}))
    channel = cli.CliAgent(name, cli.codex_argv if name == "gpt" else cli.claude_argv,
                          cli.parse_codex if name == "gpt" else cli.parse_claude, ["fake"])
    result = channel.ask("Synthetic question", {"cwd": str(tmp_path)}, home=home)
    assert result.text == "Keep the answer."
    assert "legacy-session" not in calls[0][0]
    channel.ask("Synthetic follow-up", {"cwd": str(tmp_path)}, home=home)
    assert "clean-session" in calls[1][0]
    assert "PROMPT" not in (home / "agent_transcripts" / f"{name}.md").read_text()
    if name == "gpt":
        child_home = Path(calls[0][1]["env"]["CODEX_HOME"])
        assert child_home != personal and child_home.is_relative_to(home)
        assert (child_home / "auth.json").read_text() == "{}"
        assert not (child_home / "AGENTS.md").exists()
        assert not (child_home / "config.toml").exists()
        assert cli.os.environ["CODEX_HOME"] == str(personal)
    else:
        assert "--safe-mode" in calls[0][0]


def test_refreshed_codex_auth_is_retained_and_new_login_is_reused(tmp_path, monkeypatch):
    personal = tmp_path / "personal"
    personal.mkdir()
    source = personal / "auth.json"
    source.write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(personal))
    home = tmp_path / "samsara"
    target = Path(cli._codex_env(home)["CODEX_HOME"]) / "auth.json"
    target.write_text('{"fixture": "refreshed"}')
    cli.os.utime(source, (1, 1))
    cli._codex_env(home)
    assert json.loads(target.read_text()) == {"fixture": "refreshed"}
    source.write_text('{"fixture": "new-login"}')
    cli.os.utime(source, (target.stat().st_mtime + 10, target.stat().st_mtime + 10))
    cli._codex_env(home)
    assert json.loads(target.read_text()) == {"fixture": "new-login"}


@pytest.mark.parametrize("phrase,label", [("ask gpt", "GPT"), ("ask codex", "GPT"),
    ("ask claude", "Claude"), ("ask hermes", "Hermes"), ("tell hermes", "Hermes")])
def test_history_names_agent_instead_of_foreground_window(phrase, label):
    row = {"entry_type": "command", "status": "success", "matched_command": phrase,
           "app_context": "Claude", "display_text": phrase + " synthetic question"}
    assert history.row_context(row) == label
    assert label in history.meta_text(row)


def test_other_history_context_is_unchanged():
    for row in ({"entry_type": "dictation", "matched_command": "ask gpt"},
                {"entry_type": "command", "matched_command": "focus claude"}):
        row["app_context"] = "Editor"
        assert history.row_context(row) == "Editor"


def test_ask_claude_uses_cli_even_when_desktop_is_present(monkeypatch):
    from plugins.commands import message_claude
    monkeypatch.setattr(message_claude.ClaudeUiaAdapter, "resolve_window",
                        lambda self: pytest.fail("ask Claude must not inspect desktop windows"))
    monkeypatch.setattr(ask_agent, "ask_cli", lambda app, name, question: (name, question))
    assert ask_agent.ask_claude(SimpleNamespace(), "Synthetic question") == ("claude", "Synthetic question")
