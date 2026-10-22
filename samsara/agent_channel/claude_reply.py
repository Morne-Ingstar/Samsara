"""Read completed Claude desktop replies through UI Automation and speak them."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)
_POLL_S = 0.3
_STABLE_S = 1.5
_FOLLOWUP_S = 60.0
_MESSAGE_NAME = re.compile(r"Message (\d+) of \d+")
_PATH = re.compile(r"(?<!\w)(?:[A-Za-z]:\\|/|\.\.?[/\\])[^\s,;:!?<>|]+")
_CODE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_TABLE = re.compile(r"(?m)^(?:\s*\|[^\n]+\|\s*\n?){2,}")


class ReplyFailure(Exception):
    """A reply could not be observed."""


class WindowGone(ReplyFailure):
    pass


class ReplyTimeout(ReplyFailure):
    pass


class NoNewNode(ReplyFailure):
    pass


@dataclass(frozen=True)
class _Message:
    index: int
    assistant: bool
    text: str


@dataclass(frozen=True)
class _Snapshot:
    messages: tuple[_Message, ...]
    streaming: bool
    chat_found: bool = True


@dataclass(frozen=True)
class SendContext:
    hwnd: int
    after_message_index: int
    conversation: str
    send_word: str | None = None


def _control(window):
    if isinstance(window, int):
        import win32gui
        import uiautomation as uia
        if not win32gui.IsWindow(window):
            raise WindowGone()
        return uia.ControlFromHandle(window)
    if hasattr(window, "Exists") and not window.Exists(0, 0):
        raise WindowGone()
    return window


def _walk(root, limit=5000):
    stack = [root]
    seen = 0
    while stack and seen < limit:
        control = stack.pop()
        seen += 1
        yield control
        stack.extend(reversed(control.GetChildren()))


def _prose(message):
    rows = [control for control in _walk(message, 600)
            if control.ControlTypeName == "GroupControl"
            and "group/message-row" in (control.ClassName or "")]
    if not rows:
        return ""
    parts = []
    for child in rows[0].GetChildren():
        if child.ControlTypeName != "GroupControl" or child.ClassName:
            continue
        stack = [child]
        while stack:
            control = stack.pop()
            kind = control.ControlTypeName
            class_name = control.ClassName or ""
            if kind in {"DocumentControl", "ButtonControl", "ToolBarControl", "StatusBarControl"}:
                continue
            if "thinking" in class_name or "tool-shell" in class_name:
                continue
            if "code" in class_name or kind == "CodeControl":
                parts.append("there's a code block on screen")
                continue
            if kind == "TableControl" or "table" in class_name:
                parts.append("there's a table on screen")
                continue
            if kind == "TextControl" and "sr-only" not in class_name:
                value = (control.Name or "").strip()
                if value:
                    parts.append(value)
                continue
            stack.extend(reversed(control.GetChildren()))
    return "\n".join(parts)


def _snapshot(window) -> _Snapshot:
    try:
        root = _control(window)
        chat = next((control for control in _walk(root)
                     if control.ControlTypeName == "GroupControl"
                     and control.Name == "Chat messages"), None)
        if chat is None:
            return _Snapshot((), False, False)
        messages = []
        for control in _walk(chat):
            if control.ControlTypeName != "GroupControl":
                continue
            match = _MESSAGE_NAME.fullmatch(control.Name or "")
            if match:
                assistant = any("group/message-row" in (child.ClassName or "")
                                for child in control.GetChildren())
                messages.append(_Message(int(match.group(1)), assistant,
                                         _prose(control) if assistant else ""))
        streaming = any(control.ControlTypeName == "ButtonControl"
                        and control.Name == "Stop response" for control in _walk(root))
        return _Snapshot(tuple(messages), streaming)
    except ReplyFailure:
        raise
    except Exception as exc:
        raise WindowGone() from exc


def message_index(window) -> int:
    snapshot = _snapshot(window)
    if not snapshot.chat_found:
        raise NoNewNode()
    return max((message.index for message in snapshot.messages), default=0)


def watch_for_reply(window, after_message_index: int, timeout_s: float) -> str:
    """Return new assistant prose after Stop response disappears and text settles."""
    deadline = time.monotonic() + timeout_s
    previous = ""
    stable_since = None
    saw_new = False
    while time.monotonic() < deadline:
        snapshot = _snapshot(window)
        fresh = [message for message in snapshot.messages if message.index > after_message_index]
        saw_new |= bool(fresh)
        assistant = next((message for message in reversed(fresh) if message.assistant), None)
        text = assistant.text if assistant else ""
        if text and not snapshot.streaming:
            if text != previous:
                stable_since = time.monotonic()
            elif stable_since is not None and time.monotonic() - stable_since >= _STABLE_S:
                logger.info("[CLAUDE-VOICE] reply complete index=%s chars=%s", assistant.index, len(text))
                return text
        else:
            stable_since = None
        previous = text
        time.sleep(_POLL_S)
    raise ReplyTimeout() if saw_new else NoNewNode()


def _word_cap(config, *, source="claude") -> int:
    try:
        return max(1, min(500, int(config.get(f"{source}_voice", {}).get("spoken_word_cap", 60))))
    except (TypeError, ValueError, AttributeError):
        return 60


def _reply_timeout(config) -> float:
    try:
        value = float(config.get("claude_voice", {}).get("reply_timeout_s", 180))
        return value if math.isfinite(value) and value > 0 else 180.0
    except (TypeError, ValueError, AttributeError):
        return 180.0


def spoken_form(reply: str, word_cap: int = 60) -> tuple[str, str]:
    """Replace unreadable material and split the first spoken chunk from the rest."""
    safe = _CODE.sub(" There's a code block on screen. ", reply)
    safe = _TABLE.sub(" there's a table on screen ", safe)
    safe = _PATH.sub(" there's a file path on screen ", safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    words = safe.split()
    return " ".join(words[:word_cap]), " ".join(words[word_cap:])


def _state(app):
    from .agent_reply import state
    return state(app)


def _speak(app, text: str, token, on_done=None):
    state = _state(app)
    if state["token"] is not token:
        return
    state["last"] = text
    state["speaking"] = True
    state["expires"] = time.monotonic() + _FOLLOWUP_S

    def done():
        if state["token"] is token:
            state["speaking"] = False
            state["expires"] = time.monotonic() + _FOLLOWUP_S
            if on_done:
                on_done()

    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        state["handle"] = coordinator.speak(text, category="conversation",
                                             interruptible=True, on_done=done)
        logger.info("[CLAUDE-VOICE] speak chars=%s", len(text))
        if str(getattr(state["handle"], "utterance_id", "")).startswith("noop-"):
            done()
    else:
        done()


def _active(app):
    state = _state(app)
    coordinator = getattr(app, "audio_coordinator", None)
    return (state["token"] is not None and state["handle"] is not None
            and time.monotonic() < state["expires"]
            and coordinator is not None
            and getattr(coordinator, "_last_spoken_handle", state["handle"]) == state["handle"])


def followup(app, action: str) -> bool:
    action = action.strip().lower().rstrip(".!?")
    if not _active(app):
        from samsara.agent_channel import cli_agents
        if cli_agents.followup(app, action):
            return True
        # Queue reports share Claude's spoken "read the rest" / "repeat that"
        # commands, but retain their own chunk state and captioned speech.
        view = getattr(app, "_queue_voice_view", None)
        response = view.report_followup(action) if view is not None else None
        if response is not None:
            coordinator = getattr(app, "audio_coordinator", None)
            if coordinator is not None:
                if not response:
                    coordinator.cancel_speech()
                else:
                    coordinator.speak(response, category="ava_response", interruptible=True)
            return True
        return False
    state = _state(app)
    return _drill_followup(app, action, state)


def is_followup_phrase(action: str) -> bool:
    action = action.strip().lower().rstrip(".!?")
    return (action in {"read the rest", "repeat that", "stop", "stop claude", "stop claude reply",
                       "stop the reply", "quiet claude", "stop chat gpt", "stop chatgpt", "read the whole thing", "read the code",
                       "read the first code block", "read code block two", "next section",
                       "previous section", "how long is it", "what's in it"}
            or action.startswith("read section ") or action.startswith("read code block ")
            or action.startswith("read the "))


def _drill_followup(app, action: str, state, *, cli=False) -> bool:
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is None:
        return False
    stop_phrases = {"stop", "stop claude", "stop claude reply", "stop the reply", "quiet claude",
                    "stop chat gpt", "stop chatgpt"}
    if action in stop_phrases:
        app.audio_coordinator.cancel_speech()
        state["speaking"] = False
        state["whole_generation"] = state.get("whole_generation", 0) + 1
        if state.get("resume"):
            state["resume"]()
        state["expires"] = time.monotonic() + _FOLLOWUP_S
        return True
    if action == "repeat that":
        text = state.get("last", "")
    elif action == "read the rest":
        cap = 60 if cli else _word_cap(getattr(app, "config", {}) or {},
                                         source=state.get("source", "claude"))
        text, remainder = spoken_form(state.get("rest", ""), cap)
        state["rest"] = remainder
    elif action in {"read the whole thing", "how long is it", "what's in it",
                    "read the code", "read the first code block", "read code block two"}:
        text = _select_drill_text(app, state, action)
    elif (action in {"next section", "previous section"} or action.startswith("read section ")
          or action.startswith("read code block ") or action.startswith("read the ")):
        text = _select_drill_text(app, state, action)
    else:
        return False
    if not text:
        text = ("That's all of Claude's reply." if not cli and state.get("source", "claude") == "claude"
                else "That's all of the reply.")
    state["expires"] = time.monotonic() + _FOLLOWUP_S
    if action == "read the whole thing":
        _speak_chunks(app, text, state, cli=cli)
    else:
        _deliver(app, text, state, cli=cli)
    return True


def _select_drill_text(app, state, action):
    full = state.get("full", state.get("rest", ""))
    sections = state.get("sections", [])
    if not sections:
        from .spoken_digest import sections as map_sections
        sections = map_sections(full)
    grouped = []
    for section in sections:
        if getattr(section, "kind", "") == "heading":
            grouped.append({"title": section.title, "text": "", "index": len(grouped)})
        elif grouped:
            grouped[-1]["text"] += ("\n" if grouped[-1]["text"] else "") + section.text
    if not grouped:
        grouped = [{"title": "", "text": section.text, "index": i}
                   for i, section in enumerate(sections)]
    if action in {"how long is it", "what's in it"}:
        words = len(re.findall(r"\b\w+\b", full))
        headings = [s.title for s in sections if getattr(s, "kind", "") == "heading" and s.title]
        title = ", ".join(headings) if headings else "no named sections"
        code_count = len(re.findall(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", full))
        return f"About {words} words, {len(headings)} sections: {title}; {code_count} code blocks."
    if action == "read the whole thing":
        return spoken_form(full, max(1, len(full.split())))[0]
    if action in {"next section", "previous section"}:
        if not grouped:
            return spoken_form(full, 60)[0]
        step = 1 if action == "next section" else -1
        cursor = state.get("section_cursor", -1)
        index = max(0, min(len(grouped) - 1, cursor + step if cursor >= 0 else (0 if step > 0 else len(grouped) - 1)))
        state["section_cursor"] = index
        section = grouped[index]
    elif action.startswith("read section "):
        try:
            index = int(action.removeprefix("read section ")) - 1
            section = grouped[index]
            state["section_cursor"] = index
        except (ValueError, IndexError):
            return "I couldn't find that section."
    elif action in {"read the code", "read the first code block"} or action.startswith("read code block "):
        number = 1 if action != "read code block two" else 2
        found = re.findall(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n(.*?)^\s*\1\s*$", full)
        blocks = [body for _, body in found]
        if action.startswith("read code block "):
            name = action.removeprefix("read code block ").strip()
            number = {"one": 1, "two": 2, "three": 3}.get(name, int(name) if name.isdigit() else 0)
        if not 1 <= number <= len(blocks):
            return "There isn't a code block by that number."
        block = blocks[number - 1].rstrip()
        from samsara.clipboard import copy_text
        if not copy_text(block):
            return "I couldn't copy that code block."
        lines = block.splitlines()
        return f"Code block {number}, {len(lines)} lines, starts with: {lines[0] if lines else 'empty'}. It's on your clipboard."
    elif action.startswith("read the "):
        query = action.removeprefix("read the ").strip()
        section = next((s for s in grouped if s["title"] and query in s["title"].lower()), None)
        if section is None:
            return "I couldn't find that section."
        state["section_cursor"] = section["index"]
    else:
        return "That's all of the reply."
    body = section["text"] if isinstance(section, dict) else getattr(section, "text", "")
    body = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", body)
    return spoken_form(body, 60)[0]


def _deliver(app, text, state, *, cli=False):
    if cli:
        state["last"] = text
        app.audio_coordinator.speak(text, category="ava_response", interruptible=True)
    else:
        _speak(app, text, state["token"], on_done=state.get("resume"))


def _speak_chunks(app, text, state, *, cli=False):
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    chunks = [" ".join(text.split()[i:i + 60]) for i in range(0, len(text.split()), 60)] or [""]
    silent = str((getattr(app, "config", {}) or {}).get("tts", {}).get("feedback_level", "")).lower() == "silent"
    generation = state.get("whole_generation", 0) + 1
    state["whole_generation"] = generation
    if silent:
        for chunk in chunks:
            if state.get("whole_generation") != generation:
                break
            if cli:
                state["last"] = chunk
                app.audio_coordinator.speak(chunk, category="ava_response", interruptible=True)
            else:
                _speak(app, chunk, state["token"])
        return
    def next_chunk(index=0):
        if index >= len(chunks) or state.get("whole_generation") != generation:
            return
        if cli:
            state["last"] = chunks[index]
            app.audio_coordinator.speak(chunks[index], category="ava_response", interruptible=True,
                                        on_done=lambda: next_chunk(index + 1))
        else:
            _speak(app, chunks[index], state["token"],
                   on_done=lambda: next_chunk(index + 1) if index + 1 < len(chunks) else state.get("resume", lambda: None)())
    next_chunk()


def before_send(app, hwnd: int, *, send_word=None) -> SendContext | None:
    try:
        if not app.config.get("claude_voice", {}).get("enabled", True):
            return None
        import psutil
        import win32process
        _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
        if psutil.Process(process_id).name().lower() != "claude.exe":
            return None
        from plugins.commands.message_claude import ClaudeUiaAdapter
        adapter = ClaudeUiaAdapter()
        return SendContext(hwnd, message_index(hwnd), adapter.conversation_title(hwnd), send_word)
    except Exception:
        logger.exception("[CLAUDE-VOICE] could not capture send context")
        return None


def start_after_send(app, context: SendContext | None) -> None:
    from .agent_reply import start
    start(app, context, source="claude", watch=watch_for_reply,
          timeout_s=_reply_timeout(app.config), failures=(WindowGone, ReplyTimeout, NoNewNode))
