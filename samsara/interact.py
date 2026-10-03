"""Pure, opt-in controller for one onset-bound window interaction sequence.

The application wires its ports in a later unit. No native or UI object is
constructed here, and an unwired controller cannot arm without visible feedback.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable

from samsara.subjects import InteractState, OnsetContext, SubjectRef
from samsara.window_targets import EffectResult, title_fingerprint


_SIDES = {"left", "right", "top", "bottom", "top left", "top right",
          "bottom left", "bottom right"}
_VERBS = {"focus", "minimize", "maximize", "close"}
_MONITOR_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}
IDLE_NS = 30_000_000_000
CHOICE_NS = 15_000_000_000
MAX_ONSET_LAG_NS = 100_000_000


@dataclass(frozen=True, slots=True)
class InteractReply:
    kind: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class CaptureView:
    session_epoch: int
    arm_epoch: int
    policy_generation: int
    subject_ref: SubjectRef | None


def parse_action(text: str) -> tuple[str, dict, bool] | None:
    """Whole-utterance v1 grammar; no fuzzy or named-target composition."""
    pointer = text.endswith(" this window")
    body = text[:-12] if pointer else text
    if body in _VERBS:
        return body, {}, pointer
    if body in ("left", "right"):
        return "snap", {"side": body}, pointer
    if body.startswith("snap ") and body[5:] in _SIDES:
        return "snap", {"side": body[5:]}, pointer
    match = re.fullmatch(r"move to monitor (\d+|one|two|three|four)", body)
    if match:
        raw = match.group(1)
        monitor = int(raw) if raw.isdigit() else _MONITOR_WORDS[raw]
        if monitor > 0:
            return "move", {"monitor": monitor}, pointer
    return None


class InteractController:
    """All transitions are serialized by the owning SessionModeManager lock."""

    def __init__(self, *, targets, effects, app, session_epoch: Callable[[], int],
                 get_mode: Callable[[], object], set_mode: Callable[[object], bool],
                 policy_generation: Callable[[], int], publish: Callable[[str], bool],
                 present_question: Callable[[str], int | None],
                 clock_ns: Callable[[], int] = time.monotonic_ns,
                 schedule: Callable[[float, Callable[[], None]], object] | None = None,
                 pending_probe: Callable[[], object] | None = None,
                 answer: Callable[[str], str | None] | None = None,
                 invoke: Callable[..., object] | None = None,
                 known_names: set[str] | None = None,
                 command_mode=None, switch_match: Callable[[str], bool] | None = None,
                 gate: Callable[[object], bool] | None = None,
                 transition_lock=None, normalize: Callable[[str], str] | None = None,
                 input_owned: Callable[[], bool] | None = None):
        self.targets, self.effects, self.app = targets, effects, app
        self._session_epoch, self._get_mode, self._set_mode = session_epoch, get_mode, set_mode
        self._policy_generation = policy_generation
        self._publish, self._present_question = publish, present_question
        self._clock_ns = clock_ns
        self._schedule = schedule or self._thread_timer
        self._pending_probe = pending_probe
        self._answer = answer
        self._invoke = invoke
        self._known_names = {name.casefold() for name in (known_names or set())}
        self._command_mode, self._switch_match, self._gate = command_mode, switch_match, gate
        self._transition_lock = transition_lock or threading.RLock()
        self._normalize = normalize or (lambda text: " ".join(text.casefold().strip(" .!?;:").split()))
        self._input_owned = input_owned or (lambda: False)
        self.state = InteractState()
        self.subject: SubjectRef | None = None
        self._revision = 0
        self._saved_mode = None
        self._choice_deadline_ns: int | None = None
        self._question_ready_ns: int | None = None
        self._approval_eligible = False
        self._last_effect = None
        self._timer = None
        self._view_lock = threading.Lock()
        self._view = CaptureView(0, 0, 0, None)
        self._publish_view()

    @staticmethod
    def _thread_timer(seconds, callback):
        timer = threading.Timer(seconds, callback)
        timer.daemon = True
        timer.start()
        return timer

    def capture_view(self) -> CaptureView:
        with self._view_lock:
            return self._view

    def session_epoch_changed(self) -> None:
        """Reset publishes a fresh OFF view after incrementing the epoch."""
        self._publish_view()

    def _publish_view(self) -> None:
        with self._view_lock:
            self._view = CaptureView(self._session_epoch(), self.state.arm_epoch,
                                     self._policy_generation(), self.subject)

    def _label(self) -> str:
        if self.state.phase == "CONFIRMING" and self.subject:
            return f"Close {self.subject.display_name}? Yes / No"
        if self.state.phase == "CHOOSING":
            choices = " · ".join(f"{number}. {ref.display_name}"
                                 for number, ref in enumerate(self.state.candidates, 1))
            return "Interact · " + choices
        return "Interact · " + (self.subject.display_name if self.subject else "choose a window")

    def _show(self) -> bool:
        try:
            return bool(self._publish(self._label()))
        except Exception:
            return False

    def _timer_cancel(self) -> None:
        old, self._timer = self._timer, None
        if old is not None and hasattr(old, "cancel"):
            old.cancel()

    def _deadline(self, now: int, *, refresh: bool = False) -> None:
        if refresh:
            self.state = replace(self.state, deadline_ns=now + IDLE_NS)
        self._timer_cancel()
        arm = self.state.arm_epoch
        end = min(self.state.deadline_ns,
                  self._choice_deadline_ns if self.state.phase == "CHOOSING" else self.state.deadline_ns)
        def callback():
            with self._transition_lock:
                self.expire(arm)
        self._timer = self._schedule(max(0.0, (end - now) / 1e9), callback)

    def expire(self, arm_epoch: int | None = None) -> bool:
        """Timer callback; stale callbacks cannot touch a later arm."""
        if self.state.phase == "OFF" or (arm_epoch is not None and arm_epoch != self.state.arm_epoch):
            return False
        now = self._clock_ns()
        if now >= self.state.deadline_ns or (self.state.phase == "CHOOSING"
                and now >= self._choice_deadline_ns):
            self.exit("expired")
            return True
        self._deadline(now)
        return False

    def _cancel_owned_pending(self, reason: str) -> None:
        if not self.state.pending_op_id or self._pending_probe is None:
            return
        try:
            op = self._pending_probe()
            if op is not None and op.op_id == self.state.pending_op_id:
                op.cancel(self.app, reason)
                from samsara.execution_policy import _release_slot
                _release_slot(op)
        except Exception:
            pass

    def exit(self, reason: str = "exit") -> InteractReply:
        if self.state.phase == "OFF":
            return InteractReply("interact_off")
        self._cancel_owned_pending(reason)
        self._timer_cancel()
        saved = self._saved_mode
        self.state = InteractState(arm_epoch=self.state.arm_epoch + 1)
        self.subject = None
        self._choice_deadline_ns = self._question_ready_ns = None
        self._approval_eligible = False
        self._saved_mode = None
        self._publish_view()
        if saved is not None:
            self._set_mode(saved)
        try:
            self._publish("")
        except Exception:
            pass
        return InteractReply("interact_exit", reason)

    def _fresh(self, onset: OnsetContext | None, *, armed: bool) -> bool:
        if not isinstance(onset, OnsetContext) or onset.session_epoch != self._session_epoch():
            return False
        if onset.policy_generation != self._policy_generation():
            return False
        if onset.observed_at_ns < onset.speech_start_ns or (
                onset.observed_at_ns - onset.speech_start_ns > MAX_ONSET_LAG_NS):
            return False
        return ((onset.arm_epoch == self.state.arm_epoch and onset.subject_ref is None)
                if not armed else (onset.arm_epoch == self.state.arm_epoch
                             and onset.subject_ref == self.subject
                             and onset.subject_revision == (self.subject.revision if self.subject else None)))

    def _bind(self, identity, title: str, origin: str, now: int) -> None:
        self._cancel_owned_pending("subject rebound")
        self._revision += 1
        self.subject = SubjectRef("window", identity, uuid.uuid4().hex,
                                  self._revision, self._session_epoch(), title[:80],
                                  title_fingerprint(title), origin, now)
        self.state = replace(self.state, phase="ARMED", candidates=(), pending_op_id=None)
        self._choice_deadline_ns = None
        self._publish_view()

    def _pointer_bind(self, onset: OnsetContext, now: int) -> bool:
        if onset.pointer_identity is None:
            return False
        try:
            current = self.targets.validate(onset.pointer_identity)
        except Exception:
            return False
        if not current.ok:
            return False
        self._bind(current.identity, current.title, "pointer", now)
        return True

    def _on_result(self, result) -> None:
        if not isinstance(result, EffectResult):
            return
        self._last_effect = result
        if result.ok and result.reason == "close_requested":
            self.subject = None
            self._revision += 1
            self._publish_view()
        elif result.ok and isinstance(result.subject_ref, SubjectRef):
            if self.subject == result.subject_ref:
                self._publish_view()

    def _context(self, now: int):
        from plugins.commands.interact import BoundContext
        ref = self.subject
        arm = self.state.arm_epoch
        return BoundContext(self.app, ref, ref.ref_id, arm, self._policy_generation(),
                            lambda token: self.subject if self.subject and self.subject.ref_id == token else None,
                            lambda: (self._session_epoch(), self.state.arm_epoch,
                                     self.subject.revision if self.subject else -1),
                            self.effects, self._on_result,
                            ttl_s=max(0.001, (self.state.deadline_ns - now) / 1e9),
                            confirmation_guard=lambda: self._approval_eligible)

    def _act(self, verb: str, kwargs: dict, onset: OnsetContext, now: int) -> InteractReply:
        if self.subject is None:
            return InteractReply("interact_refused", "Choose a window.")
        if not self._fresh(onset, armed=True):
            return InteractReply("interact_refused", "That capture is stale.")
        try:
            current = self.targets.validate(self.subject.identity)
        except Exception:
            current = None
        if current is None or not current.ok:
            self.subject = None
            self._revision += 1
            self._publish_view()
            if not self._show():
                self.exit("feedback unavailable")
            return InteractReply("interact_refused", "The selected window is gone.")
        if self._invoke is None:
            from plugins.commands.interact import invoke_bound
            invoke = invoke_bound
        else:
            invoke = self._invoke
        self._last_effect = None
        try:
            outcome = invoke(self._context(now), verb, **kwargs)
        except Exception:
            return InteractReply("interact_refused", "Action unavailable.")
        if verb == "close" and outcome.pending is not None:
            self.state = replace(self.state, phase="CONFIRMING", pending_op_id=outcome.pending.op_id)
            self._question_ready_ns = None
            self._publish_view()
            if not self._show():
                self._cancel_owned_pending("question unavailable")
                self.state = replace(self.state, phase="ARMED", pending_op_id=None)
                return InteractReply("interact_refused", "Confirmation unavailable.")
            try:
                presented = self._present_question(outcome.pending.prompt)
            except Exception:
                presented = None
            if presented is None:
                self._cancel_owned_pending("question unavailable")
                self.state = replace(self.state, phase="ARMED", pending_op_id=None)
                return InteractReply("interact_refused", "Confirmation unavailable.")
            self._question_ready_ns = max(now, presented)
            self._deadline(now, refresh=True)
            return InteractReply("interact_confirm", outcome.pending.prompt)
        effect = outcome.effect
        if not isinstance(effect, EffectResult) or not effect.ok:
            return InteractReply("interact_refused", getattr(effect, "reason", "Action refused."))
        self._deadline(now, refresh=True)
        if not self._show():
            self.exit("feedback unavailable")
        return InteractReply("interact_action", effect.reason)

    def _answer_close(self, text: str, onset: OnsetContext | None, signals, now: int) -> InteractReply:
        from samsara.execution_policy import classify_reply
        reply = classify_reply(text)
        if reply not in ("yes", "no"):
            return InteractReply("interact_refused", "Answer yes or no.")
        if (not self._fresh(onset, armed=True) or not self._gate(signals)
                or self._question_ready_ns is None
                or onset.speech_start_ns <= self._question_ready_ns):
            return InteractReply("interact_refused", "Answer after the question.")
        try:
            op = self._pending_probe() if self._pending_probe else None
        except Exception:
            op = None
        if op is None or op.op_id != self.state.pending_op_id:
            self.state = replace(self.state, phase="ARMED", pending_op_id=None)
            return InteractReply("interact_refused", "Confirmation expired.")
        self._approval_eligible = True
        try:
            result = self._answer(text)
        except Exception:
            result = "refused:answer unavailable"
        finally:
            self._approval_eligible = False
        if result not in ("approved", "rejected"):
            self._cancel_owned_pending("answer refused")
        self.state = replace(self.state, phase="ARMED", pending_op_id=None)
        self._question_ready_ns = None
        self._publish_view()
        self._deadline(now, refresh=True)
        if not self._show():
            self.exit("feedback unavailable")
        if reply == "yes" and result == "approved":
            if self._last_effect is None or not self._last_effect.ok:
                return InteractReply("interact_refused", getattr(self._last_effect, "reason", "No effect observed."))
            return InteractReply("interact_reply", self._last_effect.reason)
        return InteractReply("interact_reply", str(result))

    def handle(self, text: str, signals, onset: OnsetContext | None) -> InteractReply | None:
        """Return None only when OFF or for an explicit legacy lane switch."""
        normalized = self._normalize(text or "")
        if self.state.phase == "OFF":
            if normalized != "interact":
                return None
            if not self._gate(signals) or not self._fresh(onset, armed=False):
                return InteractReply("interact_refused", "Interact needs a fresh capture.")
            try:
                occupied = self._input_owned() or (self._pending_probe and self._pending_probe() is not None)
            except Exception:
                occupied = True
            if occupied:
                return InteractReply("interact_refused", "Finish or cancel the current action.")
            now = self._clock_ns()
            previous = self._get_mode()
            if not self._set_mode(self._command_mode):
                return InteractReply("interact_refused", "Command capture unavailable.")
            self._saved_mode = previous
            self.state = InteractState("ARMED", self.state.arm_epoch + 1, now + IDLE_NS)
            self._revision = 0
            if onset.pointer_identity is not None:
                self._pointer_bind(onset, now)
            self._publish_view()
            if not self._show():
                self.exit("feedback unavailable")
                return InteractReply("interact_refused", "Interact feedback unavailable.")
            self._deadline(now)
            return InteractReply("interact_armed", self._label())
        now = self._clock_ns()
        if self.expire(self.state.arm_epoch):
            return InteractReply("interact_expired", "Interact timed out.")
        if normalized in ("exit interact", "cancel interact"):
            return self.exit()
        if normalized == "clear subject":
            self._cancel_owned_pending("subject cleared")
            self.subject = None
            self._revision += 1
            self.state = replace(self.state, phase="ARMED", candidates=(), pending_op_id=None)
            self._choice_deadline_ns = self._question_ready_ns = None
            self._publish_view()
            if not self._show():
                self.exit("feedback unavailable")
                return InteractReply("interact_refused", "Interact feedback unavailable.")
            self._deadline(now, refresh=True)
            return InteractReply("interact_cleared")
        if self._switch_match is not None and self._switch_match(text):
            if not self._gate(signals):
                return InteractReply("interact_refused", "That switch was not clear.")
            self.exit("lane switch")
            return None
        if self.state.phase == "CONFIRMING":
            return self._answer_close(text, onset, signals, now)
        if self.state.phase == "CHOOSING":
            match = re.fullmatch(r"window (\d+)", normalized)
            if not match:
                return InteractReply("interact_refused", "Choose a window number.")
            index = int(match.group(1)) - 1
            if not self._gate(signals) or not self._fresh(onset, armed=True):
                return InteractReply("interact_refused", "That capture is stale.")
            if index < 0 or index >= len(self.state.candidates):
                return InteractReply("interact_refused", "Choose a listed window.")
            selected = self.state.candidates[index]
            try:
                current = self.targets.validate(selected.identity)
            except Exception:
                current = None
            if current is None or not current.ok:
                return InteractReply("interact_refused", "That window is gone.")
            self._bind(current.identity, current.title, "name", now)
            self._deadline(now, refresh=True)
            if not self._show():
                self.exit("feedback unavailable")
                return InteractReply("interact_refused", "Interact feedback unavailable.")
            return InteractReply("interact_selected", self._label())
        if not self._gate(signals) or not self._fresh(onset, armed=True):
            return InteractReply("interact_refused", "That capture is stale.")
        if normalized == "this window":
            if not self._pointer_bind(onset, now):
                return InteractReply("interact_refused", "Cannot select that window.")
            self._deadline(now, refresh=True)
            if not self._show():
                self.exit("feedback unavailable")
                return InteractReply("interact_refused", "Interact feedback unavailable.")
            return InteractReply("interact_selected", self._label())
        action = parse_action(normalized)
        if action is not None:
            verb, kwargs, pointer = action
            if pointer and not self._pointer_bind(onset, now):
                return InteractReply("interact_refused", "Cannot select that window.")
            # An explicit pointer rebind creates a new revision; use it for the
            # invocation after validating the old onset above.
            if pointer:
                onset = replace(onset, subject_ref=self.subject,
                                subject_revision=self.subject.revision)
            return self._act(verb, kwargs, onset, now)
        name = normalized.removeprefix("select window ") if normalized.startswith("select window ") else normalized
        if normalized.startswith("select window ") or name in self._known_names:
            try:
                found = self.targets.enumerate_named(name)
            except Exception:
                return InteractReply("interact_refused", "Window search unavailable.")
            if found.reason or not found.candidates:
                return InteractReply("interact_refused", "No eligible matching window.")
            if len(found.candidates) == 1:
                only = found.candidates[0]
                self._bind(only.identity, only.title, "name", now)
                self._deadline(now, refresh=True)
                if not self._show():
                    self.exit("feedback unavailable")
                    return InteractReply("interact_refused", "Interact feedback unavailable.")
                return InteractReply("interact_selected", self._label())
            refs = tuple(SubjectRef("window", item.identity, uuid.uuid4().hex,
                                    self._revision + 1, self._session_epoch(), item.title[:80],
                                    title_fingerprint(item.title), "name", now)
                         for item in found.candidates)
            self.state = replace(self.state, phase="CHOOSING", candidates=refs)
            self._choice_deadline_ns = min(now + CHOICE_NS, self.state.deadline_ns)
            self._publish_view()
            self._deadline(now)
            if not self._show():
                self.exit("feedback unavailable")
                return InteractReply("interact_refused", "Interact feedback unavailable.")
            return InteractReply("interact_choosing", self._label())
        return InteractReply("interact_unknown", "Say a window action or exit interact.")
