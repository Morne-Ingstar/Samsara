"""Inert registry identities and the controller-only bound Interact entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from samsara import execution_policy as policy
from samsara.command_registry import DispatchState
from samsara.plugin_commands import command
from samsara.subjects import SubjectRef
from samsara.window_targets import BoundWindowEffects, EffectResult, title_fingerprint

_SCHEMA = {"subject_token": {"type": "str", "required": True}}
_META = dict(pack="core", ai_visible=False, ai_composable=False,
             voice_triggerable=False, reversible=False, side_effects=["ui"])


@command("interact focus", risk_class="ui", param_schema=_SCHEMA, **_META)
def focus(_app, _remainder=""):
    return DispatchState.FAILED


@command("interact minimize", risk_class="ui", param_schema=_SCHEMA, **_META)
def minimize(_app, _remainder=""):
    return DispatchState.FAILED


@command("interact maximize", risk_class="ui", param_schema=_SCHEMA, **_META)
def maximize(_app, _remainder=""):
    return DispatchState.FAILED


@command("interact move", risk_class="ui", param_schema={
    **_SCHEMA, "monitor": {"type": "int", "min": 1, "required": True}}, **_META)
def move(_app, _remainder=""):
    return DispatchState.FAILED


@command("interact snap", risk_class="ui", param_schema={
    **_SCHEMA, "side": {"type": "str", "required": True, "choices": [
        "left", "right", "top", "bottom", "top left", "top right",
        "bottom left", "bottom right"]}}, **_META)
def snap(_app, _remainder=""):
    return DispatchState.FAILED


@command("interact close", risk_class="destructive", param_schema=_SCHEMA,
         preview_template="Close the bound window?", **_META)
def close(_app, _remainder=""):
    return DispatchState.FAILED


@dataclass(frozen=True, slots=True)
class BoundContext:
    """Controller-issued token plus live epoch/revision probe; never spoken."""

    app: object
    subject: SubjectRef
    subject_token: str
    arm_epoch: int
    generation: int
    lookup: Callable[[str], SubjectRef | None]
    state_probe: Callable[[], tuple[int, int, int]]
    effects: BoundWindowEffects
    on_result: Callable[[EffectResult], None] | None = None
    ttl_s: float = 30.0
    confirmation_guard: Callable[[], bool] | None = None

    def live(self) -> bool:
        try:
            return (bool(self.subject_token)
                    and self.lookup(self.subject_token) == self.subject
                    and self.state_probe() == (self.subject.session_epoch,
                                               self.arm_epoch, self.subject.revision)
                    and policy.is_fresh(self.app, self.generation))
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class BoundOutcome:
    decision: object
    effect: EffectResult | None = None
    pending: policy.PendingOperation | None = None


def invoke_bound(context: BoundContext, verb: str, *, side: str | None = None,
                 monitor: int | None = None) -> BoundOutcome:
    """Controller calls this directly; voice/model handlers cannot mint context."""
    if not isinstance(context, BoundContext) or not context.live():
        return BoundOutcome(policy.Denied("missing_controller_context"))
    if verb not in {"focus", "minimize", "maximize", "move", "snap", "close"}:
        return BoundOutcome(policy.Denied("invalid_verb"))
    ref = context.subject
    current = context.effects.targets.validate(ref.identity)
    if not current.ok:
        return BoundOutcome(policy.Denied("target_changed"))
    args = {"subject_token": context.subject_token}
    if verb == "move":
        args["monitor"] = monitor
    if verb == "snap":
        args["side"] = side
    invocation = policy.Invocation("interact " + verb, args, policy.Route.EXACT,
                                   context.generation)
    decision = policy.authorize(invocation, app=context.app)
    if isinstance(decision, policy.Denied):
        return BoundOutcome(decision)
    ready = context.live
    if verb == "close":
        if not isinstance(decision, policy.NeedsConfirmation):
            return BoundOutcome(policy.Denied("close_requires_confirmation"))
        if context.confirmation_guard is None:
            return BoundOutcome(policy.Denied("missing_reply_guard"))
        close_hash = title_fingerprint(current.title)
        target = {"ref_id": ref.ref_id, "session_epoch": ref.session_epoch,
                  "revision": ref.revision, "hwnd": ref.identity.hwnd,
                  "pid": ref.identity.pid, "process": ref.identity.process_name,
                  "started": ref.identity.process_start_time,
                  "class": ref.identity.window_class, "title_hash": close_hash}

        def probe():
            return target if ready() and context.effects.targets.validate(
                ref.identity, title_hash=close_hash).ok else {"gone": "the bound window"}

        def approved(_op):
            result = context.effects.execute(ref, "close", guard=ready, title_hash=close_hash)
            if context.on_result is not None:
                context.on_result(result)

        label = " ".join(ref.display_name.split())[:80] or "the bound window"
        pending = policy.stage_bound_pending(
            context.app, invocation, f"Close {label}? Say yes or no.", targets=target,
            target_probe=probe, current_invocation=lambda: invocation,
            reply_guard=lambda: ready() and bool(context.confirmation_guard()),
            on_approve=approved, ttl_s=min(30.0, context.ttl_s))
        return BoundOutcome(decision, pending=pending)
    if not isinstance(decision, policy.Allowed):
        return BoundOutcome(decision)
    result = context.effects.execute(ref, verb, guard=ready, side=side, monitor=monitor)
    if context.on_result is not None:
        context.on_result(result)
    return BoundOutcome(decision, effect=result)
