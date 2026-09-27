"""Voice access to the owner's existing prompt queue and reports."""
from __future__ import annotations

from pathlib import Path

from samsara import execution_policy
from samsara.agent_channel.queue_voice import (
    DEFAULT_PYTHON, DEFAULT_QUEUE, DEFAULT_REPORTS, QueuePaths,
    QueueView, prompt_id,
)
from samsara.command_registry import DispatchState
from samsara.execution_policy import Invocation, Route
from samsara.plugin_commands import command


def _view(app) -> QueueView:
    view = getattr(app, "_queue_voice_view", None)
    if view is None:
        config = (getattr(app, "config", {}) or {}).get("queue_voice", {}) or {}
        paths = QueuePaths(
            queue_root=Path(config.get("queue_root") or DEFAULT_QUEUE),
            reports_root=Path(config.get("reports_root") or DEFAULT_REPORTS),
            python=Path(config.get("python") or DEFAULT_PYTHON),
        )
        view = QueueView(paths)
        app._queue_voice_view = view
    return view


def _say(app, text: str, chip: str | None = None) -> None:
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(text, category="ava_response", interruptible=True)
    elif getattr(app, "tts_engine", None) is not None:
        app.tts_engine.speak(text)
    show = getattr(app, "_show_outcome_chip", None)
    if show is not None and chip:
        show(chip, "info")


def _read(app, action):
    try:
        text, count = action(_view(app))
    except Exception:
        _say(app, "I can't read the queue folder", "queue unavailable")
        return DispatchState.FAILED
    _say(app, text)
    return True


@command("what finished", aliases=["what's done", "whats done", "what is done"], pack="core",
         risk_class="safe", reversible=True, side_effects=["audio", "ui"])
def what_finished(app, _remainder=""):
    try:
        text, count = _view(app).finished()
    except Exception:
        _say(app, "I can't read the queue folder", "queue unavailable")
        return DispatchState.FAILED
    _say(app, text, f"queue: {count} finished")
    return True


@command("what can I run", pack="core", risk_class="safe", reversible=True,
         side_effects=["audio", "ui"])
def what_can_i_run(app, _remainder=""):
    return _read(app, QueueView.runnable)


@command("what's running", aliases=["whats running", "what is running"], pack="core",
         risk_class="safe", reversible=True, side_effects=["audio", "ui"])
def whats_running(app, _remainder=""):
    return _read(app, QueueView.running)


@command("read report", pack="core", risk_class="safe", reversible=True,
         side_effects=["audio", "ui"])
def read_report(app, remainder=""):
    pid = prompt_id(remainder)
    if pid is None:
        _say(app, "Say a report number.")
        return DispatchState.FAILED
    try:
        _say(app, _view(app).read_report(pid), f"queue: report {pid}")
    except Exception:
        _say(app, "I can't read the queue folder", "queue unavailable")
        return DispatchState.FAILED
    return True


@command("what did", pack="core", risk_class="safe", reversible=True,
         side_effects=["audio", "ui"])
def what_did(app, remainder=""):
    words = remainder.strip().split()
    if not words or words[-1].lower().rstrip(".?!") != "say":
        _say(app, "Say what did, a report number, say.")
        return DispatchState.FAILED
    return read_report(app, " ".join(words[:-1]))


@command("more", pack="core", risk_class="safe", reversible=True,
         side_effects=["audio", "ui"])
def more(app, _remainder=""):
    return _read(app, QueueView.more)


@command("stop queue", pack="core", risk_class="safe", reversible=True,
         side_effects=["audio", "ui"])
def stop_queue(app, _remainder=""):
    try:
        view = _view(app)
        if view.report_followup("stop queue") is None:
            _say(app, "No report is being read.")
            return True
        coordinator = getattr(app, "audio_coordinator", None)
        if coordinator is not None:
            coordinator.cancel_speech()
        _say(app, "Stopped reading the report.")
        return True
    except Exception:
        _say(app, "I can't read the queue folder", "queue unavailable")
        return DispatchState.FAILED


def _confirm(app, view: QueueView, command_id: str, remainder: str, question: str, effect):
    generation = execution_policy.current_generation(app)
    inv = Invocation(command_id, {"remainder": remainder} if remainder else {},
                     Route.EXACT, generation, source_text=question)

    def approved(_operation):
        decision = execution_policy.authorize(inv, app=app, confirmed=True)
        if isinstance(decision, execution_policy.Allowed):
            try:
                _say(app, effect())
            except Exception:
                _say(app, "I can't read the queue folder")
        else:
            _say(app, "That queue request expired.")

    execution_policy.stage_pending(app, inv, question, on_approve=approved,
                                   record_type="action")
    _say(app, question + " Say yes to confirm, or ava cancel.", "queue: confirm")
    return DispatchState.QUEUED


@command("accept", pack="core", risk_class="write", reversible=True,
         ai_visible=False, side_effects=["file", "audio", "ui"],
         preview_template="Accept {remainder}?")
def accept(app, remainder=""):
    pid = prompt_id(remainder)
    if pid is None:
        _say(app, "Say a prompt number to accept.")
        return DispatchState.FAILED
    try:
        view = _view(app)
        prompt = view._prompt(pid)
        if prompt is None or view.note.report_state(pid, prompt)[0] != "reported":
            _say(app, f"{pid} has no finished report to accept.")
            return DispatchState.FAILED
        return _confirm(app, view, "accept", pid, f"Accept {pid}?", lambda: view.accept(pid))
    except Exception:
        _say(app, "I can't read the queue folder", "queue unavailable")
        return DispatchState.FAILED


@command("queue note", pack="core", risk_class="write", reversible=True,
         ai_visible=False, side_effects=["file", "audio", "ui"],
         preview_template="Refresh queue note?")
def queue_note(app, _remainder=""):
    try:
        view = _view(app)
        return _confirm(app, view, "queue note", "", "Refresh queue note?", view.queue_note)
    except Exception:
        _say(app, "I can't read the queue folder", "queue unavailable")
        return DispatchState.FAILED
