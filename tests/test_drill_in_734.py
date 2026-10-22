"""Drill-in follow-ups for replies already spoken by agent channels."""
from types import SimpleNamespace
from unittest.mock import Mock

from samsara.agent_channel import claude_reply
from samsara.agent_channel.spoken_digest import sections


REPLY = """# Setup
Set up the project first.

# Config
Change the local setting next.

```python
first = 1
print(first)
```

# Tests
Run the small test selection.

```js
const ready = true;
```
"""


class Coordinator:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.captions = []
        self.cancelled = False

    def speak(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.config.get("tts", {}).get("feedback_level") == "silent":
            self.captions.append(text)
        return SimpleNamespace(utterance_id=f"handle-{len(self.calls)}")

    def cancel_speech(self):
        self.cancelled = True


def make_app(monkeypatch, *, silent=False):
    config = {"tts": {"feedback_level": "silent" if silent else "full"}}
    app = SimpleNamespace(config=config)
    app.audio_coordinator = Coordinator(config)
    state = claude_reply._state(app)
    state.update(token=object(), full=REPLY, rest=REPLY, last="Initial digest",
                 sections=sections(REPLY), expires=10**12)
    claude_reply._speak(app, "Initial digest", state["token"])
    state["expires"] = 10**12
    copied = []
    monkeypatch.setattr("samsara.clipboard.copy_text", lambda text: copied.append(text) or True)
    return app, state, copied


def test_code_commands_copy_block_and_speak_metadata(monkeypatch):
    app, state, copied = make_app(monkeypatch)
    assert claude_reply.followup(app, "read the code")
    assert copied[-1] == "first = 1\nprint(first)"
    assert app.audio_coordinator.calls[-1][0] == "Code block 1, 2 lines, starts with: first = 1. It's on your clipboard."
    assert claude_reply.followup(app, "read code block two")
    assert copied[-1] == "const ready = true;"
    assert "Code block 2, 1 lines" in app.audio_coordinator.calls[-1][0]


def test_sections_and_length_commands(monkeypatch):
    app, state, _ = make_app(monkeypatch)
    assert claude_reply.followup(app, "next section")
    assert app.audio_coordinator.calls[-1][0] == "Set up the project first."
    assert claude_reply.followup(app, "read section 3")
    assert app.audio_coordinator.calls[-1][0] == "Run the small test selection. There's a code block on screen."
    assert claude_reply.followup(app, "read the config")
    assert app.audio_coordinator.calls[-1][0] == "Change the local setting next. There's a code block on screen."
    assert claude_reply.followup(app, "previous section")
    assert app.audio_coordinator.calls[-1][0] == "Set up the project first."
    assert claude_reply.followup(app, "how long is it")
    assert "words" in app.audio_coordinator.calls[-1][0]
    assert "3 sections: Setup, Config, Tests" in app.audio_coordinator.calls[-1][0]
    assert "2 code blocks" in app.audio_coordinator.calls[-1][0]


def test_whole_reply_uses_safe_words_and_repeats_and_stop(monkeypatch):
    app, state, _ = make_app(monkeypatch)
    app.audio_coordinator.calls.clear()
    monkeypatch.setattr(claude_reply, "_FOLLOWUP_S", 60)
    assert claude_reply.followup(app, "read the whole thing")
    assert app.audio_coordinator.calls[0][0].startswith("Setup Set up the project first.")
    assert "first = 1" not in " ".join(call[0] for call in app.audio_coordinator.calls)
    assert "There's a code block on screen." in " ".join(call[0] for call in app.audio_coordinator.calls)
    assert claude_reply.followup(app, "repeat that")
    assert claude_reply.followup(app, "stop")
    assert app.audio_coordinator.cancelled


def test_silent_followups_use_existing_caption_route_and_extend_expiry(monkeypatch):
    app, state, _ = make_app(monkeypatch, silent=True)
    monkeypatch.setattr(claude_reply.time, "monotonic", lambda: 120.0)
    assert claude_reply.followup(app, "what's in it")
    assert app.audio_coordinator.captions[-1] == app.audio_coordinator.calls[-1][0]
    assert state["expires"] == 180.0


def test_idle_expiry_is_preserved(monkeypatch):
    app, state, _ = make_app(monkeypatch)
    state["expires"] = 9.0
    monkeypatch.setattr(claude_reply.time, "monotonic", lambda: 10.0)
    assert not claude_reply.followup(app, "next section")


def test_cli_followup_uses_same_drill_helper(monkeypatch):
    from samsara.agent_channel import cli_agents

    app, state, copied = make_app(monkeypatch)
    app._claude_voice_state = None
    app._cli_agent_voice_state = {"last": "Initial digest", "rest": REPLY, "full": REPLY,
                                  "expires": 1000.0, "sections": sections(REPLY)}
    monkeypatch.setattr(cli_agents.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr("samsara.clipboard.copy_text", lambda text: copied.append(text) or True)
    assert cli_agents.followup(app, "read the code")
    assert copied[-1] == "first = 1\nprint(first)"

