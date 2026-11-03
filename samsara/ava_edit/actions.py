"""Typed stage-only adapter, shared by terse commands and validated model actions.

Block 4 owns wire parsing and router dispatch. It must carry EditTurn.snapshot
with the queued request, invoke from_action with that snapshot (never recapture),
and call release on non-edit/cancelled turns. No source enters the router string.
"""
from __future__ import annotations

from samsara.ava_edit import intent, targets


class EditTurn(str):
    def __new__(cls, text, snapshot, utterance):
        obj = super().__new__(cls, text)
        obj.snapshot, obj.utterance = snapshot, utterance
        return obj

    def release(self):
        self.snapshot.release()


def ingress_payload(app, text, context=''):
    # Do not put a source passage in the conversation prompt. Context previously
    # contained the draft; the shared editor resolves it locally when needed.
    return EditTurn(text, targets.snapshot(app), text)


def from_utterance(app, text, *, snapshot=None):
    target = intent.edit_target(text)
    # The existing DICTATE cleanup exception and draft buttons address the
    # pending draft, even when the foreground selection cannot be inspected.
    from samsara.session_modes import SessionMode
    manager = getattr(app, '_session_mode_manager', None)
    if (target == 'auto' and getattr(manager, 'mode', None) is SessionMode.DICTATE
            and getattr(manager, 'dictate_pending_buffer', '')
            and intent.draft_cleanup_scope(text) is not None):
        target = 'draft'
    captured = snapshot or targets.snapshot(app, selection_needed=target in {'auto', 'selection'})
    return targets.EditRequest(target, text, captured, text)


def from_action(app, args, *, snapshot, original_utterance):
    """Accept only already-parsed args; no model-supplied source or authority."""
    if not isinstance(args, dict) or set(args) != {'target', 'instruction'}:
        raise ValueError('invalid edit arguments')
    explicit = intent.explicit_target(original_utterance)
    if explicit is not None and args['target'] != explicit:
        raise ValueError('contradictory edit target')
    if snapshot is None:
        raise ValueError('missing request snapshot')
    return targets.EditRequest(args['target'], args['instruction'], snapshot, original_utterance)


def propose_and_stage(app, request):
    from samsara.ava_edit import session, selection, model
    captured = request.snapshot
    bound, reason = targets.bind(request)
    if (bound is None or not targets.request_valid(app, captured)
            or not targets.source_valid(app, bound)):
        # PLACEHOLDER COPY (1036b): reuse the existing refusal presentation.
        selection._notice(app, reason or 'the text changed')
        captured.release()
        return False
    cleanup, instruction = intent.cleanup_instruction(request.instruction)
    if len(bound.text) > model.MAX_SOURCE_CHARS:
        selection._notice(app, 'that text is too long to edit')
        captured.release()
        return False
    if not cleanup and not session.enabled(app):
        selection._notice(app, 'This edit needs Ava. Turn Ava on and choose a model first.')
        captured.release()
        return False
    with session._lock:
        if session.pending() is not None:
            captured.release()
            return False
        # Reserve worker authority. A later worker, clear or discard invalidates it.
        app._ava_edit_epoch = getattr(app, '_ava_edit_epoch', 0) + 1
        captured.epoch = app._ava_edit_epoch
        session._worker_app = app
    session._chip(app, 'edit_thinking')
    if not targets.request_valid(app, captured) or not targets.source_valid(app, bound):
        captured.release()
        return False
    proposal = session.propose(bound.text, instruction, app,
        offline=cleanup and not session.enabled(app), cleanup=cleanup,
        before_submit=lambda: (targets.request_valid(app, captured)
                               and targets.source_valid(app, bound)))
    with session._lock:
        if (not targets.request_valid(app, captured) or not targets.source_valid(app, bound)
                or session.pending() is not None):
            captured.release()
            return False
        draft_target = None
        if bound.kind == 'draft':
            draft_target = (bound.manager, bound.document_id, bound.revision,
                            bound.full_text, bound.start, bound.end)
        return session.stage(app, proposal,
            selection_target=bound.selection.target if bound.kind == 'selection' else None,
            draft_target=draft_target, bound_target=bound, request=request)
