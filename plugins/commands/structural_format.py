"""Whole-utterance voice commands for deterministic structural formatting."""
from samsara import delivery_verify, draft_commands, structural_format, text_selection
from samsara.command_registry import DispatchResult, DispatchState
from samsara.plugin_commands import command


def _register_format(phrase, transform, aliases, label):
    def handle(app, _remainder):
        manager = getattr(app, '_session_mode_manager', None)
        if manager is None and callable(ensure := getattr(app, '_ensure_session_mode_manager', None)):
            manager = ensure()
        if manager is not None:
            with manager._dispatch_lock:
                proof = delivery_verify.snapshot()
                if manager.draft_document.text and (not proof or proof[0][3] == proof[0][4]):
                    applied = draft_commands.format_draft(manager, transform)
                else:
                    applied = text_selection.format_external(manager, proof, transform)
                if applied:
                    manager.cancel_word_correction('formatted')
                    return DispatchResult(DispatchState.COMPLETED, detail={'owns_undo': True})
        message = 'Select some text or dictate first.'
        if callable(show := getattr(app, '_show_outcome_chip', None)):
            show(message, 'warning', 3500)
        return DispatchResult(DispatchState.FAILED, detail={'reason': message, 'chain_feedback_sent': True})

    handle.__qualname__ = 'format_' + transform.__name__
    handle.__doc__ = f'{label} using the selection or verified last dictation. Say scratch that to undo.'
    command(phrase, aliases=aliases, pack='core', whole_utterance=True, risk_class='write', ai_visible=False,
            ai_composable=False, side_effects=['ui'], preconditions=[], voice_triggerable=True,
            param_schema={}, reversible=True, preview_template='')(handle)


for _phrase, _spec in structural_format.COMMANDS.items():
    _register_format(_phrase, *_spec)
