"""Shared liveness and dismissal for command-suggestion feedback."""
def bind_pending_reply(manager, reply):
    def answer(text):
        alive = getattr(manager, "_display_suggestion_alive", None)
        if alive is not None:
            alive()
        suggestion = manager._pending_command_suggestion
        result = reply(text) if reply is not None else None
        if alive is not None:
            alive()
        if result in ("approved", "rejected"):
            if manager._pending_command_suggestion is suggestion:
                manager._pending_command_suggestion = None
        return result

    return answer


def suggestion_callbacks(app, manager, suggestion, policy, op):
    mode = getattr(manager, "mode", None)
    epoch = getattr(manager, "_session_epoch", None)
    generation = policy.current_generation(app) if policy else None

    def retire():
        if suggestion is not None and manager._pending_command_suggestion is suggestion:
            manager._pending_command_suggestion = None

    def alive():
        same_session = (getattr(manager, "mode", None) == mode
                        and getattr(manager, "_session_epoch", None) == epoch)
        live = same_session
        if op is not None:
            current = policy.pending_operation() is op
            if current and not same_session:
                op.cancel(app, "session changed", chip=False)
            live = live and current and getattr(
                op, "is_live", lambda _: op.approved is None)(app)
        elif policy is not None:
            live = (live and policy.current_generation(app) == generation
                    and manager._clock() <= suggestion["expires"])
        else:
            live = live and manager._clock() <= suggestion["expires"]
        if suggestion is not None:
            live = live and manager._pending_command_suggestion is suggestion
        if not live:
            retire()
        return live

    def dismiss():
        if not alive():
            return False
        if op is not None:
            if policy.answer_pending(app, "no", source=policy.ANSWER_DIALOG) != "rejected":
                return False
        retire()
        return True

    if suggestion is not None:
        manager._display_suggestion_alive = alive
    return alive, dismiss
