"""One desktop-agent reply lifecycle and follow-up state per application."""
from __future__ import annotations

import re

from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)


def state(app):
    current = getattr(app, "_agent_reply_state", None)
    if current is None:
        current = getattr(app, "_claude_voice_state", None)
        if current is None:
            current = {"token": None, "last": "", "rest": "", "handle": None,
                       "expires": 0.0, "speaking": False, "resume": None,
                       "sections": [], "code_blocks": [], "section_cursor": -1,
                       "source": "claude"}
        app._agent_reply_state = current
        # Compatibility alias; both names always refer to the same state.
        app._claude_voice_state = current
    return current


def start(app, context, *, source, watch, timeout_s, failures):
    if context is None:
        return
    from . import claude_reply as speech

    current = state(app)
    token = object()
    current.update(token=token, source=source, expires=0.0, last="", rest="", full="",
                   sections=[], code_blocks=[], section_cursor=-1, handle=None,
                   speaking=False, whole_generation=current.get("whole_generation", 0) + 1)

    def resume():
        if context.send_word and getattr(app, "app_state", None) == "asleep":
            try:
                import win32gui
                if win32gui.GetForegroundWindow() == context.hwnd:
                    app._start_wake_session(mode="focus_dictate", send_word=context.send_word)
            except Exception:
                logger.warning("[AGENT-VOICE] wake resume failed source=%s", source)

    current["resume"] = resume

    def run():
        try:
            reply = watch(context.hwnd, context.after_message_index, timeout_s)
            if state(app)["token"] is not token:
                return
            from .spoken_digest import first_reply
            first = first_reply(app, reply, source=source)
            if state(app)["token"] is not token:
                return
            cap = speech._word_cap(getattr(app, "config", {}) or {}, source=source)
            spoken = speech.spoken_form(first.spoken, cap)[0]
            current.update(rest=reply, full=reply, sections=first.sections,
                           code_blocks=[m.group(2) for m in re.finditer(
                               r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n(.*?)^\s*\1\s*$", reply)])
            speech._speak(app, spoken, token, on_done=resume)
        except failures[0]:
            speech._speak(app, "I lost the ChatGPT window" if source == "chatgpt"
                          else "I lost the Claude window", token, on_done=resume)
        except failures[1:]:
            speech._speak(app, "The reply is still going." if source == "chatgpt"
                          else "Claude didn't answer", token, on_done=resume)

    try:
        thread_registry.spawn(f"{source}.reply", run)
    except Exception:
        logger.warning("[AGENT-VOICE] watcher could not start source=%s", source)
