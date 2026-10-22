"""Confirm a desktop ChatGPT message, verify its send, then read the reply."""
from __future__ import annotations

import hashlib
import time
import uuid

from samsara import execution_policy
from samsara.agent_channel import chatgpt_reply
from samsara.command_registry import DispatchResult, DispatchState
from samsara.execution_policy import Invocation, Route
from samsara.plugin_commands import command
from plugins.commands.message_claude import ClaudeUiaAdapter, Draft, _norm, shape_body, body_hash

_drafts = {}
OBSERVE_TIMEOUT_S = 5.0


class ChatGPTUiaAdapter(ClaudeUiaAdapter):
    """Process-validated window discovery; no browser title matching."""

    def resolve_window(self):
        import psutil
        import win32gui
        import win32process

        windows = []

        def found(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if psutil.Process(pid).name().lower() == "chatgpt.exe":
                        windows.append(hwnd)
                except (psutil.Error, OSError):
                    pass
            return True

        win32gui.EnumWindows(found, None)
        if not windows:
            return None
        foreground = win32gui.GetForegroundWindow()
        if len(windows) > 1 and foreground not in windows:
            return None  # do not choose an arbitrary conversation
        hwnd = foreground if foreground in windows else windows[0]
        return hwnd, "ChatGPT", self.conversation_title(hwnd)

    def conversation_title(self, hwnd):
        # The recorded title is only ChatGPT. Guard the conversation using the
        # first user message in memory instead; never persist text or titles.
        reader = chatgpt_reply._Reader(hwnd)
        reader.snapshot()
        slots = chatgpt_reply._slots(reader.container) if reader.container is not None else []
        first = next((roots for assistant, roots in slots if not assistant), [])
        text, _, _, truncated = chatgpt_reply._content(first)
        if truncated:
            raise chatgpt_reply.NoNewNode()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def find_composer(self, hwnd):
        root = self._window_control(hwnd)
        editors = [c for c in chatgpt_reply._walk(root)
                   if c.ControlTypeName == "EditControl"]
        named = [c for c in editors if (c.Name or "").strip().lower() in
                 {"message chatgpt", "message chatgpt…", "message chatgpt...", "ask anything"}]
        return named[0] if len(named) == 1 else (editors[0] if len(editors) == 1 else None)

    def find_send(self, composer):
        # Buttons live in the composer's own group in the recorded tree.
        scope = composer
        for _ in range(2):
            matches = [c for c in chatgpt_reply._walk(scope)
                       if c.ControlTypeName == "ButtonControl" and
                       (c.Name or "").strip().lower() in {"send", "send prompt", "send message"}]
            if len(matches) == 1:
                return matches[0]
            scope = scope.GetParentControl()
            if scope is None:
                break
        return None

    def user_messages(self, hwnd):
        reader = chatgpt_reply._Reader(hwnd)
        reader.snapshot()
        slots = chatgpt_reply._slots(reader.container) if reader.container is not None else []
        latest = next(((i + 1, roots) for i, (assistant, roots) in reversed(list(enumerate(slots)))
                       if not assistant), None)
        return [(latest[0], chatgpt_reply._content(latest[1])[0])] if latest else []


_adapter = None


def _get_adapter():
    global _adapter
    if _adapter is None:
        _adapter = ChatGPTUiaAdapter()
    return _adapter


def _speak(app, text):
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(text, category="confirmation", interruptible=False)


def _result(dispatch_state, **detail):
    return DispatchResult(dispatch_state, "chatgpt message submit", "chatgpt message submit", detail)


@command("message chat gpt", aliases=["ask chat gpt", "message chatgpt", "ask chatgpt"],
         pack="ai", risk_class="write", reversible=True,
         side_effects=["ui"], side_effect_category="draft")
def handle_chatgpt_message_prepare(app, remainder):
    """Prepares a message for the running ChatGPT desktop app and asks to send it."""
    adapter = _get_adapter()
    window = adapter.resolve_window()
    if window is None:
        from plugins.commands.quick_ask import ask_default
        return ask_default(app, "chat gpt " + (remainder or ""))
    body = shape_body(remainder)
    if not body:
        _speak(app, "Ask ChatGPT what?")
        return _result(DispatchState.FAILED, reason="empty message")
    hwnd, title, conversation = window
    if adapter.find_composer(hwnd) is None:
        _speak(app, "I can't find the message box in ChatGPT.")
        return _result(DispatchState.FAILED, reason="composer not found")
    draft = Draft(uuid.uuid4().hex[:8], body, body_hash(body), hwnd, title, conversation)
    _drafts[draft.id] = draft
    question = f"Send to ChatGPT: {draft.preview()}?"
    inv = Invocation("chatgpt message submit", {"remainder": draft.id}, Route.EXACT,
                     execution_policy.current_generation(app), prompt=question,
                     source_text=remainder)

    def approve(_operation):
        decision = execution_policy.authorize(inv, app=app, confirmed=True)
        if isinstance(decision, execution_policy.Allowed):
            handle_chatgpt_message_submit(app, draft.id)
        else:
            _speak(app, "That request expired. Not sent.")

    execution_policy.stage_pending(app, inv, question, on_approve=approve,
                                   record_type="action", extra={"draft_id": draft.id})
    _speak(app, question + " Say yes to send, or ava cancel.")
    return _result(DispatchState.QUEUED, draft_id=draft.id, awaiting_confirmation=True)


@command("chatgpt message submit", pack="ai", risk_class="destructive", reversible=False,
         side_effects=["message_sent"], side_effect_category="external_write",
         param_schema={"remainder": {"type": "str", "required": True}})
def handle_chatgpt_message_submit(app, remainder):
    """Sends a confirmed ChatGPT draft once and starts reading its reply."""
    draft = _drafts.get((remainder or "").strip())
    if draft is None or draft.state != "prepared":
        return _result(DispatchState.REJECTED, reason="draft unavailable or already attempted")
    adapter = _get_adapter()

    def fail(reason):
        draft.state = "failed"
        _speak(app, "Not sent. " + reason)
        return _result(DispatchState.FAILED, reason=reason)

    window = adapter.resolve_window()
    if window is None or window[0] != draft.hwnd or window[2] != draft.conversation:
        return fail("The ChatGPT conversation changed.")
    if not adapter.focus(draft.hwnd):
        return fail("I couldn't focus ChatGPT.")
    composer = adapter.find_composer(draft.hwnd)
    if composer is None or _norm(adapter.read_composer(composer)):
        return fail("The message box is unavailable or already contains a draft.")
    if not adapter.paste(draft.body, draft.hwnd):
        return fail("The paste was refused.")
    time.sleep(0.15)
    if _norm(adapter.read_composer(composer)) != _norm(draft.body):
        return fail("The message box differs from the draft.")
    context = chatgpt_reply.before_send(app, draft.hwnd)
    if context is None and app.config.get("chatgpt_voice", {}).get("enabled", True):
        return fail("I couldn't observe the conversation.")
    baseline = context.after_message_index if context else chatgpt_reply.message_index(draft.hwnd)
    button = adapter.find_send(composer)
    if button is None or not adapter.is_foreground(draft.hwnd):
        return fail("The send control or window focus changed.")
    # From this point any exception/timeout is ambiguous. Never offer a retry.
    draft.state = "unknown"
    try:
        if not adapter.invoke(button):
            raise RuntimeError("send outcome unknown")
        chatgpt_reply.start_after_send(app, context)
        deadline = time.monotonic() + OBSERVE_TIMEOUT_S
        while time.monotonic() < deadline:
            if any(index > baseline and _norm(text).startswith(_norm(draft.body))
                   for index, text in adapter.user_messages(draft.hwnd)):
                draft.state = "sent"
                _speak(app, "Sent.")
                return _result(DispatchState.COMPLETED, state="sent", draft_id=draft.id)
            time.sleep(0.3)
    except Exception:
        pass
    _speak(app, "I couldn't confirm the send. Check ChatGPT; I will not resend.")
    return _result(DispatchState.FAILED, state="unknown", reason="submitted, not observed")


@command("stop chat gpt", aliases=["stop chatgpt"], pack="ai", risk_class="safe",
         reversible=True, side_effects=["audio"])
def handle_chatgpt_stop(app, remainder):
    """Stops reading the latest agent reply aloud."""
    return chatgpt_reply.followup(app, "stop chat gpt")
