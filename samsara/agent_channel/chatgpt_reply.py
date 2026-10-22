"""Observe the newest ChatGPT desktop reply without rereading older prose."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .claude_reply import (NoNewNode, ReplyTimeout, SendContext, WindowGone,
                           _control, followup, spoken_form)

_POLL_S = 0.3
_STABLE_S = 1.5
_TIMEOUT_S = 120.0
_ROLES = {"You said:": False, "ChatGPT said:": True}
_STOP = {"stop generating", "stop streaming", "stop response", "stop generating response"}
_FINISHED = {"copy", "copy response", "read aloud"}


@dataclass(frozen=True)
class _Message:
    index: int
    assistant: bool
    text: str = ""
    finished: bool = False


@dataclass(frozen=True)
class _Snapshot:
    messages: tuple[_Message, ...]
    streaming: bool
    chat_found: bool = True
    truncated: bool = False


def _role(control):
    if control.ControlTypeName == "TextControl":
        return _ROLES.get((control.Name or "").strip())
    return None


def _walk(root, *, skip=None, limit=5000):
    stack = [root]
    seen = 0
    while stack:
        if seen >= limit:
            raise NoNewNode()
        control = stack.pop()
        seen += 1
        if skip is not None and control == skip:
            continue
        yield control
        stack.extend(reversed(control.GetChildren()))


def _slots(container, role_cache=None):
    """Read boundaries only: flat markers and role-bearing immediate groups."""
    def role_of(control):
        if role_cache is None or control.ControlTypeName != "TextControl":
            return _role(control)
        runtime_id = getattr(control, "GetRuntimeId", None)
        key = tuple(runtime_id()) if runtime_id is not None else control
        if key not in role_cache:
            role_cache[key] = _role(control)
        return role_cache[key]

    slots = []
    for child in container.GetChildren():
        role = role_of(child)
        if role is None and child.ControlTypeName == "GroupControl":
            markers = [role for c in child.GetChildren() if (role := role_of(c)) is not None]
            role = markers[0] if len(markers) == 1 else None
        if role is not None:
            slots.append((role, []))
        if slots:
            slots[-1][1].append(child)
    return slots


def _content(roots):
    parts = []
    finished = False
    streaming = False
    # Flat message lists put timestamps beside prose groups. Text directly in
    # that list is a role or metadata; prose lives inside the following group.
    stack = [c for c in reversed(roots) if c.ControlTypeName != "TextControl"]
    seen = 0
    while stack:
        seen += 1
        if seen > 10000:
            return "", False, streaming, True
        control = stack.pop()
        kind = control.ControlTypeName
        name = (control.Name or "").strip()
        css = (getattr(control, "ClassName", "") or "").lower()
        if _role(control) is not None:
            continue  # includes the nested duplicate accessibility label
        if kind == "ButtonControl":
            finished |= name.lower() in _FINISHED
            streaming |= name.lower() in _STOP
            continue
        if kind in {"ToolBarControl", "StatusBarControl", "EditControl"}:
            # Toolbar buttons can signal completion, but never supply prose.
            if kind == "ToolBarControl":
                for child in _walk(control):
                    if child.ControlTypeName == "ButtonControl":
                        finished |= (child.Name or "").strip().lower() in _FINISHED
                        streaming |= (child.Name or "").strip().lower() in _STOP
            continue
        if "sr-only" in css or "thinking" in css:
            continue
        if kind == "CodeControl" or "code-block" in css:
            parts.append("There's a code block on screen.")
            continue
        if kind == "TableControl":
            parts.append("There's a table on screen.")
            continue
        if kind == "TextControl" and name:
            parts.append(name)
            continue
        children = control.GetChildren()
        buttons = [c for c in children if c.ControlTypeName == "ButtonControl"]
        if buttons:
            # Action groups sometimes duplicate a button label as sibling text.
            # Keep their signals without reading those accessibility duplicates.
            for button in buttons:
                finished |= (button.Name or "").strip().lower() in _FINISHED
                streaming |= (button.Name or "").strip().lower() in _STOP
            if any(_role(c) is not None for c in children):
                stack.extend(reversed([c for c in children if c.ControlTypeName != "ButtonControl"]))
            continue
        stack.extend(reversed(children))
    return "\n".join(parts), finished, streaming, False


class _Reader:
    def __init__(self, window):
        self.window = window
        self.root = _control(window)
        self.chat = next((c for c in _walk(self.root)
                          if c.ControlTypeName == "GroupControl" and c.Name == "Conversation"), None)
        self.container = None
        self.role_cache = {}
        if self.chat is not None:
            self.container = self._find_container()
        # Conversation also contains the composer in the recorded app. Prune
        # the message list, otherwise its Stop button would be hidden as well.
        editors = [c for c in _walk(self.root, skip=self.container)
                   if c.ControlTypeName == "EditControl"]
        self.chrome = self.root
        if len(editors) == 1 and hasattr(editors[0], "GetParentControl"):
            self.chrome = editors[0].GetParentControl() or self.root

    def _find_container(self):
        return next((c for c in _walk(self.chat) if c.ControlTypeName == "GroupControl"
                     and _slots(c, self.role_cache)), None)

    def snapshot(self):
        try:
            _control(self.window)  # validates HWND without recapturing old prose
            if self.chat is None:
                return _Snapshot((), False, False)
            if self.container is None:
                # Descend structural wrappers only until message boundaries appear.
                self.container = self._find_container()
            slots = _slots(self.container, self.role_cache) if self.container is not None else []
            latest = next((i for i in range(len(slots) - 1, -1, -1) if slots[i][0]), None)
            text, finished, local_stop, truncated = (
                _content(slots[latest][1]) if latest is not None else ("", False, False, False))
            # Composer and messages share Conversation; prune only history.
            stop = local_stop or any(c.ControlTypeName == "ButtonControl"
                                     and (c.Name or "").strip().lower() in _STOP
                                     for c in _walk(self.chrome, skip=self.container))
            messages = tuple(_Message(i + 1, role, text if i == latest else "",
                                      finished if i == latest else False)
                             for i, (role, _) in enumerate(slots))
            return _Snapshot(messages, stop, True, truncated)
        except (WindowGone, NoNewNode):
            raise
        except Exception as exc:
            raise WindowGone() from exc


def _snapshot(window):
    return _Reader(window).snapshot()


def message_index(window):
    snapshot = _snapshot(window)
    if not snapshot.chat_found:
        raise NoNewNode()
    return max((m.index for m in snapshot.messages), default=0)


def _seconds(value, default, maximum):
    try:
        parsed = float(value)
        return min(maximum, parsed) if math.isfinite(parsed) and parsed > 0 else default
    except (TypeError, ValueError):
        return default


def watch_for_reply(window, after_message_index, timeout_s=120.0, *, settle_s=1.5, observe=None):
    if isinstance(window, int):
        import uiautomation as uia
        with uia.UIAutomationInitializerInThread():
            return _watch_for_reply(window, after_message_index, timeout_s,
                                    settle_s=settle_s, observe=observe)
    return _watch_for_reply(window, after_message_index, timeout_s, settle_s=settle_s, observe=observe)


def _watch_for_reply(window, after_message_index, timeout_s, *, settle_s, observe):
    """Require settled newest prose and no stop, or its own completion button."""
    started = time.monotonic()
    deadline = started + _seconds(timeout_s, _TIMEOUT_S, _TIMEOUT_S)
    settle_s = _seconds(settle_s, _STABLE_S, 30.0)
    reader = _Reader(window)
    previous = None
    stable_since = None
    saw_new = False
    while time.monotonic() < deadline:
        read_started = time.monotonic()
        snapshot = reader.snapshot()
        fresh = [m for m in snapshot.messages if m.index > after_message_index]
        saw_new |= bool(fresh)
        newest = fresh[-1] if fresh and fresh[-1].assistant else None
        signature = (newest.index, newest.text) if newest and newest.text and not snapshot.truncated else None
        now = time.monotonic()
        if observe is not None:
            observe({"elapsed_s": round(now - started, 4), "read_s": round(now - read_started, 4),
                     "index": newest.index if newest else None,
                     "chars": len(newest.text) if newest else 0,
                     "words": len(newest.text.split()) if newest else 0,
                     "streaming": snapshot.streaming,
                     "finished_button": bool(newest and newest.finished),
                     "text_changed": signature != previous, "truncated": snapshot.truncated})
        if signature is None:
            stable_since = None
        elif signature != previous:
            stable_since = now
        elif (stable_since is not None and now - stable_since >= settle_s
              and (not snapshot.streaming or newest.finished)):
            return newest.text
        previous = signature
        time.sleep(_POLL_S)
    raise ReplyTimeout() if saw_new else NoNewNode()


def before_send(app, hwnd, *, send_word=None):
    try:
        if not app.config.get("chatgpt_voice", {}).get("enabled", True):
            return None
        import psutil
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if psutil.Process(pid).name().lower() != "chatgpt.exe":
            return None
        return SendContext(hwnd, message_index(hwnd), "", send_word)
    except Exception:
        return None


def start_after_send(app, context):
    from .agent_reply import start
    options = (getattr(app, "config", {}) or {}).get("chatgpt_voice", {}) or {}

    def watch(window, index, timeout):
        return watch_for_reply(window, index, timeout,
                               settle_s=options.get("settle_s", _STABLE_S))

    start(app, context, source="chatgpt", watch=watch,
          timeout_s=_seconds(options.get("reply_timeout_s", _TIMEOUT_S), _TIMEOUT_S, _TIMEOUT_S),
          failures=(WindowGone, ReplyTimeout, NoNewNode))
