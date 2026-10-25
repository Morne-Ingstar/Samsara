"""Whole-utterance report routing plus the existing command outcome adapter.

Kept outside session_modes to respect its size ceiling (803).
"""
from samsara.session_modes import (DispatchOutcome, StackItem, SessionMode,
                                   COMMAND_UNSUCCESSFUL_STATES, _COMMAND_HEARD_MAX,
                                   _short, _command_state)


def dispatch_command(manager, text, *, heard=None, allow_suggestion=True):
    from .coordinator import report_latest
    # 869s: command-lane punctuation is removed before dispatch and History.
    text = str(text or "").rstrip(" \t\r\n.,!?;:…")
    heard = str(text if heard is None else heard).rstrip(" \t\r\n.,!?;:…")
    if " ".join(str(text).strip().casefold().split()).rstrip(".!?") == "report that":
        opened = report_latest()
        return DispatchOutcome(kind="command_executed" if opened else "command_failed", detail={
            "phrase": "report that", "state": "executed" if opened else "failed",
            "reason": "" if opened else "Report preview unavailable",
        })
    heard = _short(text if heard is None else heard, _COMMAND_HEARD_MAX)
    result = manager._command_dispatch_fn(text)
    state = _command_state(result)
    if result.matched and state in COMMAND_UNSUCCESSFUL_STATES:
        # Recognised but not carried out: say so, but it is still a
        # command -- not a miss, not dictation, nothing to undo.
        return DispatchOutcome(kind="command_failed", detail={
            **getattr(result, "detail", {}),
            "phrase": result.phrase, "state": state, "heard": heard,
        })
    if result.matched and getattr(result, "awaiting_confirmation", False):
        # Nothing has run: no undo entry, and never "command_executed".
        return DispatchOutcome(kind="command_awaiting_confirmation", detail={
            "phrase": result.phrase, "state": state, "heard": heard,
        })
    if result.matched:
        manager._stack.push(StackItem(
            kind="command", payload=result.phrase or text,
            mode=SessionMode.COMMAND, timestamp=manager._clock(),
        ))
        return DispatchOutcome(kind="command_executed", detail={
            **getattr(result, "detail", {}),
            "phrase": result.phrase, "state": state, "heard": heard,
        })
    pack = getattr(result, "disabled_pack", None)
    if pack:
        return DispatchOutcome(kind="command_miss", detail={
            "reason": "pack_disabled", "pack": pack, "heard": heard,
        })
    suggested = manager._offer_command_suggestion(text) if allow_suggestion else None
    if suggested is not None:
        return suggested
    return DispatchOutcome(kind="command_miss", detail={"heard": heard})

