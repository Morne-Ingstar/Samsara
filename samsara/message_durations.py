"""Shared reading time for Full feedback and Compact's outcome chip."""
from __future__ import annotations
from time import monotonic

MIN_MESSAGE_MS = 2500
MESSAGE_BASE_MS = 0
MESSAGE_CHAR_MS = 60
MAX_MESSAGE_MS = 8000


def routine_receipt(label: str, kind: str) -> bool:
    """Routine delivery receipts are omitted from command History."""
    return getattr(kind, 'value', kind) == 'success' and str(label) in ('typed', 'Captured')


def routine_progress(purpose, owner) -> bool:
    """Hold-release progress is capture state; a new queued action is a message."""
    return getattr(purpose, 'value', purpose) == 'running' and owner == 'hold'


def message_duration_ms(label: str, kind: str, requested_ms=1800, *, purpose='result'):
    """Normalize legacy numeric TTLs; preserve explicit persistent lifecycles.

    Questions/running work belong to their producer. Result errors get reading
    time even when a legacy caller requests an untimed fault; device state is
    handled separately by each display.
    """
    kind, purpose = getattr(kind, 'value', kind), getattr(purpose, 'value', purpose)
    if purpose != 'result' or kind in ('pending', 'live') or (requested_ms is None and kind != 'error'):
        return None
    # 899: capture reasons and ordinary results share 591's reading formula.
    return max(MIN_MESSAGE_MS, min(MAX_MESSAGE_MS, MESSAGE_BASE_MS + MESSAGE_CHAR_MS * len(str(label))))


def message_protected(label, kind, expires_at, now, *, purpose='result') -> bool:
    """Only a timed result can defer a routine state update or clear."""
    return (bool(label) and getattr(purpose, 'value', purpose) == 'result'
            and getattr(kind, 'value', kind) not in ('pending', 'live')
            and expires_at is not None and now < expires_at)


def message_priority(kind, *, purpose='result', owner='') -> int:
    """Capture and errors may interrupt; ordinary results wait their turn."""
    kind, purpose = getattr(kind, 'value', kind), getattr(purpose, 'value', purpose)
    if kind == 'live':
        return 5
    if kind == 'error':
        return 4
    if purpose == 'running':
        return 0 if routine_progress(purpose, owner) else 2
    if purpose == 'question' or kind in ('pending', 'question'):
        return 3
    if kind == 'warning':
        return 2
    return 1


def message_should_wait(current_kind, incoming_kind, *, protected,
                        current_purpose='result', current_owner='',
                        incoming_purpose='result', incoming_owner='', replace=False) -> bool:
    """Shared replacement rule; each display retains only its latest waiter."""
    return not replace and protected and message_priority(incoming_kind, purpose=incoming_purpose, owner=incoming_owner) <= message_priority(
        current_kind, purpose=current_purpose, owner=current_owner)


class ReceiptDwell:
    """Keep one footer receipt and the latest waiter across refresh/timer calls.

    Callbacks render the text and arm the display's existing timer. Returning
    False from restore lets the display replace the expired receipt with its
    current summary; state updates themselves never wait on this object.
    """

    def __init__(self, show, start_timer):
        self._show = show
        self._start_timer = start_timer
        self.deadline = None
        self._kind = 'success'
        self._label = ''
        self._waiting = None

    def show(self, label, success=True):
        # Promote an overdue waiter before considering a newly arriving receipt.
        if self._waiting is not None and self.deadline is not None and monotonic() >= self.deadline:
            self.restore()
        kind = 'success' if success else 'error'
        if message_should_wait(self._kind, kind, protected=message_protected(
                self._label, self._kind, self.deadline, monotonic())):
            self._waiting = (label, success)
            return
        self._show(label, success)
        duration = message_duration_ms(label, kind)
        self._label, self._kind = label, kind
        self.deadline = monotonic() + duration / 1000
        self._start_timer(duration)

    def restore(self):
        """Rearm early callbacks or promote a waiter; report whether text stays."""
        now = monotonic()
        if self.deadline is not None and now < self.deadline:
            self._start_timer(max(1, round((self.deadline - now) * 1000)))
            return True
        self.deadline = None
        waiting, self._waiting = self._waiting, None
        if waiting is not None:
            self.show(*waiting)
            return True
        return False


def restore_receipt_text(widget, text):
    """Restore a timed button receipt unless its row was already rebuilt."""
    try:
        widget.setText(text)
    except RuntimeError:
        pass
