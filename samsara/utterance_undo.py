"""Scratch transactions and grouped redo for the hands-free session.

The manager owns routing and the draft; this mixin owns its undo transaction.
Editor mutation remains behind the verified receipts supplied by paste.py.
"""
from collections import deque
from copy import deepcopy
import logging

log = logging.getLogger("Samsara.session_modes")


class UtteranceUndoMixin:
    def _do_scratch_that_n(self, count: int) -> dict:
        """N scratches as ONE action. Returns
        {"requested", "scratched", "reason"} with reason in
        "done" | "empty" | "failed".

        Every pop is _do_scratch_that() -- the same focus checks, verified
        range replacement and buffer arithmetic. What this adds is counting,
        and two things a repeated single scratch cannot give you:

          * ATOMICITY. The whole count is ONE dispatch: one DispatchOutcome,
            one chip, one earcon. Nothing downstream sees N events, and the
            run either reaches the count or stops and says where it stopped.
          * HONESTY ABOUT THE SHORTFALL. The stack holds
            UnitOfWorkStack.MAX_SIZE, so "scratch that ten times" cannot do
            ten. It pops what exists and says so; it never claims the count.

        Recovery uses a separate redo transaction (606), never queue 84's
        prepend-style recovery slot. It restores only the successfully removed
        units and requires the unchanged staged revision or editor receipt.

        PARTIAL FAILURE stops the run at the first refusal rather than
        pressing on. _do_scratch_that() returns False for a reason -- the
        focus moved, the window is not the one that received the text, the
        buffer no longer ends with the chunk -- and every one of those reasons
        applies just as much to the next pop. Stopping leaves a draft the user
        can see, under a chip that says how far it got, instead of pressing on
        into a window that is no longer theirs.
        """
        before = self._scratch_state()
        removed_units = []
        requested = max(1, int(count))
        scratched = 0
        reason = "done"
        for _ in range(requested):
            if not self._stack:
                reason = "empty"
                break
            item = self._stack.peek()
            if not self._do_scratch_that():
                reason = "failed"
                break
            scratched += 1
            removed_units.append(item.extra.get('undo_unit'))
        if scratched:
            self._last_scratch_at = self._clock()
            # Local staged edits need no editor receipt. Mixed legacy external
            # operations cannot promise a reversible transaction.
            local = all(item.kind == 'dictation_staged_chunk'
                        for item in before['items'][-scratched:])
            if local or all(unit is not None for unit in removed_units):
                self._scratch_redo = (before, self._scratch_state(), removed_units if not local else [])
        log.info("[SESSION] counted scratch: asked for %d, scratched %d (%s)",
                 requested, scratched, reason)
        return {"requested": requested, "scratched": scratched, "reason": reason}

    def _scratch_state(self):
        return {'draft': deepcopy({key: value for key, value in self._draft.__dict__.items()
                                   if key != '_clock'}),
                'items': list(self._stack._items),
                'audio': list(self._dictate_pending_audio),
                'ended': self._last_dictate_ended_terminal,
                'stage': self._stage_buffer,
                'epoch': self._session_epoch}

    def _redo_scratch_outcome(self):
        from samsara.session_modes import DispatchOutcome

        transaction = getattr(self, '_scratch_redo', None)
        ok = False
        restored = 0
        if transaction is not None:
            before, after, units = transaction
            # Refuse stale local revisions even when the words look identical.
            current = self._scratch_state()
            if current == after:
                ok = True
                previous = None
                for unit in reversed(units):
                    if previous is not None:
                        if previous.expected is None or previous.expected[:5] != unit.before[:5]:
                            ok = False
                            break
                        unit.expected = previous.expected
                    if not unit.change(redo=True):
                        ok = False
                        break
                    restored += 1
                    original = next(item for item in before['items']
                                    if item.extra.get('undo_unit') is unit)
                    self._stack.push(original)
                    previous = unit
                if ok:
                    revision = self._draft.revision + 1
                    self._draft.__dict__.update(deepcopy(before['draft']))
                    self._draft.revision = revision
                    self._stack._items = deque(before['items'], maxlen=self._stack.MAX_SIZE)
                    self._dictate_pending_audio = list(before['audio'])
                    self._last_dictate_ended_terminal = before['ended']
                    self._stage_buffer = before['stage']
            # One attempt only: a partially restored external run must never
            # replay earlier units into an uncertain target.
            self._scratch_redo = None
        if self._on_scratch_result:
            self._on_scratch_result(ok)
        message = ('Redone' if ok else f'Redo stopped after {restored}' if restored
                   else 'Cannot redo safely here')
        return DispatchOutcome('scratch_success' if ok else 'scratch_refuse', {'message': message})


def remove_legacy_chars(n: int, *, injection_safety, foreground_hwnd, logger) -> bool:
    # Queue 50 (ARC audit5b): was n x pyautogui.hotkey('shift','left')
    # + press('delete'). Measured (reports/50): pyautogui sends arrows
    # with scan code 0 and no extended-key flag and sleeps PAUSE=0.1 s
    # after every call, so 40 characters took 4.1 s and -- on a plain
    # text control and on a target that samples Shift late -- selected
    # nothing or deleted the wrong character. Paced Backspace carries
    # no modifier state and re-checks the target window between
    # batches: 40 characters in 0.14 s, exact on all three targets.
    verdict = injection_safety.window_integrity()
    if verdict.blocked:
        logger.warning('[SESSION] scratch-that refused: foreground window is elevated (%s)',
                       verdict.describe())
        return False
    start_hwnd = foreground_hwnd()
    deleted, why = injection_safety.delete_backwards(
        n, still_target=lambda: start_hwnd is not None and foreground_hwnd() == start_hwnd)
    if why != 'done':
        logger.warning('[SESSION] scratch-that stopped after %d of %d characters: %s',
                       deleted, n, why)
        return False
    return True

