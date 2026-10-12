"""Voice questions through the owner's signed-in Codex and Claude CLIs."""
from __future__ import annotations

import time

from samsara.agent_channel import cli_agents
from samsara.agent_channel.claude_reply import spoken_form
from samsara.command_registry import DispatchState
from samsara.plugin_commands import command
from samsara.runtime import thread_registry


def _say(app, text: str) -> None:
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(text, category="ava_response", interruptible=True)
    show = getattr(app, "_show_outcome_chip", None)
    if show is not None:
        show("agent: " + text[:45], "info")


def _thinking(app, active: bool) -> None:
    def update():
        indicator = getattr(app, "listening_indicator", None)
        if indicator is not None:
            indicator.set_thinking(active)
    schedule = getattr(app, "_schedule_ui", None)
    schedule(update) if schedule else update()
    tools = getattr(app, "_smart_actions_tools", None)
    if tools is not None:
        (tools._start_thinking_pulse if active else tools._stop_thinking_pulse)()


def _failure(name: str, exc: Exception) -> str:
    label = "GPT" if name == "gpt" else "Claude"
    if isinstance(exc, TimeoutError):
        return f"{label} didn't answer in time"
    if isinstance(exc, FileNotFoundError):
        return f"{label}'s CLI isn't installed"
    raw = str(exc).lower()
    if any(word in raw for word in ("login", "sign in", "authentication", "unauthorized")):
        return "Codex isn't signed in" if name == "gpt" else "Claude isn't signed in"
    return f"{label} couldn't answer. {str(exc).splitlines()[-1][:140]}"


def ask_cli(app, name: str, question: str):
    if not question.strip():
        _say(app, f"Ask {name} what?")
        return DispatchState.FAILED
    _thinking(app, True)

    def run():
        try:
            config = (getattr(app, "config", {}) or {}).get("agent_channel", {}) or {}
            result = cli_agents.agent(name).ask(question, config)
            first, rest = spoken_form(result.text, 60)
            app._cli_agent_voice_state = {"last": first, "rest": rest,
                                          "expires": time.monotonic() + 60}
            app._cli_agent_last = result
            _say(app, first or f"{name} answered on screen")
        except Exception as exc:
            _say(app, _failure(name, exc))
        finally:
            _thinking(app, False)

    thread_registry.spawn(f"agent.{name}", run)
    return DispatchState.QUEUED


@command("ask gpt", aliases=["ask codex"], pack="core", risk_class="safe",
         reversible=True, side_effects=["network", "audio", "ui", "file"])
def ask_gpt(app, remainder=""):
    """Asks GPT a question through your signed-in account and reads the reply aloud."""  # PLACEHOLDER COPY (531)
    return ask_cli(app, "gpt", remainder)


def ask_claude(app, remainder=""):
    from plugins.commands.message_claude import ClaudeUiaAdapter, handle_claude_message_prepare
    if ClaudeUiaAdapter().resolve_window() is not None:
        return handle_claude_message_prepare(app, remainder)
    return ask_cli(app, "claude", remainder)


@command("new conversation with", pack="core", risk_class="safe", reversible=True,
         side_effects=["file", "audio", "ui"])
def new_conversation(app, remainder=""):
    """Starts a fresh conversation with GPT or Claude, without the previous chat's context."""  # PLACEHOLDER COPY (531)
    name = remainder.strip().lower().rstrip(".?!")
    if name == "hermes":
        from samsara.agent_channel.hermes_voice import new
        return new(app)
    if name not in {"gpt", "codex", "claude"}:
        _say(app, "Say new conversation with GPT or Claude.")
        return DispatchState.FAILED
    cli_agents.agent(name).new_conversation()
    _say(app, f"New {('GPT' if name == 'codex' else name.upper())} conversation ready.")
    return True


@command("ask hermes", aliases=["tell hermes"], pack="hermes", risk_class="safe",
         reversible=True, side_effects=["network", "audio", "ui", "file"])
def ask_hermes(app, remainder=""):
    """Sends your question to your own Hermes while you keep dictating."""  # PLACEHOLDER COPY (539)
    from samsara.agent_channel.hermes_voice import ask
    return ask(app, remainder)


@command("new hermes session", aliases=["new conversation with hermes"], pack="hermes",
         risk_class="safe", reversible=True, side_effects=["file", "audio", "ui"])
def new_hermes_session(app, remainder=""):
    """Starts a fresh Hermes conversation; optionally say with high effort."""  # PLACEHOLDER COPY (539)
    from samsara.agent_channel.hermes_voice import new
    return new(app, remainder)


@command("stop hermes", pack="hermes", risk_class="safe", reversible=True,
         side_effects=["audio", "ui"])
def stop_hermes(app, remainder=""):
    """Stops Hermes's current turn and keeps your conversation for later."""  # PLACEHOLDER COPY (539)
    from samsara.agent_channel.hermes_voice import stop
    return stop(app)


def start_services(app):
    # Keep the legacy desktop command, but transfer its spoken ask alias before
    # CommandExecutor refreshes its frozen matcher.
    from samsara import plugin_commands
    old = plugin_commands._MODULE_ENTRIES["plugins.commands.message_claude"]["claude message prepare"]
    old["aliases"] = [alias for alias in old["aliases"] if alias != "ask claude"]
    command("ask claude", pack="core", risk_class="safe", reversible=True,
            side_effects=["network", "audio", "ui", "file"])(ask_claude)
