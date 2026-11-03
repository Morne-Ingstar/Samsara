"""1029: immediate harmless commands; cancellation still protects real effects.

Uses the real session and policy with inert dispatch and manually fired timers.
No application bootstrap, microphone, model, desktop effects or live profile.
"""
import json
from pathlib import Path
from time import perf_counter_ns

import pytest

from samsara import execution_policy as ep
from tests.test_cancel_window_69 import (
    PRESERVE, _app, _clean, _manager, _probe, _register, _say, timers,
)


@pytest.fixture
def builtin_app():
    app = _app()
    app.command_executor.commands = json.loads(
        (Path(__file__).resolve().parents[1] / "commands.json").read_text(encoding="utf-8")
    )["commands"]
    return app


@pytest.mark.parametrize("phrase", [
    "snap left", "snap right", "snap top left", "snap top right",
    "minimize", "maximize", "minimize all", "switch window", "switch app",
    "scroll to top", "scroll to bottom", "next tab", "previous tab",
    "next field", "previous field", "select all", "select word left",
    "open chrome", "open notepad", "open calculator", "open claude web",
])
@pytest.mark.parametrize("all_commands", [False, True])
def test_real_harmless_commands_dispatch_without_a_window(builtin_app, timers, phrase, all_commands):
    app, calls = builtin_app, []
    # Even an existing saved three-second value must not slow UI commands.
    app.config["command_mode"].update(cancel_window_s=3.0, cancel_window_all_commands=all_commands)
    assert ep.classify(phrase, app=app)[0] in (ep.RISK_READ, ep.RISK_UI)
    manager = _manager(app, timers, calls, probe=_probe({phrase: (phrase, PRESERVE, False)}))
    assert _say(manager, app, phrase).kind == "hands_free_command_executed"
    assert calls == [phrase] and timers == [] and ep.pending_operation() is None


@pytest.mark.parametrize("risk,reversible,delay", [
    ("read", False, 0), ("ui", False, 0), ("ui", True, 0),
    ("write", True, 0), ("write", False, 1.5),
    ("destructive", True, 1.5), ("destructive", False, 1.5),
])
@pytest.mark.parametrize("reserved", [False, True])
def test_declared_risk_controls_the_window_even_for_aliases(risk, reversible, delay, reserved):
    _register("test effect", [], aliases=["effect alias"], risk_class=risk, reversible=reversible)
    assert ep.cancel_window_for("effect alias", _app(), reserved=reserved) == 0.0  # 1031a: no lane timers


@pytest.mark.parametrize("phrase", ["submit", "enter", "delete", "close window", "close tab"])
@pytest.mark.parametrize("reserved", [False, True])
def test_real_submit_delete_close_commands_keep_a_window(builtin_app, timers, phrase, reserved):
    app, calls = builtin_app, []
    # 1031a: this checks production lane timing, not the explicit lifecycle harness.
    manager = _manager(app, timers, calls, with_window=False,
                       probe=_probe({phrase: (phrase, PRESERVE, reserved)}))
    outcome = _say(manager, app, phrase)
    # 1031a: dispatch reaches authorization directly, without timer then question.
    assert outcome.kind == "hands_free_command_executed"
    assert calls == [phrase] and timers == []


@pytest.mark.parametrize("phrase", ["send message", "delete permanently", "purchase", "shutdown", "restart"])
def test_irreversible_effect_waits_and_still_requires_explicit_confirmation(timers, phrase):
    _register(phrase, [], risk_class="destructive", reversible=False)
    app, decisions = _app(), []
    manager = _manager(app, timers, [], probe=_probe({phrase: (phrase, PRESERVE, False)}))

    def dispatch(text):
        from types import SimpleNamespace
        decision = ep.authorize(ep.Invocation(text, generation=0), app=app)
        decisions.append(decision)
        return SimpleNamespace(matched=True, phrase=text, state="queued", awaiting_confirmation=True)

    manager._command_dispatch_fn = dispatch
    # 1031a: protected effects ask immediately, never after a timer.
    assert _say(manager, app, phrase).kind == "command_awaiting_confirmation"
    assert timers == []
    assert len(decisions) == 1 and isinstance(decisions[0], ep.NeedsConfirmation)


