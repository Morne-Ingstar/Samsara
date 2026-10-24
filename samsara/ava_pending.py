"""One bounded Ava question per app, ahead of literal command dispatch."""
from dataclasses import dataclass
import math
import time

from samsara import execution_policy as policy
from samsara.ava_turn_queue import ownership_lock
from samsara.live_surface.feedback import Feedback, FeedbackKind, FeedbackPurpose
from samsara.runtime import thread_registry


@dataclass
class Question:
    runner: object
    kind: str
    text: str
    options: tuple
    operation: object = None
    deadline: float = 0
    opened: bool = False
    capture_owned: bool = False
    state_owned: bool = False
    previous: tuple = ()
    timer: object = None
    duration: float = None
    label: str = ""
    reprompted: bool = False


def reply_seconds(app):
    try:
        value = float(app.config.get("ava", {}).get("reply_window_seconds", 10))
        return max(2.0, min(30.0, value)) if math.isfinite(value) else 10.0
    except (TypeError, ValueError):
        return 10.0


def short_answer_question(app):
    """Only a live question that owns wake routing may replace its audio gate."""
    q = getattr(app, "_ava_question", None)
    if (q is not None and q.opened and q.state_owned and _live(app, q)
            and (getattr(app, "app_state", None), getattr(app, "wake_word_triggered", False)) == ("command_window", True)
            and getattr(q.runner, "short_answer", q.kind == "yes_no")):
        return q
    return None


def _capture_history(app, q, reason):
    missed = getattr(q.runner, "missed", None)
    if callable(missed):
        missed(reason)
    else:
        write = getattr(app, "_log_history", None)
        if callable(write):
            write(raw_text=q.text, display_text=f"Question: {reason}", mode="command",
                  status="empty", entry_type="failed")


def missed_capture(app, reason, *, question=None):
    """Keep a bounded yes/no answer window; speak at most one second chance."""
    from plugins.commands import ask_ollama as ava
    with ownership_lock(app):
        q = short_answer_question(app)
        if q is None or (question is not None and q is not question):
            return False
        _capture_history(app, q, reason)
        if q.reprompted:
            return True
        q.reprompted, q.opened = True, False
        if q.timer:
            q.timer.cancel()
        # Bound cancelled TTS, then give the full configured window on completion.
        q.deadline = time.monotonic() + 60
        _tick(app, q)
        if not ava.speak(app, "Say yes or no", category="confirmation", on_done=lambda: _open(app, q)):
            _open(app, q)
        return True


def _feedback(app, label, question=None):
    indicator = getattr(app, "listening_indicator", None)
    schedule = getattr(app, "_schedule_ui", None)
    if indicator is None or not callable(schedule):
        return
    if callable(getattr(type(indicator), "show_feedback", None)):
        feedback = Feedback(FeedbackKind.QUESTION if question else FeedbackKind.INFORMATION,
            FeedbackPurpose.QUESTION if question else FeedbackPurpose.RESULT,
            owner="ava_question" if question else "", resolves="" if question else "ava_question",
            alive=(lambda: getattr(app, "_ava_question", None) is question) if question else None)
        schedule(indicator.show_feedback, label, "pending" if question else "accent",
                 None if question else 1800, feedback)
    else:
        schedule(indicator.show_outcome, label, "pending" if question else "accent", None if question else 1800)


def _clear(app, question, label=""):
    if getattr(app, "_ava_question", None) is not question:
        return
    app._ava_question = None
    if question.timer:
        question.timer.cancel()
    if question.capture_owned:
        # Do not overwrite a different lane that took over while waiting.
        if question.state_owned and (app.app_state, app.wake_word_triggered) == ("command_window", True):
            app.app_state, app.wake_word_triggered = question.previous
        app._release_wake_consumer("ava_question")
    if label:
        _feedback(app, label)


def _live(app, q):
    return (getattr(app, "_ava_question", None) is q and q.runner.eligible()
            and (q.operation is None or policy.pending_operation() is q.operation)
            and (q.operation is not None or policy.pending_operation() is None))


def _expire(app, q):
    if getattr(app, "_ava_question", None) is not q:
        return
    _clear(app, q, "Not saved." if q.kind == "teaching" else
           "Cancelled." if isinstance(q.runner, policy._ConfirmationListener) else "Ava question expired.")
    expired = getattr(q.runner, "expired", None)
    if callable(expired):
        expired()
    if q.operation is not None:
        # Release by identity, including if clearing capture installed a successor.
        policy._release_slot(q.operation)
        q.operation.cancel(app, "expired", chip=False)


def _tick(app, q):
    with ownership_lock(app):
        if not _live(app, q):
            if q.kind == "teaching" or isinstance(q.runner, policy._ConfirmationListener):
                _expire(app, q)
            else:
                _clear(app, q, "Ava question closed.")  # Stand-in copy (624).
            return
        if time.monotonic() >= q.deadline:
            if short_answer_question(app) is q and not q.reprompted:
                missed_capture(app, "timeout (retry)")
                return
            if q.kind != "teaching":
                _capture_history(app, q, "timeout")
            _expire(app, q)
            return
        q.timer = thread_registry.timer("ava.question.expiry", 0.1, lambda: _tick(app, q), daemon=True)


