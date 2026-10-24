"""Structured feedback and History retention for both listening displays.

The producer bridge keeps legacy three-argument indicators as a fallback.
No message text is parsed to decide severity, purpose or interaction ownership.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from enum import Enum
import sys
import threading
import logging
from typing import Callable
from samsara.message_durations import routine_receipt


DEFAULT_TTL_MS = 1800
BAND_HEIGHT_DIP = 56
_HANDOFF_CONTEXT = threading.local()


class FeedbackKind(str, Enum):
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFORMATION = "accent"
    QUESTION = "question"
    RUNNING = "running"
    LIVE = "live"


class FeedbackPurpose(str, Enum):
    RESULT = "result"
    QUESTION = "question"
    RUNNING = "running"


# Token names are resolved at paint time so a palette change updates the band.
FILL_TOKENS = {FeedbackKind.SUCCESS: "SUCCESS", FeedbackKind.WARNING: "WARNING",
               FeedbackKind.ERROR: "ERROR", FeedbackKind.INFORMATION: "ACCENT",
               FeedbackKind.QUESTION: "ACCENT", FeedbackKind.RUNNING: "ACCENT",
               FeedbackKind.LIVE: "ERROR"}
GLYPHS = {FeedbackKind.SUCCESS: "check", FeedbackKind.WARNING: "triangle",
          FeedbackKind.ERROR: "circle-x", FeedbackKind.INFORMATION: "arrow",
          FeedbackKind.QUESTION: "circle-question", FeedbackKind.RUNNING: "arrow",
          FeedbackKind.LIVE: "circle-x"}
KIND_WORDS = {FeedbackKind.SUCCESS: "success", FeedbackKind.WARNING: "warning",
              FeedbackKind.ERROR: "error", FeedbackKind.INFORMATION: "information",
              FeedbackKind.QUESTION: "question", FeedbackKind.RUNNING: "running",
              FeedbackKind.LIVE: "recording"}


@dataclass(frozen=True)
class Feedback:
    kind: FeedbackKind
    purpose: FeedbackPurpose = FeedbackPurpose.RESULT
    owner: str = ""
    resolves: str = ""
    alive: Callable[[], bool] | None = field(default=None, compare=False, repr=False)
    dismiss: Callable[[], bool] | None = field(default=None, compare=False, repr=False)


def feedback_for(kind: str, *, source: str = "", detail: dict | None = None) -> Feedback:
    """Explicit dispatch vocabulary; legacy pending is a question, never guessed."""
    detail = detail or {}
    if source == "tutorial_sample":
        return Feedback(FeedbackKind(kind), owner="tutorial_sample")
    if source == "hold_pending":
        return Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, "hold")
    if source in ("dictate_staged", "dictation_staged_chunk", "draft_parked"):
        return Feedback(FeedbackKind.INFORMATION, owner="draft")
    if source == "ava_dispatched":
        return Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, "ava")
    if source == "edit_thinking":
        return Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, "edit")
    if source == "edit_proposed":
        return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "edit")
    if source in ("edit_applied", "edit_discarded", "edit_refused"):
        return Feedback(FeedbackKind(kind), resolves="edit")
    if source == "command_cancel_window":
        return Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, "command")
    if source in ("command_executed", "hands_free_command_executed") and detail.get("state") in ("queued", "matched"):
        return Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, "command")
    if source == "command_suggestion_pending":
        return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "suggestion")
    if source == "personal_alias_offer":
        return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "alias")
    if source == "pending_reply":
        if detail.get("answer") == "extended":
            return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "command")
        return Feedback(FeedbackKind(kind), resolves="command")
    if source == "command_awaiting_confirmation":
        return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "command")
    if kind == "pending":
        return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "legacy")
    return Feedback(FeedbackKind(kind) if kind in {k.value for k in FeedbackKind}
                    else FeedbackKind.INFORMATION)


def producer_feedback(app, kind: str, source: str = "", detail: dict | None = None) -> Feedback:
    feedback = feedback_for(kind, source=source, detail=detail)
    # Read the already-loaded producer's structured operation; never import a
    # bootstrap/backend, infer a question from its words, or resolve another owner.
    policy = sys.modules.get("samsara.execution_policy")
    pending = getattr(policy, "pending_operation", None)
    op = pending() if callable(pending) else None
    manager = getattr(app, "_session_mode_manager", None)
    suggestion = getattr(manager, "_pending_command_suggestion", None)
    if source == "command_suggestion_pending":
        if not isinstance(suggestion, dict):
            suggestion = None
        if suggestion is None and op is None:
            return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION,
                            "suggestion", alive=lambda: False)
        from samsara.live_surface.suggestion_lifecycle import suggestion_callbacks
        alive, dismiss = suggestion_callbacks(app, manager, suggestion, policy, op)
        owner = "policy:" + str(op.op_id) if op is not None else str(id(suggestion))
        bound = Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION,
                         "suggestion:" + owner, alive=alive, dismiss=dismiss)
        if op is not None:
            op._suggestion_feedback = bound
        return bound
    if op is not None and getattr(op, "approved", None) is None:
        owner = "policy:" + str(op.op_id)
        alive = lambda: pending() is op and getattr(
            op, "is_live", lambda _: op.approved is None)(app)
        if kind == "pending" and source in ("", "command_cancel_window", "command_awaiting_confirmation", "pending_reply"):
            bound = getattr(op, "_suggestion_feedback", None)
            if bound is not None:
                return bound
            running = type(op).__name__ == "TimedOperation" and not getattr(op, "converted", False)
            return Feedback(FeedbackKind.RUNNING if running else FeedbackKind.QUESTION,
                            FeedbackPurpose.RUNNING if running else FeedbackPurpose.QUESTION,
                            owner, alive=alive)
    edit_session = sys.modules.get("samsara.ava_edit.session")
    edit_pending = getattr(edit_session, "pending", None)
    if kind == "pending" and source in ("", "edit_proposed") and callable(edit_pending):
        proposal = edit_pending()
        if proposal is not None:
            return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "edit:" + str(id(proposal)),
                            alive=lambda: edit_pending() is proposal)
    alias = getattr(app, "_personal_alias_offer", None)
    if source == "personal_alias_offer" and isinstance(alias, dict):
        return Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "alias:" + str(id(alias)),
                        alive=lambda: getattr(app, "_personal_alias_offer", None) is alias)
    return feedback


def schedule_feedback(app, label, kind, ttl_ms, *, source="", detail=None):
    indicator = getattr(app, "listening_indicator", None)
    context = getattr(_HANDOFF_CONTEXT, "value", None)
    if context is not None and context[0] is app:
        _, source, detail = context
    feedback = producer_feedback(app, kind, source, detail)
    if not getattr(indicator, "_records_feedback", False):
        remember_feedback(app, label, feedback)
    if callable(getattr(type(indicator), "show_feedback", None)):
        app._schedule_ui(indicator.show_feedback, label, kind, ttl_ms,
                         feedback)
    else:
        app._schedule_ui(indicator.show_outcome, label, kind, ttl_ms)


def remember_feedback(app, label, feedback):
    """Keep exact result/refusal words searchable in existing History > All."""
    store = getattr(app, "history_db", None)
    kind = getattr(feedback, "feedback", feedback.kind)
    if feedback.owner == "tutorial_sample" or not label or store is None or feedback.purpose is not FeedbackPurpose.RESULT or routine_receipt(label, kind):
        return
    try:
        store.add(raw_text="", display_text=str(label), mode="feedback",
                  status=kind.value, entry_type="feedback")
    except Exception:
        logging.getLogger(__name__).debug("Feedback history unavailable", exc_info=True)


@contextmanager
def alias_handoff(app, detail):
    """Carry alias metadata while preserving the producer's public chip call."""
    indicator = getattr(app, "listening_indicator", None)
    if not callable(getattr(type(indicator), "show_feedback", None)):
        yield
        return
    previous = getattr(_HANDOFF_CONTEXT, "value", None)
    _HANDOFF_CONTEXT.value = (app, "personal_alias_offer", detail)
    try:
        yield
    finally:
        if previous is None:
            del _HANDOFF_CONTEXT.value
        else:
            _HANDOFF_CONTEXT.value = previous


