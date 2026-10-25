"""Result wording shared by the two listening displays."""
CHAIN_CONNECTOR = " · "
AVA_KEY_OFF = "Ava is off. Turn her on in Settings > Ava."  # stand-in
CLEAR_RESTORE_PHRASES = ("bring it back", "undo clear", "put it back", "restore the draft")
AVA_INTERPRET_HELP_LONG = (
    "You can ask in your own words, such as 'make the music quieter'. "
    "If Samsara does not recognise a command, Ava tries to find an available command "
    "that fits what you asked. She can only use commands Samsara already has, "
    "and still asks for your confirmation when a command needs it. "
    "Turn this off if you want Samsara to accept only commands it recognises directly. "
    "It is on by default."
)


def is_chain_result(label):
    return str(label).startswith(("Done: ", "Completed (Ava): "))


def is_ava_chain_result(label):
    return str(label).startswith("Completed (Ava): ")


def spoken_result(label):
    """Use pause punctuation for the visible separator, never a connector word."""
    return label.replace(CHAIN_CONNECTOR, ", ") if is_chain_result(label) else label


def report_held_command(app, dispatch, heard):
    """Finish command-key feedback without replacing a plugin's own reply."""
    app._notify_tutorial('command', dispatch.result or '')
    if getattr(getattr(dispatch, 'state', None), 'value', None) != 'completed':
        return
    detail = dict(dispatch.detail or {})
    # Chains and plugins may already have resolved this hold's pending chip.
    # Teaching also owns a separate question/caption; it must keep that reply.
    seq = getattr(app, '_hold_chip_seq', 0)
    if (detail.get('chain_feedback_sent') or getattr(app, '_ava_question', None)
            or (seq and getattr(app, '_hold_chip_resolved_seq', 0) >= seq)):
        return
    from samsara.session_modes import DispatchOutcome
    detail.update(phrase=dispatch.phrase or dispatch.result, state='completed', heard=heard)
    app._show_dispatch_outcome_chip(DispatchOutcome(kind='command_executed', detail=detail))
