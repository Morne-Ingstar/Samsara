"""Optional pause-bounded delivery through the existing staged commit transaction."""
from copy import copy
from collections import deque

from samsara.draft_document import DraftDocument
from samsara.session_modes import (DispatchOutcome, InjectionDelivery, UnitOfWorkStack,
                                   chunk_ends_terminal, seam_join)


class HandsFreeDelivery:
    def __init__(self, app):
        self.app = app
        self.previous = None
        self.target = None
        self.last_item = None
        self.stack = None

    def enabled(self, manager):
        enabled = (self.app.config.get('hands_free') or {}).get('send_mode', 'staged') == 'as_i_speak'
        if manager._stack is not self.stack:
            self.previous = self.target = self.last_item = None
            self.stack = manager._stack
        item = manager._stack.peek()
        if not enabled:
            self.previous = self.target = None
        elif item is not self.last_item:
            # After a scratch or an explicit draft send, use the surviving
            # confirmed output as the seam, never text that was only staged.
            self.previous = item.payload if item is not None and item.kind == 'dictation_chunk' else None
            self.target = (item.extra.get('target_process'), item.extra.get('hwnd')) if self.previous else None
            self.last_item = item
        return enabled

    def deliver(self, manager, text, audio_ref=None):
        target = (manager._foreground_exe_resolver(), manager._foreground_hwnd_resolver())
        clipboard_only = not self.app.config.get('auto_paste', True)
        previous = self.previous if target == self.target and not clipboard_only else None
        body = seam_join(chunk_ends_terminal(previous), text) if previous else text.strip()
        prefix = ' ' if previous and not previous[-1].isspace() else ''
        # Formatting may strip leading whitespace; the host adds this at the
        # last seam before paste, then records those exact bytes for undo.
        manager._sentence_prefix = prefix
        manager._sentence_clipboard = clipboard_only
        # A separate transaction reuses the guarded commit without mutating
        # an earlier waiting document, its revision, audio, or recovery slot.
        transaction = copy(manager)
        transaction._draft = DraftDocument(clock=manager._clock)
        transaction._stack = UnitOfWorkStack()
        transaction._foreground_exe_resolver = self._chosen_target(target[0], manager._foreground_exe_resolver)
        transaction._foreground_hwnd_resolver = self._chosen_target(target[1], manager._foreground_hwnd_resolver)
        transaction._dictate_pending_buffer = body
        transaction._dictate_pending_audio = [] if audio_ref is None else [audio_ref]
        transaction._parked_hold = False
        transaction._commit_redecode_fn = None
        # Tutorial installs an instance closure bound to the original manager.
        # Invoke the existing class commit on this transaction instead; tutorial
        # captures have already taken their separate dispatch branch.
        try:
            outcome = (self._copy_sentence(transaction, body) if clipboard_only else
                       type(manager)._commit_dictate_buffer(transaction, target_mode=None))
        except Exception:
            outcome = DispatchOutcome('dictate_commit_failed', {'error': 'Could not send'})
        finally:
            manager._sentence_prefix = ''
            manager._sentence_clipboard = False
        if outcome.kind == 'dictate_committed':
            self.previous = None if clipboard_only else outcome.detail.get('text')
            self.target = target
            if clipboard_only or outcome.detail.get('delivery_unverified'):
                # No older undo can safely backspace past this unconfirmed send.
                self._disarm_external_undo(manager)
            elif transaction._stack.peek() is not None:
                manager._stack.push(transaction._stack.peek())
            manager._stage_buffer = transaction._stage_buffer
            self.last_item = manager._stack.peek()
            outcome.detail['immediate'] = True
            return outcome
        # Failed/refused words are visible in History only, never armed for
        # retype, auto-retry, session-exit parking, or a later paste word.
        self._disarm_external_undo(manager)
        self.last_item = manager._stack.peek()
        self.app.add_to_history(text.strip(), is_command=False)
        self.app._log_history(raw_text=text, display_text=text.strip(), mode='dictate',
                              status='failed' if outcome.kind == 'dictate_commit_failed' else 'refused',
                              entry_type='dictation')
        outcome.detail['saved_to_history'] = True
        outcome.detail['immediate'] = True
        return outcome

    @staticmethod
    def _chosen_target(value, resolver):
        # Commit and sentence spacing use one destination; every subsequent
        # focus check still reads the real foreground, including before paste.
        chosen = False

        def resolve():
            nonlocal chosen
            if not chosen:
                chosen = True
                return value
            return resolver()

        return resolve

    @staticmethod
    def _disarm_external_undo(manager):
        manager._stack._items = deque(
            (item for item in manager._stack._items if item.kind == 'dictation_staged_chunk'),
            maxlen=manager._stack.MAX_SIZE)

    def scratch_ready(self, manager):
        item = manager._stack.peek()
        return item is not None and item.kind == 'dictation_chunk'

    @staticmethod
    def _copy_sentence(manager, body):
        delivered = manager._inject_fn(body)
        if delivered is False:
            return DispatchOutcome('dictate_commit_failed', {'error': 'Could not copy'})
        actual = delivered.text if isinstance(delivered, InjectionDelivery) else delivered
        actual = actual if isinstance(actual, str) else body
        manager._dictate_pending_buffer = ''
        manager._dictate_pending_audio = []
        manager._last_dictate_ended_terminal = None
        manager._stage_buffer = actual
        return DispatchOutcome('dictate_committed', {'text': actual, 'copied_to_clipboard': True})


def finish_sentence_display(app, outcome):
    """Show a sent sentence after draft sync, respecting an overlapping hold."""
    if not outcome.detail.get('immediate'):
        return
    controller = getattr(app, 'live_surface', None)
    text = outcome.detail.get('text', '').strip()
    if not text:
        return
    if controller is not None:
        from samsara.live_surface.model import EventKind, Lane, NoticeKind, SurfaceEvent
        if getattr(controller, '_active_lane', None) is not Lane.HOLD:
            waiting = getattr(app, '_session_mode_manager', None)
            waiting = getattr(waiting, 'dictate_pending_buffer', '')
            if not waiting and not outcome.detail.get('copied_to_clipboard'):
                controller.post(SurfaceEvent(EventKind.DELIVERY_FINISHED, text=text))
            label = ('Copied' if outcome.detail.get('copied_to_clipboard') else
                     'Sent, unconfirmed' if outcome.detail.get('delivery_unverified') else 'Typed')
            kind = NoticeKind.WARNING if outcome.detail.get('delivery_unverified') else NoticeKind.RESULT
            controller.post(SurfaceEvent(EventKind.NOTICE, notice=kind, message=label + '\n' + text, ttl_s=.9))
    elif text:
        label = ('copied: ' if outcome.detail.get('copied_to_clipboard') else
                 'sent, unconfirmed: ' if outcome.detail.get('delivery_unverified') else '') + text
        app._show_outcome_chip(label, 'warning' if outcome.detail.get('delivery_unverified') else 'success', 900)