def schedule_hold_clear(app):
    indicator = getattr(app, "listening_indicator", None)
    if callable(getattr(type(indicator), "clear_feedback", None)):
        app._schedule_ui(indicator.clear_feedback, "hold")
    else:
        app._schedule_ui(indicator.clear_outcome)


def paint_glyph(painter, kind: FeedbackKind, rect, ink):
    """Geometric semantic cues; question curve is also independent of fonts."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QPainterPath, QPen, QPolygonF
    painter.save()
    painter.setPen(QPen(ink, 2)); painter.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = rect.center().x(), rect.center().y()
    if kind is FeedbackKind.SUCCESS:
        path = QPainterPath(QPointF(cx-8, cy)); path.lineTo(cx-2, cy+6); path.lineTo(cx+9, cy-7)
        painter.drawPath(path)
    elif kind is FeedbackKind.WARNING:
        painter.drawPolygon(QPolygonF([QPointF(cx, cy-10), QPointF(cx-11, cy+9), QPointF(cx+11, cy+9)]))
        painter.drawLine(QPointF(cx, cy-4), QPointF(cx, cy+2)); painter.drawPoint(QPointF(cx, cy+6))
    elif kind in (FeedbackKind.ERROR, FeedbackKind.LIVE, FeedbackKind.QUESTION):
        painter.drawEllipse(QRectF(cx-11, cy-11, 22, 22))
        if kind is FeedbackKind.QUESTION:
            path = QPainterPath(QPointF(cx-4, cy-4))
            path.cubicTo(cx-4, cy-10, cx+6, cy-9, cx+5, cy-3)
            path.cubicTo(cx+5, cy, cx, cy-1, cx, cy+3)
            painter.drawPath(path); painter.drawPoint(QPointF(cx, cy+7))
        else:
            painter.drawLine(QPointF(cx-4, cy-4), QPointF(cx+4, cy+4))
            painter.drawLine(QPointF(cx+4, cy-4), QPointF(cx-4, cy+4))
    else:
        painter.drawLine(QPointF(cx-9, cy), QPointF(cx+9, cy))
        painter.drawLine(QPointF(cx+9, cy), QPointF(cx+3, cy-6))
        painter.drawLine(QPointF(cx+9, cy), QPointF(cx+3, cy+6))
    painter.restore()
