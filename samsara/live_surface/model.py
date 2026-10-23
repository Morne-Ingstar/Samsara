"""Pure event/state contract for the live feedback surface; no Qt or I/O."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Callable, Optional

from samsara.live_surface.feedback import FeedbackKind, FeedbackPurpose, DEFAULT_TTL_MS
from samsara.message_durations import message_duration_ms, message_protected, message_should_wait

# Geometry/timing are design constants, never per-user tuning knobs.
MARK_TARGET_DIP = 44
BRAND_MARK_DIP = 24
CARD_CORNER_RADIUS_DIP = 12
STATUS_SIZE_DIP = (360, 132)
LIVE_SIZE_DIP = (500, 240)
REVIEW_SIZE_DIP = (500, 360)
DRAFT_BADGE_SIZE_DIP = (120, 44)
ATTACHMENT_GAP_DIP = 4
IDLE_DELAY_S = 5.0
RESULT_RECEIPT_S = message_duration_ms('typed', 'success') / 1000
ORDINARY_NOTICE_S = DEFAULT_TTL_MS / 1000
AVA_CAPTION_LINGER_S = 6.0


class CaptureState(str, Enum):
    OFF = "off"
    WAKE_ARMED = "wake_armed"
    HANDS_FREE_LISTENING = "hands_free_listening"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    UNAVAILABLE = "unavailable"


class Lane(str, Enum):
    HOLD = "hold"
    TOGGLE = "toggle"
    HANDS_FREE_DICTATE = "hands_free_dictate"
    CONTINUOUS = "continuous"
    COMMAND = "command"
    AVA = "ava"


def initial_capture_state(lane: Lane) -> CaptureState:
    """A hold starts capturing immediately; hands-free starts waiting for speech."""
    return (CaptureState.RECORDING if lane in (Lane.HOLD, Lane.TOGGLE)
            else CaptureState.HANDS_FREE_LISTENING)


class DocumentState(str, Enum):
    EMPTY = "empty"
    PROVISIONAL_ONLY = "provisional_only"
    EDITABLE_DRAFT = "editable_draft"
    PARKED_DRAFT = "parked_draft"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    DELIVERY_UNVERIFIED = "delivery_unverified"


class PresentationState(str, Enum):
    COMPACT = "compact"
    AUTOMATIC_EXPANSION = "automatic_expansion"
    PINNED_REVIEW = "pinned_review"
    CORRECTION = "correction"
    KEYBOARD_INTERACTION = "keyboard_interaction"


class NoticeKind(str, Enum):
    NONE = "none"
    RESULT = "result"
    PENDING = "pending"
    CONFIRMATION = "confirmation"
    ALIAS_OFFER = "alias_offer"
    ERROR = "error"
    SUCCESS = "success"
    WARNING = "warning"
    INFORMATION = "information"
    RUNNING = "running"


class VisibleForm(str, Enum):
    MARK = "M"
    DRAFT_BADGE = "D"
    STATUS = "S"
    LIVE = "L"
    REVIEW = "R"


class EventKind(str, Enum):
    CAPTURE_STARTED = "capture_started"
    CAPTURE_CHANGED = "capture_changed"
    CAPTURE_STOPPED = "capture_stopped"
    LANE_CHANGED = "lane_changed"
    PARTIAL = "partial"
    DOCUMENT_UPDATED = "document_updated"
    DOCUMENT_PARKED = "document_parked"
    DOCUMENT_CLEARED = "document_cleared"
    DELIVERY_STARTED = "delivery_started"
    DELIVERY_FINISHED = "delivery_finished"
    DELIVERY_UNVERIFIED = "delivery_unverified"
    NOTICE = "notice"
    NOTICE_CLEARED = "notice_cleared"
    SNOOZE_CHANGED = "snooze_changed"
    DEVICE_CHANGED = "device_changed"
    REVIEW_PINNED = "review_pinned"
    REVIEW_UNPINNED = "review_unpinned"
    CORRECTION_OPENED = "correction_opened"
    CORRECTION_CLOSED = "correction_closed"
    KEYBOARD_INTERACTION_STARTED = "keyboard_interaction_started"
    KEYBOARD_INTERACTION_ENDED = "keyboard_interaction_ended"


@dataclass(frozen=True)
class SurfaceEvent:
    """A producer event. ``sequence`` is monotonically increasing per capture.

    Document producers must also supply ``document_id`` and an increasing
    ``revision``.  All fields are intentionally transport-neutral so the
    controller can publish them from worker threads before Qt is introduced.
    """

    kind: EventKind
    capture_id: Optional[str] = None
    sequence: int = 0
    lane: Optional[Lane] = None
    capture: Optional[CaptureState] = None
    document_id: Optional[str] = None
    revision: Optional[int] = None
    text: Optional[str] = None
    notice: NoticeKind = NoticeKind.NONE
    message: str = ""
    ttl_s: Optional[float] = None
    snoozed: bool = False
    device_lost: bool = False
    feedback: FeedbackKind | None = None
    purpose: FeedbackPurpose | None = None
    owner: str = ""
    resolves: str = ""
    persistent: bool = False


@dataclass(frozen=True)
class Notice:
    kind: NoticeKind
    message: str
    expires_at: Optional[float]
    lane: Optional[Lane] = None
    feedback: FeedbackKind = FeedbackKind.INFORMATION
    purpose: FeedbackPurpose = FeedbackPurpose.RESULT
    owner: str = ""


@dataclass(frozen=True)
class SurfaceView:
    form: VisibleForm
    capture: CaptureState
    lane: Optional[Lane]
    document: DocumentState
    presentation: PresentationState
    notice: Notice
    document_id: Optional[str]
    revision: int
    text: str
    provisional_text: str
    can_collapse: bool
    snoozed: bool = False
    capture_id: Optional[str] = None
    secondary_notice: Notice | None = None
    feedback_event: Notice | None = None
    feedback_revision: int = 0


class LiveSurfaceModel:
    """The one pure owner of visible state.

    ``consume(event)`` returns False for stale producer work. ``view()`` is
    clock-driven only through the injected ``clock`` and may safely be called
    from a UI timer without changing capture or document ownership.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self.capture = CaptureState.OFF
        self.snoozed = False
        self.device_lost = False
        self.lane: Optional[Lane] = None
        self.document = DocumentState.EMPTY
        self.presentation = PresentationState.COMPACT
        self.document_id: Optional[str] = None
        self.revision = 0
        self.text = ""
        self.provisional_text = ""
        self._active_capture: Optional[str] = None
        self._retired_captures: set[str] = set()
        self._sequence: dict[str, int] = {}
        self._cleared_documents: set[str] = set()
        self._notice = Notice(NoticeKind.NONE, "", None)
        self._waiting_notice: SurfaceEvent | None = None
        self._question: Notice | None = None
        self._feedback_event: Notice | None = None
        self._feedback_revision = 0
        self._last_activity = clock()
        self._delivered_at: Optional[float] = None

    def consume(self, event: SurfaceEvent) -> bool:
        """Apply one event, returning False when its causal token is stale."""
        if not self._accept_event(event):
            return False
        now = self._clock()
        self._advance_notice(now)
        correcting = self.presentation is PresentationState.CORRECTION
        self._last_activity = now
        kind = event.kind
        if kind is EventKind.CAPTURE_STARTED:
            self.dismiss_error()
            if self.document is DocumentState.DELIVERED:
                self.text = ""
                self.document = DocumentState.EMPTY
                self._delivered_at = None
            if self._notice.expires_at is not None and not self._message_protected(now):
                self._notice = Notice(NoticeKind.NONE, "", None)
            self._active_capture = event.capture_id
            self.lane = event.lane or Lane.HOLD
            self.capture = event.capture or initial_capture_state(self.lane)
            self.provisional_text = ""
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.CAPTURE_CHANGED:
            if event.capture is CaptureState.RECORDING and self.capture is not CaptureState.RECORDING:
                self.dismiss_error()
            self.capture = event.capture or self.capture
            self.lane = event.lane or self.lane
        elif kind is EventKind.CAPTURE_STOPPED:
            self.capture = event.capture or CaptureState.OFF
            if event.capture_id:
                self._retired_captures.add(event.capture_id)
            self._active_capture = None
            self.provisional_text = ""
        elif kind is EventKind.LANE_CHANGED:
            self.lane = event.lane
            if event.capture is not None:
                self.capture = event.capture
            self._clear_receipt()
            if self._notice.kind is NoticeKind.RESULT and not self._message_protected(now):
                self._notice = Notice(NoticeKind.NONE, "", None)
            self.provisional_text = ""
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.PARTIAL:
            self.provisional_text = event.text or ""
            if not self.text:
                self.document = (DocumentState.PROVISIONAL_ONLY if self.provisional_text
                                 else DocumentState.EMPTY)
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.DOCUMENT_UPDATED:
            self._set_document(event, DocumentState.EDITABLE_DRAFT)
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.DOCUMENT_PARKED:
            self._set_document(event, DocumentState.PARKED_DRAFT)
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.DOCUMENT_CLEARED:
            if self._notice.owner == "draft":
                self._notice = Notice(NoticeKind.NONE, "", None)
            if event.document_id:
                self._cleared_documents.add(event.document_id)
            self.document = DocumentState.EMPTY
            self.document_id = event.document_id
            self.revision = event.revision or self.revision
            self.text = ""
            self.provisional_text = ""
            self.presentation = PresentationState.COMPACT
        elif kind is EventKind.DELIVERY_STARTED:
            self._set_document(event, DocumentState.DELIVERING)
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.DELIVERY_FINISHED:
            self._set_document(event, DocumentState.DELIVERED)
            self._delivered_at = now
            if self._notice.expires_at is not None and not self._message_protected(now):
                self._notice = Notice(NoticeKind.NONE, "", None)
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.DELIVERY_UNVERIFIED:
            self._set_document(event, DocumentState.DELIVERY_UNVERIFIED)
            self.presentation = PresentationState.PINNED_REVIEW
        elif kind is EventKind.NOTICE:
            self._receive_notice(event, now)
        elif kind is EventKind.NOTICE_CLEARED:
            if self._waiting_notice is not None and (event.owner == self._waiting_notice.owner if event.owner
                    else self._waiting_notice.purpose in (FeedbackPurpose.RUNNING, FeedbackPurpose.QUESTION)):
                if event.notice in (NoticeKind.NONE, self._waiting_notice.notice):
                    self._waiting_notice = None
            if self._question is not None and (not event.owner or event.owner == self._question.owner):
                if event.notice in (NoticeKind.NONE, self._question.kind):
                    self._question = None
            if (not event.owner or event.owner == self._notice.owner) and event.notice in (NoticeKind.NONE, self._notice.kind) and not self._message_protected(now):
                self._notice = Notice(NoticeKind.NONE, "", None)
            if event.notice in (NoticeKind.NONE, NoticeKind.RESULT) and not event.owner:
                self._clear_receipt()
        elif kind is EventKind.DEVICE_CHANGED:
            self.device_lost = event.device_lost
        elif kind is EventKind.SNOOZE_CHANGED:
            self.snoozed = event.snoozed
        elif kind is EventKind.REVIEW_PINNED:
            self.presentation = PresentationState.PINNED_REVIEW
        elif kind is EventKind.REVIEW_UNPINNED:
            self.presentation = PresentationState.AUTOMATIC_EXPANSION
        elif kind is EventKind.CORRECTION_OPENED:
            self.presentation = PresentationState.CORRECTION
        elif kind is EventKind.CORRECTION_CLOSED:
            self.presentation = PresentationState.PINNED_REVIEW
        elif kind is EventKind.KEYBOARD_INTERACTION_STARTED:
            self.presentation = PresentationState.KEYBOARD_INTERACTION
        elif kind is EventKind.KEYBOARD_INTERACTION_ENDED:
            self.presentation = PresentationState.PINNED_REVIEW
        if correcting and kind in (EventKind.PARTIAL, EventKind.DOCUMENT_UPDATED, EventKind.DOCUMENT_PARKED):
            self.presentation = PresentationState.CORRECTION
        return True

    def view(self) -> SurfaceView:
        """Return the visible projection, expiring only elapsed notices."""
        now = self._clock()
        self._advance_notice(now)
        if self._delivered_at is not None and now >= self._delivered_at + RESULT_RECEIPT_S:
            self._clear_receipt()
        can_collapse = self._can_collapse(now)
        form = self._form(now, can_collapse)
        return SurfaceView(
            form=form, capture=CaptureState.UNAVAILABLE if self.device_lost else self.capture, lane=self.lane, document=self.document,
            presentation=self.presentation, notice=self._question or self._notice, document_id=self.document_id,
            revision=self.revision, text=self.text, provisional_text=self.provisional_text,
            can_collapse=can_collapse,
            snoozed=self.snoozed, capture_id=self._active_capture,
            secondary_notice=self._notice if self._question and self._notice.kind is not NoticeKind.NONE else None,
            feedback_event=self._feedback_event, feedback_revision=self._feedback_revision,
        )

    def _receive_notice(self, event: SurfaceEvent, now: float, *, publish_feedback: bool = True) -> None:
        if event.resolves and self._question is not None and event.resolves == self._question.owner:
            self._question = None
        purpose = event.purpose or (FeedbackPurpose.QUESTION if event.notice in
            (NoticeKind.PENDING, NoticeKind.CONFIRMATION, NoticeKind.ALIAS_OFFER) else
            FeedbackPurpose.RUNNING if event.notice is NoticeKind.RUNNING else FeedbackPurpose.RESULT)
        feedback = event.feedback or (FeedbackKind.QUESTION if purpose is FeedbackPurpose.QUESTION else
            FeedbackKind.RUNNING if purpose is FeedbackPurpose.RUNNING else
            FeedbackKind.ERROR if event.notice is NoticeKind.ERROR else
            FeedbackKind.WARNING if event.notice is NoticeKind.WARNING else
            FeedbackKind.SUCCESS if event.notice is NoticeKind.SUCCESS else FeedbackKind.INFORMATION)
        ttl = None if event.persistent or purpose is not FeedbackPurpose.RESULT else (
            event.ttl_s if event.ttl_s is not None else self._notice_ttl(event.notice))
        duration = message_duration_ms(event.message, feedback, None if ttl is None else ttl * 1000, purpose=purpose)
        ttl = None if duration is None else duration / 1000
        candidate = Notice(event.notice, event.message, None if ttl is None else now + ttl,
                           event.lane or self.lane, feedback, purpose, event.owner)
        if publish_feedback:
            # Receipt delivery/History/accessibility happen once on arrival;
            # only its visible text may wait behind the current message.
            self._feedback_revision += 1
            self._feedback_event = candidate
        if message_should_wait(self._notice.feedback, feedback, protected=self._message_protected(now),
                               current_purpose=self._notice.purpose, current_owner=self._notice.owner,
                               incoming_purpose=purpose, incoming_owner=event.owner, replace=True):
            self._waiting_notice = event
            return
        if purpose is FeedbackPurpose.QUESTION:
            self._question = candidate
            if self._notice.expires_at is not None or self._notice.purpose is FeedbackPurpose.RUNNING:
                self._notice = Notice(NoticeKind.NONE, "", None)
        elif self._question is not None:
            if feedback in (FeedbackKind.ERROR, FeedbackKind.WARNING):
                self._notice = candidate
        else:
            self._notice = candidate

    def dismiss_error(self) -> None:
        """A click/new utterance dismisses result errors, never device state."""
        if self._notice.feedback is FeedbackKind.ERROR and self._notice.purpose is FeedbackPurpose.RESULT:
            self._notice = Notice(NoticeKind.NONE, '', None)

    def _advance_notice(self, now: float) -> None:
        if self._notice.expires_at is not None and now >= self._notice.expires_at:
            self._notice = Notice(NoticeKind.NONE, "", None)
        if self._waiting_notice is not None and self._notice.kind is NoticeKind.NONE and self._question is None:
            event, self._waiting_notice = self._waiting_notice, None
            self._receive_notice(event, now, publish_feedback=False)

    def _message_protected(self, now: float) -> bool:
        return message_protected(self._notice.message, self._notice.feedback,
                                 self._notice.expires_at, now, purpose=self._notice.purpose)

    def _clear_receipt(self) -> None:
        """An expired delivery is history, never the next idle/lane status."""
        if self._delivered_at is not None and self._clock() < self._delivered_at + RESULT_RECEIPT_S:
            return
        if self.document is DocumentState.DELIVERED:
            self.document = DocumentState.EMPTY
            self.text = ""
        self._delivered_at = None

    def _accept_event(self, event: SurfaceEvent) -> bool:
        if event.capture_id is not None:
            previous = self._sequence.get(event.capture_id, -1)
            if event.sequence <= previous:
                return False
            if event.kind is EventKind.CAPTURE_STARTED:
                if self._active_capture and self._active_capture != event.capture_id:
                    self._retired_captures.add(self._active_capture)
                self._retired_captures.discard(event.capture_id)
            elif event.capture_id in self._retired_captures or event.capture_id != self._active_capture:
                return False
            if event.lane is not None and self.lane is not None and event.kind not in (
                EventKind.LANE_CHANGED, EventKind.CAPTURE_STARTED,
            ):
                if event.lane is not self.lane:
                    return False
            self._sequence[event.capture_id] = event.sequence
        if event.document_id is not None:
            if event.document_id in self._cleared_documents and event.kind is not EventKind.DOCUMENT_UPDATED:
                return False
            if self.document_id == event.document_id and event.revision is not None and event.revision < self.revision:
                return False
        return True

    def _set_document(self, event: SurfaceEvent, state: DocumentState) -> None:
        if event.document_id is not None and (event.document_id != self.document_id
                                              or event.kind is EventKind.DOCUMENT_UPDATED):
            self._cleared_documents.discard(event.document_id)
        self.document_id = event.document_id or self.document_id
        if event.revision is not None:
            self.revision = event.revision
        if event.text is not None:
            self.text = event.text
        self.provisional_text = ""
        self.document = state if self.text or state in (DocumentState.DELIVERING, DocumentState.DELIVERED,
                                                        DocumentState.DELIVERY_UNVERIFIED) else DocumentState.EMPTY

    @staticmethod
    def _notice_ttl(kind: NoticeKind) -> Optional[float]:
        if kind in (NoticeKind.ERROR, NoticeKind.CONFIRMATION, NoticeKind.ALIAS_OFFER, NoticeKind.PENDING):
            return None
        return None if kind is NoticeKind.RUNNING else DEFAULT_TTL_MS / 1000

    @staticmethod
    def _notice_priority(kind: NoticeKind) -> int:
        return {
            NoticeKind.NONE: 0,
            NoticeKind.RESULT: 1,
            NoticeKind.SUCCESS: 1,
            NoticeKind.WARNING: 2,
            NoticeKind.INFORMATION: 1,
            NoticeKind.RUNNING: 1,
            NoticeKind.PENDING: 1,
            NoticeKind.ALIAS_OFFER: 2,
            NoticeKind.CONFIRMATION: 3,
            NoticeKind.ERROR: 4,
        }[kind]

    def _can_collapse(self, now: float) -> bool:
        active = self.capture in (CaptureState.RECORDING, CaptureState.TRANSCRIBING)
        blocking = self._question is not None or self._notice.purpose is FeedbackPurpose.RUNNING or self.device_lost or self.capture is CaptureState.UNAVAILABLE or self.snoozed or self._notice.kind in (
            NoticeKind.ERROR, NoticeKind.CONFIRMATION, NoticeKind.ALIAS_OFFER, NoticeKind.PENDING)
        protected = self.presentation in (PresentationState.PINNED_REVIEW, PresentationState.CORRECTION,
                                          PresentationState.KEYBOARD_INTERACTION)
        delivered_receipt = self.document is DocumentState.DELIVERED and self._delivered_at is not None \
            and now < self._delivered_at + RESULT_RECEIPT_S
        return not (active or blocking or protected or delivered_receipt or self.document is DocumentState.DELIVERING)

    def _form(self, now: float, can_collapse: bool) -> VisibleForm:
        protected = self.presentation in (PresentationState.PINNED_REVIEW, PresentationState.CORRECTION,
                                           PresentationState.KEYBOARD_INTERACTION)
        if protected and (self.text or self.presentation is PresentationState.CORRECTION):
            return VisibleForm.REVIEW
        if self.text and self.document in (DocumentState.EDITABLE_DRAFT, DocumentState.PARKED_DRAFT):
            # A waiting draft stays readable through idle, capture and notices.
            # Clear/Insert are explicit actions; time cannot collapse its text.
            return VisibleForm.LIVE
        if (self._question is not None or self.device_lost or self.capture is CaptureState.UNAVAILABLE or self.snoozed or self._notice.kind in (NoticeKind.CONFIRMATION, NoticeKind.ALIAS_OFFER, NoticeKind.PENDING)
                or self._notice.kind is NoticeKind.ERROR and self._notice.expires_at is None):
            return VisibleForm.STATUS
        if self.capture in (CaptureState.RECORDING, CaptureState.TRANSCRIBING):
            return VisibleForm.LIVE if self.text or self.provisional_text else VisibleForm.STATUS
        if self.provisional_text:
            return VisibleForm.LIVE
        if self.document in (DocumentState.DELIVERING, DocumentState.DELIVERY_UNVERIFIED):
            return VisibleForm.STATUS
        if self.document is DocumentState.DELIVERED:
            return VisibleForm.STATUS if not can_collapse or self.capture is CaptureState.HANDS_FREE_LISTENING else VisibleForm.MARK
        if self.text:
            return VisibleForm.LIVE
        if self._notice.kind is not NoticeKind.NONE:
            return VisibleForm.STATUS
        if self.capture in (CaptureState.HANDS_FREE_LISTENING, CaptureState.WAKE_ARMED):
            return VisibleForm.STATUS
        return VisibleForm.MARK
