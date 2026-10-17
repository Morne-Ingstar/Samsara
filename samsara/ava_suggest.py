"""Suggestion-only gate for model interpretations of command misses."""
from samsara import execution_policy
from samsara.execution_policy import Route


def suggest_command_miss(app, command_id, *, generation, args=None, source_text=""):
    """Offer one registry command; only a user reply can cause its effect.

    This seam is for COMMAND misses, not conversational/advice responses.
    Reuse the policy's single pending slot, frozen arguments and target guard.
    """
    import copy
    from plugins.commands import ask_ollama as ava
    from samsara.command_registry import DispatchResult, DispatchState
    executor = app.command_executor

    def miss():
        return DispatchResult(DispatchState.MISS, source_text)

    if (not ava._ai_state(app).allowed
            or not (app.config.get("ava_command_session", {}) or {}).get("interpret_misses", True)
            or ava.get_pending_action() is not None):
        return miss()
    entry = executor._registry_entry(command_id)
    if entry is None:
        return miss()
    inv = execution_policy.Invocation(entry.phrase, copy.deepcopy(dict(args or {})),
                                      Route.MODEL, generation, source_text=source_text)
    if set(inv.args) - {"remainder"}:
        return miss()  # This resolver offers only a phrase and its spoken tail.
    decision = execution_policy.authorize(inv, app=app, executor=executor, quiet=True)
    if isinstance(decision, execution_policy.Denied):
        return miss()
    target_inv = inv
    if entry.phrase in {"focus", "close"} and inv.args.get("remainder"):
        target_inv = execution_policy.Invocation(entry.phrase, {"target": inv.args["remainder"]})
    targets, target_probe = execution_policy.bind_target(target_inv)

    def probe():
        return {"foreground": execution_policy.foreground_window_identity(),
                "named": target_probe() if target_probe else targets}

    bound = probe()
    if not bound["foreground"] or (targets or {}).get("gone"):
        return miss()
    serial = getattr(executor, "_grammar_request_serial", 0)
    request_id = getattr(app, "_ava_interpret_request", None)

    def eligible():
        from samsara import ava_command_session
        return (ava._ai_state(app).allowed
                and (app.config.get("ava_command_session", {}) or {}).get("interpret_misses", True)
                and execution_policy.is_current(app, generation)
                and getattr(executor, "_grammar_request_serial", 0) == serial
                and getattr(app, "_ava_interpret_request", None) == request_id
                and not ava_command_session.is_cancelled(str(request_id)))

    # Registry phrase and validated slots only; never the model's CONFIRM line.
    phrase = entry.phrase
    remainder = str(inv.args.get("remainder", "") or "")
    label = " ".join(f"{phrase} {remainder}".split())
    # Stand-in copy (608); the common outcome chip renders on Compact and Full.
    prompt = f"Did you mean '{label}'? Say yes."

    def approve(op):
        from samsara.ava_action_feedback import execute_action, speak_result
        result = execute_action(executor, inv.command_id, app, route=Route.MODEL,
                                generation=inv.generation, args=copy.deepcopy(inv.args),
                                source_text=inv.source_text, confirmed=True)
        if result.state is DispatchState.COMPLETED:
            app._command_mode_miss_count = 0
            speak_result(app, result.detail.get("message") or f"{label} done.",
                         {"state": "completed"}, inv.command_id)

    if not eligible() or probe() != bound or ava.get_pending_action() is not None:
        return miss()
    execution_policy.stage_bound_pending(
        app, inv, prompt, targets=bound, target_probe=probe,
        current_invocation=lambda: inv, reply_guard=eligible, on_approve=approve)
    ava.speak(app, prompt, category="confirmation")
    return DispatchResult(DispatchState.QUEUED, source_text, label,
                          {"awaiting_confirmation": True, "prompt": prompt})
