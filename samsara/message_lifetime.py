"""Message slots and scoped acknowledgement shared by auxiliary surfaces."""
from dataclasses import dataclass
from time import monotonic

from samsara.message_durations import PendingMessages, message_duration_ms, message_protected, message_should_wait


@dataclass(frozen=True)
class Message:
    text: str
    kind: str
    deadline: float | None
    payload: object = None


class MessageSlot:
    """Retain the latest waiting result, starting its reading time on display."""

    def __init__(self, clock=monotonic):
        self.clock = clock
        self.current = None
        self.waiting = PendingMessages()

    def advance(self):
        if self.current and self.current.deadline is not None and self.clock() >= self.current.deadline:
            self.current = None
        if self.current is None and self.waiting:
            args = self.waiting.pop()
            self._display(*args)
        return self.current

    def offer(self, text, kind='accent', requested_ms=1800, payload=None):
        self.advance()
        current = self.current
        if current and message_should_wait(current.kind, kind, protected=message_protected(
                current.text, current.kind, current.deadline, self.clock())):
            self.waiting.push((text, kind, requested_ms, payload), kind)
            return current
        self._display(text, kind, requested_ms, payload)
        return self.current

    def _display(self, text, kind, requested_ms, payload):
        duration = message_duration_ms(text, kind, requested_ms)
        self.current = Message(str(text), kind, None if duration is None else self.clock() + duration / 1000, payload)

    def clear(self):
        current = self.advance()
        if current and not message_protected(current.text, current.kind, current.deadline, self.clock()):
            self.current = None
        return self.advance()

    def dismiss(self, *, next_action=False):
        self.current = None
        if next_action:
            self.waiting.clear()
        return self.advance()


def dismiss_messages(app):
    """Run on the UI thread; explicit acknowledgement also clears Home feedback."""
    indicator = getattr(app, 'listening_indicator', None)
    dismiss = getattr(indicator, 'dismiss_error', None)
    if callable(dismiss):
        dismiss()
    window = getattr(getattr(app, 'main_window', None), '_window', None)
    if window is not None:
        from PySide6.QtWidgets import QWidget
        card = window.findChild(QWidget, 'homeNotice')
        if card is not None:
            from samsara.ui.home_notice_qt import dismiss_problem_notice
            dismiss_problem_notice(card)
        bar = window.statusBar()
        dismiss = getattr(bar, 'dismiss', None)
        if callable(dismiss):
            dismiss()


@dataclass
class AcknowledgementCapture:
    capture_id: object
    eligible: bool = False


@dataclass
class AcknowledgementReceipt:
    state: object
    capture: AcknowledgementCapture
    eligible: bool


def acknowledgement_state(app):
    indicator = getattr(app, 'listening_indicator', None)
    controller = getattr(indicator, 'controller', None)
    return getattr(controller, 'model', None) if controller is not None else indicator


def begin_acknowledgement_capture(state, capture_id=None):
    """A replacement capture invalidates every receipt from its predecessor."""
    state._acknowledgement_capture = AcknowledgementCapture(capture_id or object())


def arm_acknowledgement(state):
    capture = getattr(state, '_acknowledgement_capture', None)
    if capture is not None:
        capture.eligible = True


def end_acknowledgement_capture(state):
    """Stop/cancel/empty completion cannot leave display eligibility armed."""
    capture = getattr(state, '_acknowledgement_capture', None)
    if capture is not None:
        capture.eligible = False


def take_acknowledgement(app, *, enabled=True):
    """Move eligibility into this decoder's local, one-use capture receipt.

    Key release precedes decoding. No later routing call may infer ownership
    from whatever capture happens to be displayed when its text arrives.
    """
    state = acknowledgement_state(app)
    capture = getattr(state, '_acknowledgement_capture', None)
    if capture is None:
        return None
    receipt = AcknowledgementReceipt(state, capture, enabled and capture.eligible)
    capture.eligible = False
    return receipt


def retire_acknowledgement(app, receipt=None):
    state = acknowledgement_state(app)
    if state is not None and (receipt is None or
            getattr(state, '_acknowledgement_capture', None) is receipt.capture):
        state._acknowledgement_capture = None
    if receipt is not None:
        receipt.eligible = False


def consume_acknowledgement(app, text, *, receipt=None):
    """Only consume dismiss/okay with the receipt for the originating capture.

    Pending questions keep their existing answer routing. No global command is
    added, so an ordinary dictated 'okay' remains ordinary text.
    """
    from samsara.dictation_retry import request
    if request(app, text):
        return True
    state = acknowledgement_state(app)
    if receipt is None:
        return False
    eligible = (receipt.eligible and receipt.state is state and
                getattr(state, '_acknowledgement_capture', None) is receipt.capture)
    receipt.eligible = False
    if not eligible or str(text).strip().casefold().rstrip('.!?') not in ('dismiss', 'okay', 'ok'):
        return False
    if getattr(app, '_ava_question', None) is not None:
        return False
    from samsara.execution_policy import pending_operation
    if pending_operation() is not None:
        return False
    schedule = getattr(app, '_schedule_ui', None)
    if callable(schedule):
        schedule(dismiss_messages, app)
    return True