def _pending_close(app, timers):
    calls = []
    # 1031a: keep 1029's timer/onset regression IDs using the explicit lifecycle harness.
    _register("close window", [], risk_class="write")
    app.command_executor.commands.pop("close window", None)
    manager = _manager(app, timers, calls, probe=_probe({"close window": ("close window", PRESERVE, False)}))
    assert _say(manager, app, "close window").kind == "command_cancel_window"
    return manager, calls, ep.pending_operation()


@pytest.mark.parametrize("word", ["no", "cancel"])
@pytest.mark.parametrize("reply_processing_s", [1.78, 2.41])
def test_cancel_onset_inside_short_window_holds_for_slow_transcription(
    builtin_app, timers, word, reply_processing_s,
):
    manager, calls, op = _pending_close(builtin_app, timers)
    # Synthetic timing from the historical median/p90 described in the policy:
    # reply starts just before expiry, finishes after it. No real sleeps.
    now = [1.49]
    op._clock = lambda: now[0]
    op.deadline = 1.5
    ep.note_speech_onset(builtin_app)
    assert op.held and timers[-1].seconds == ep.CANCEL_HOLD_MAX_S == 6.0
    timers[0].fire()
    now[0] += reply_processing_s
    assert _say(manager, builtin_app, word).detail == {"answer": "rejected"}
    timers[-1].fire()
    assert calls == [] and op.approved is False and ep.pending_operation() is None


@pytest.mark.parametrize("word", ["yes", "do it"])
def test_yes_or_do_it_runs_early_once(builtin_app, timers, word):
    manager, calls, op = _pending_close(builtin_app, timers)
    assert _say(manager, builtin_app, word).detail == {"answer": "approved"}
    assert calls == ["close window"] and op.outcome_reason == "you said yes"
    assert timers[0].cancelled and ep.pending_operation() is None
    timers[0].fire()
    assert calls == ["close window"]


def test_unknown_effect_keeps_explicit_confirmation():
    app = _app()
    _register("unrated effect", [], risk_class="unknown")
    assert ep.cancel_window_for("unrated effect", app) == 0
    assert isinstance(ep.authorize(ep.Invocation("unrated effect", generation=0), app=app), ep.Allowed)  # 1031a: no new waits for unrated ordinary commands
    assert ep.cancel_window_for("unregistered effect", app) == 0
    assert isinstance(ep.authorize(ep.Invocation("unregistered effect", generation=0), app=app), ep.Denied)


def test_explicit_off_and_user_duration_remain_supported(builtin_app):
    builtin_app.config["command_mode"]["cancel_window_s"] = 0
    assert ep.cancel_window_for("close window", builtin_app) == 0
    builtin_app.config["command_mode"]["cancel_window_s"] = 3.0
    assert ep.cancel_window_for("close window", builtin_app) == 0.0  # 1031a: T3 is never timed
    assert ep.cancel_window_for("snap left", builtin_app) == 0


def test_recognized_snap_left_has_no_scheduled_delay(builtin_app, timers, record_property):
    calls = []
    manager = _manager(builtin_app, timers, calls, probe=_probe({"snap left": ("snap left", PRESERVE, False)}))
    start = perf_counter_ns()
    outcome = _say(manager, builtin_app, "snap left")
    elapsed_ms = (perf_counter_ns() - start) / 1_000_000
    record_property("recognized_text_to_inert_dispatch_ms", elapsed_ms)
    # This measures only policy/session overhead, not speech endpointing or STT.
    assert outcome.kind == "hands_free_command_executed" and calls == ["snap left"]
    assert timers == [] and ep.pending_operation() is None
