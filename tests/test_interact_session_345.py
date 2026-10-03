"""345c: sequence controller with fake windows, clock, timer and UI ports."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama, interact as bound_plugin
from samsara import execution_policy as policy, plugin_commands
from samsara.interact import parse_action
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals
from samsara.subjects import OnsetContext
from samsara.window_targets import BoundWindowEffects, WindowSnapshot, WindowTargetAdapter
from samsara.window_targets import EffectResult


GOOD = UtteranceSignals(True, (1.0,))
BAD = UtteranceSignals(False, (9.0,))


class Clock:
    def __init__(self):
        self.now = 1_000_000_000
        self.timers = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * 1e9)

    def schedule(self, seconds, callback):
        timer = SimpleNamespace(seconds=seconds, callback=callback, cancelled=False)
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.timers.append(timer)
        return timer


class Native:
    def __init__(self):
        self.windows_by_id = {
            101: WindowSnapshot(101, 101, 0, True, False, 0, "Downloads - Explorer",
                                "CabinetWClass", 42, r"C:\Windows\explorer.exe", 123.5, "ok"),
            202: WindowSnapshot(202, 202, 0, True, False, 0, "Other - Explorer",
                                "CabinetWClass", 42, r"C:\Windows\explorer.exe", 123.5, "ok"),
        }
        self.pointer_reads = 0

    def window(self, hwnd):
        return self.windows_by_id.get(hwnd)

    def windows(self):
        return tuple(self.windows_by_id)

    def sample_pointer(self):
        self.pointer_reads += 1
        raise AssertionError("controller must use only the frozen onset")


class Actions:
    def __init__(self):
        self.calls = []

    def focus(self, hwnd, preflight):
        assert preflight()
        self.calls.append(("focus", hwnd))
        return True

    def show(self, hwnd, how):
        self.calls.append((how, hwnd))

    def state(self, hwnd, how):
        return True

    def move(self, hwnd, number, preflight):
        assert preflight()
        self.calls.append(("move", hwnd, number))
        return True

    def snap(self, hwnd, side, preflight):
        assert preflight()
        self.calls.append(("snap", hwnd, side))
        return True

    def close(self, hwnd):
        self.calls.append(("close", hwnd))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    ask_ollama.clear_pending_action()
    for entry in plugin_commands._MODULE_ENTRIES[bound_plugin.__name__].values():
        plugin_commands._register(entry)
    clock, native, actions = Clock(), Native(), Actions()
    targets = WindowTargetAdapter(native, own_pid=999, aliases={"explorer": "explorer.exe"})
    effects = BoundWindowEffects(targets, actions, sleep=lambda _: None)
    app = SimpleNamespace(config={}, _ava_cmd_generation=11, _show_outcome_chip=Mock())
    calls = SimpleNamespace(inject=Mock(), command=Mock(), agent=Mock(), mode=Mock())
    manager = SessionModeManager(
        abort_phrases=["cancel", "abort"], foreground_exe_resolver=lambda: "notepad.exe",
        inject_fn=calls.inject, remove_chars_fn=Mock(),
        command_dispatch_fn=calls.command, agent_dispatch_fn=calls.agent,
        on_mode_change=calls.mode, buffer_dictate_until_commit=True,
        stop_fn=lambda _: {}, pending_reply_fn=lambda text: policy.answer_pending(app, text),
    )
    manager.reset(SessionMode.DICTATE)
    labels, questions = [], []
    controller = manager.configure_interact(
        targets=targets, effects=effects, app=app,
        publish=lambda label: labels.append(label) or True,
        present_question=lambda question: questions.append(question) or clock(),
        clock_ns=clock, schedule=clock.schedule, known_names={"explorer"},
    )

    def onset(pointer=101, *, lag_ns=10_000_000, view=None):
        capture = view or controller.capture_view()
        return OnsetContext(
            str(clock.now), clock.now - lag_ns, clock.now,
            capture.session_epoch, capture.arm_epoch, capture.policy_generation,
            capture.subject_ref,
            capture.subject_ref.revision if capture.subject_ref else None,
            (10, 10), targets.inspect(pointer).identity if pointer else None,
            None if pointer else "no_window",
        )

    def say(text, pointer=101, signals=GOOD, *, context=None):
        return manager.dispatch_utterance(text, signals,
                                          context if context is not None else onset(pointer))

    yield SimpleNamespace(manager=manager, controller=controller, clock=clock,
                          native=native, actions=actions, app=app, calls=calls,
                          labels=labels, questions=questions, onset=onset, say=say)
    ask_ollama.clear_pending_action()


@pytest.mark.parametrize("text,expected", [
    ("focus", ("focus", {}, False)),
    ("minimize this window", ("minimize", {}, True)),
    ("left", ("snap", {"side": "left"}, False)),
    ("snap top right", ("snap", {"side": "top right"}, False)),
    ("move to monitor two", ("move", {"monitor": 2}, False)),
    ("close explorer", None),
    ("please close", None),
    ("focus and close", None),
])
def test_exact_whole_utterance_grammar(text, expected):
    assert parse_action(text) == expected


def test_three_successive_actions_keep_arm_and_original_window(rig):
    rig.manager._dictate_pending_buffer = "unfinished draft"
    assert rig.say("interact").kind == "interact_armed"
    assert rig.manager.mode is SessionMode.COMMAND
    assert rig.controller.subject.identity.hwnd == 101
    for phrase, pointer in (("left", 202), ("maximize", 202), ("focus", 202)):
        rig.clock.advance(0.2)
        assert rig.say(phrase, pointer).kind == "interact_action"
        assert rig.controller.state.phase == "ARMED"
    assert rig.actions.calls == [("snap", 101, "left"), ("maximize", 101), ("focus", 101)]
    assert rig.say("exit interact").kind == "interact_exit"
    assert rig.manager.mode is SessionMode.DICTATE
    assert rig.manager.dictate_pending_buffer == "unfinished draft"
    assert rig.controller.subject is None
    assert rig.native.pointer_reads == 0


def test_explicit_pointer_rebind_uses_new_revision_once(rig):
    rig.say("interact")
    old = rig.controller.subject
    rig.clock.advance(0.2)
    assert rig.say("minimize this window", pointer=202).kind == "interact_action"
    assert rig.actions.calls == [("minimize", 202)]
    assert rig.controller.subject.revision == old.revision + 1
    assert rig.controller.subject.identity.hwnd == 202


def test_stale_capture_cannot_rebind_or_act(rig):
    before_arm = rig.onset(202)
    rig.say("interact")
    assert rig.say("focus", context=before_arm).kind == "interact_refused"
    stale = rig.onset(101)
    rig.clock.advance(0.2)
    rig.say("this window", 202)
    assert rig.say("focus", context=stale).kind == "interact_refused"
    assert rig.actions.calls == []


def test_chooser_freezes_refs_and_revalidates_choice(rig):
    rig.say("interact")
    rig.clock.advance(0.2)
    assert rig.say("Explorer").kind == "interact_choosing"
    assert len(rig.controller.state.candidates) == 2
    assert "1. Downloads - Explorer" in rig.labels[-1]
    assert "2. Other - Explorer" in rig.labels[-1]
    assert rig.say("focus", pointer=202).kind == "interact_refused"
    assert rig.actions.calls == []
    rig.clock.advance(0.2)
    assert rig.say("window 2", pointer=101).kind == "interact_selected"
    assert rig.controller.subject.identity.hwnd == 202


def test_chooser_timeout_and_silent_idle_timer(rig):
    rig.say("interact")
    rig.clock.advance(0.2)
    rig.say("Explorer")
    callback = rig.clock.timers[-1].callback
    rig.clock.advance(16)
    callback()
    assert rig.controller.state.phase == "OFF"
    rig.say("interact")
    callback = rig.clock.timers[-1].callback
    rig.clock.advance(31)
    callback()
    assert rig.controller.state.phase == "OFF"
    assert rig.manager.mode is SessionMode.DICTATE


def test_ambient_and_rejected_text_do_not_refresh_deadline(rig):
    rig.say("interact")
    deadline = rig.controller.state.deadline_ns
    rig.clock.advance(12)
    assert rig.say("the window looks nice").kind == "interact_unknown"
    assert rig.say("focus", signals=BAD).kind == "interact_refused"
    assert rig.controller.state.deadline_ns == deadline
    assert rig.calls.inject.call_count == 0
    assert rig.calls.agent.call_count == 0


def test_close_yes_after_question_runs_once_and_stays_armed(rig):
    rig.say("interact")
    rig.clock.advance(0.2)
    assert rig.say("close").kind == "interact_confirm"
    assert rig.actions.calls == []
    assert "Downloads" in rig.questions[-1]
    rig.clock.advance(0.2)
    assert rig.say("yes", pointer=202).kind == "interact_reply"
    assert rig.actions.calls == [("close", 101)]
    assert rig.controller.subject is None
    assert rig.controller.state.phase == "ARMED"
    assert rig.say("yes").kind != "interact_reply"
    assert rig.actions.calls == [("close", 101)]


def test_pre_question_yes_and_bad_audio_cannot_approve(rig):
    rig.say("interact")
    early = rig.onset()
    rig.clock.advance(0.2)
    rig.say("close")
    assert rig.say("yes", context=early).kind == "interact_refused"
    rig.clock.advance(0.2)
    assert rig.say("yes", signals=BAD).kind == "interact_refused"
    assert rig.actions.calls == []
    assert rig.say("no").kind == "interact_reply"
    assert rig.controller.state.phase == "ARMED"
    assert rig.actions.calls == []


def test_reset_stop_and_late_timer_invalidate_confirmation(rig):
    rig.say("interact")
    rig.clock.advance(0.2)
    rig.say("close")
    stale_timer = rig.clock.timers[-1].callback
    assert rig.say("halt", signals=BAD).kind == "stopped"
    assert rig.controller.state.phase == "OFF"
    stale_timer()
    rig.manager.reset(SessionMode.COMMAND)
    assert rig.say("yes").kind != "interact_reply"
    assert rig.actions.calls == []


def test_missing_context_and_lag_refuse_arm_or_action(rig):
    assert rig.manager.dispatch_utterance("interact", GOOD).kind == "interact_refused"
    assert rig.say("interact", context=rig.onset(lag_ns=200_000_000)).kind == "interact_refused"
    rig.say("interact")
    assert rig.manager.dispatch_utterance("focus", GOOD).kind == "interact_refused"
    assert rig.actions.calls == []


def test_old_off_capture_cannot_rearm_after_exit(rig):
    rig.say("interact")
    old = rig.onset()
    rig.say("exit interact")
    assert rig.say("interact", context=old).kind == "interact_refused"
    assert rig.controller.state.phase == "OFF"


def test_another_pending_question_refuses_arming(rig):
    with ask_ollama._pending_action_lock:
        ask_ollama._pending_action = {"type": "unrelated_question"}
    assert rig.say("interact").kind == "interact_refused"
    assert rig.manager.mode is SessionMode.DICTATE


def test_reset_republishes_epoch_and_stale_timer_cannot_end_next_arm(rig):
    rig.say("interact")
    stale_timer = rig.clock.timers[-1].callback
    old_epoch = rig.controller.capture_view().session_epoch
    rig.manager.reset(SessionMode.DICTATE)
    assert rig.controller.capture_view().session_epoch == old_epoch + 1
    rig.say("interact")
    stale_timer()
    assert rig.controller.state.phase == "ARMED"


def test_boolean_effect_cannot_rebind_subject(rig):
    rig.say("interact")
    original = rig.controller.subject
    rig.controller._on_result(True)
    rig.controller._on_result(EffectResult(True, "done", True))
    assert rig.controller.subject is original


def test_native_close_failure_is_reported_as_refusal(rig):
    rig.say("interact")
    rig.clock.advance(0.2)
    rig.say("close")

    def fail(_):
        raise OSError("blocked")

    rig.actions.close = fail
    rig.clock.advance(0.2)
    assert rig.say("yes").kind == "interact_refused"
    assert rig.controller.subject is not None
    assert rig.controller.state.phase == "ARMED"


def test_degraded_lane_switch_is_consumed_without_dictation(rig):
    rig.say("interact")
    assert rig.say("dictate mode", signals=BAD).kind == "interact_refused"
    assert rig.controller.state.phase == "ARMED"
    assert rig.calls.inject.call_count == 0
    assert rig.say("exit interact", signals=BAD).kind == "interact_exit"


def test_forced_switch_hold_and_device_loss_disarm(rig):
    rig.say("interact")
    rig.manager.force_mode(SessionMode.AVA)
    assert rig.controller.state.phase == "OFF"
    rig.manager.reset(SessionMode.DICTATE)
    rig.say("interact")
    rig.manager.invalidate_interact("microphone lost")
    assert rig.controller.state.phase == "OFF"
    rig.say("interact")
    rig.manager._live_surface_enabled = True
    rig.manager.pause_draft()
    assert rig.controller.state.phase == "OFF"


def test_reply_speech_started_before_question_cannot_approve(rig):
    rig.say("interact")
    rig.clock.advance(0.2)
    rig.say("close")
    rig.clock.advance(0.02)
    overlapping = rig.onset(lag_ns=30_000_000)
    assert rig.say("yes", context=overlapping).kind == "interact_refused"
    assert rig.actions.calls == []


def test_feedback_failure_cannot_leave_a_silent_arm(rig):
    rig.controller._publish = lambda _: False
    assert rig.say("interact").kind == "interact_refused"
    assert rig.controller.state.phase == "OFF"
    assert rig.manager.mode is SessionMode.DICTATE


def test_dead_target_and_boolean_last_action_cannot_rebind(rig):
    rig.say("interact")
    rig.native.windows_by_id.pop(101)
    assert rig.say("focus", pointer=202).kind == "interact_refused"
    assert rig.controller.subject is None
    assert rig.actions.calls == []
