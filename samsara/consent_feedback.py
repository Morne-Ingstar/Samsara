"""Bound consent copy for both displays; no command, body or tier inference."""
from __future__ import annotations

import math


def compact_max_lines(indicator):
    """Expand consent while retaining the current retry/report button layout."""
    return None if (indicator._report_width() or
                    getattr(indicator._chip_feedback, 'owner', '').startswith('policy:')) else 2


def question_label(op):
    """Keep the exact locally prepared question when the reply mic opens."""
    # PLACEHOLDER COPY (1031f)
    seconds = max(0, math.ceil(op.deadline - op._clock()))
    return (f"{op.prompt}\nSay yes to confirm or no to cancel. "
            f"No answer cancels within {seconds} s. Cancel before it runs.")


def pending_label(app, label, feedback):
    """Decorate only the operation that owns this particular UI handoff."""
    if not feedback.owner.startswith("policy:"):
        return label
    from samsara.execution_policy import pending_operation
    op = pending_operation()
    if op is None or feedback.owner != "policy:" + op.op_id:
        return label
    if getattr(op, "kind", "") in {"choice", "cancel_window"}:
        if (getattr(op.handling, "mode", "") != "short_send_grace"
                or getattr(op, "converted", False)):
            return label  # Retain legacy choice/wait semantics.
        target = op.invocation.effect_target
        message = getattr(target, "message", None) or target
        preview = getattr(message, "preview", "")
        preview = preview() if callable(preview) else preview
        destination = getattr(target, "label", "") or op.label
        # PLACEHOLDER COPY (1031f): cancellation is pre-send, never a recall.
        return (f"Sending to {destination}: {preview or destination}\n"
                "Say cancel or send now. Cancel before sending only; no guaranteed recall.")
    return question_label(op)


def prioritize_confirmation(app):
    """Retire queued speech before asking, including in Silent feedback mode."""
    coordinator = getattr(app, "audio_coordinator", None)
    cancel = getattr(coordinator, "cancel_speech", None)
    if callable(cancel):
        cancel()
    else:
        engine = getattr(app, "tts_engine", None)
        cancel = getattr(engine, "cancel_all", None)
        if callable(cancel):
            cancel()