def _open(app, q):
    with ownership_lock(app):
        if not _live(app, q):
            if q.kind == 'teaching' or isinstance(q.runner, policy._ConfirmationListener):
                _expire(app, q)
            else:
                _clear(app, q)
            return
        if q.opened:
            return
        if q.timer: q.timer.cancel()
        q.opened = True
        q.deadline = time.monotonic() + (q.duration if q.duration is not None else reply_seconds(app))
        if not q.capture_owned:
            q.previous = (getattr(app, "app_state", "asleep"), getattr(app, "wake_word_triggered", False))
        ensure = getattr(app, "_ensure_wake_consumer", None)
        if callable(ensure) and not q.capture_owned:
            # A running toggle session already has its own routing and state.
            active = (getattr(app, "command_mode_active", False)
                      and app.config.get("command_mode", {}).get("mode") == "toggle")
            if not active and q.previous[0] in {"asleep", "command_window"}:
                if q.previous[0] == "command_window":
                    q.previous = ("asleep", False)
                    timer = getattr(app, "wake_word_timer", None)
                    if timer: timer.cancel()
                q.state_owned = True
                app.app_state, app.wake_word_triggered = "command_window", True
            q.capture_owned = True
            try:
                ensure("ava_question")
            except Exception:
                if q.kind == 'teaching' or isinstance(q.runner, policy._ConfirmationListener):
                    _expire(app, q)
                else:
                    _clear(app, q, "Ava reply microphone unavailable. Hold the Ava key to ask again.")
                return
        # This feedback bridge renders in Compact and Full.
        opened = getattr(q.runner, "opened", None)
        if callable(opened):
            opened()
        _feedback(app, q.label or "Ava is waiting for your answer. " + q.text, q)
        _tick(app, q)


def ask(app, runner, kind, text, options, *, operation=None, duration=None, label=""):
    from plugins.commands import ask_ollama as ava
    with ownership_lock(app):
        if not runner.eligible() or (policy.pending_operation() is not operation):
            return ava.TurnOutcome("ask", "refused", reason="another_confirmation")
        old = getattr(app, "_ava_question", None)
        if old is not None:
            if old.kind == "teaching":
                _expire(app, old)
            else:
                _clear(app, old)
        q = Question(runner, kind, text, options, operation=operation,
                     deadline=time.monotonic() + 60, duration=duration, label=label)
        app._ava_question = q
        _tick(app, q)  # Also retires a question if speech is cancelled or AI is switched off.
        spoken = text + (" " + "; ".join(options) + "." if options else "")
        # The answer window starts after speech completes, including Silent mode.
        if not ava.speak(app, spoken, category="confirmation", on_done=lambda: _open(app, q)):
            _open(app, q)
        return ava.TurnOutcome("ask", "queued", name="question")


def consume(app, text, *, capture_path=None):
    """True means this utterance belongs to Ava, even if it is not a valid answer."""
    from plugins.commands import ask_ollama as ava
    with ownership_lock(app):
        q = getattr(app, "_ava_question", None)
        if q is None:
            return False
        if policy.is_stop_utterance(text):
            if q.kind == "teaching":
                _expire(app, q)
                return True
            _clear(app, q, "Ava question cancelled.")
            policy.stop_all(app, "ava question cancelled")
            return True
        if q.kind == "policy" and isinstance(q.runner, policy._ConfirmationListener):
            # Capture owns the microphone, not the definition of an answer.
            # Keyed replies can arrive before the readback finishes; ordinary
            # words remain unconsumed and leave the pending operation intact.
            if policy.pending_operation() is not q.operation or (q.opened and time.monotonic() >= q.deadline):
                _expire(app, q)
                return True
            result = policy.answer_pending(app, text, capture_path=capture_path)
            return result is not None
        # Never fall through to the global yes command for a retired question.
        if not _live(app, q) or (q.opened and time.monotonic() >= q.deadline):
            _expire(app, q)
            return True
        if not q.opened:
            return True  # The question itself is still being spoken.
        # Deterministic follow-ups (635) share capture/expiry, without a model turn.
        answer = getattr(q.runner, "answer", None)
        if callable(answer):
            _clear(app, q)
            return answer(text) is not False
        answer = (text or "").strip().rstrip(".!?").casefold()
        if answer in {"nevermind", "never mind"}:
            _clear(app, q, "Ava question cancelled.")
            if q.operation is not None:
                policy.answer_pending(app, "no", capture_path=capture_path)
            return True
        if q.kind == "policy":
            # The existing voice filters, target bindings and single-use guard
            # resolve this operation. We never manufacture a confirmed flag here.
            reply = policy.classify_reply(text)
            if reply is None:
                return True
            if reply == policy.REPLY_WAIT:
                if policy.answer_pending(app, text, capture_path=capture_path) == "extended":
                    q.deadline = time.monotonic() + reply_seconds(app)
                return True
            _clear(app, q, "Ava question answered.")
            result = policy.answer_pending(app, text, capture_path=capture_path)
            if result and result.startswith("refused:") and result not in {"refused:expired", "refused:stale"}:
                q.runner.failure(result)
            return True
        if q.kind == "yes_no":
            reply = policy.classify_reply(text)
            if reply not in {policy.REPLY_YES, policy.REPLY_NO}:
                return True
            _clear(app, q, "Ava question cancelled." if reply == policy.REPLY_NO else "Ava question answered.")
            if reply == policy.REPLY_YES:
                q.runner.run()  # Still unconfirmed: destructive steps ask locally.
            return True
        if not answer or len(text) > 2000:
            return True
        if q.kind == "choice":
            normalized = [o.strip().casefold() for o in q.options]
            if answer.isdigit() and 1 <= int(answer) <= len(normalized):
                text = q.options[int(answer) - 1]
            elif answer not in normalized:
                return True
        _clear(app, q)
        # Only the user's original request and reply supply action grounding.
        # Re-enter 536's ordered lane, rather than making a second provider client.
        ava.handle_ask_ava(app, remainder=q.runner.utterance + "\nFollow-up answer: " + text,
                           generation=q.runner.generation)
        return True
