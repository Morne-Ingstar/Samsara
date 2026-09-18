"""Pure state-contract traces for live surface package A."""
from samsara.live_surface.model import (
    CaptureState, DocumentState, EventKind, Lane, LiveSurfaceModel, NoticeKind,
    PresentationState, SurfaceEvent, VisibleForm,
)


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


def event(kind, capture_id="c1", sequence=1, **kwargs):
    return SurfaceEvent(kind, capture_id=capture_id, sequence=sequence, **kwargs)


def test_five_axes_are_independent_enums():
    assert CaptureState.RECORDING.value == "recording"
    assert Lane.HANDS_FREE_DICTATE.value == "hands_free_dictate"
    assert DocumentState.PARKED_DRAFT.value == "parked_draft"
    assert PresentationState.CORRECTION.value == "correction"
    assert NoticeKind.ERROR.value == "error"


def test_state_table_core_forms_and_draft_never_collapses_to_mark():
    clock = Clock(); model = LiveSurfaceModel(clock=clock)
    assert model.view().form is VisibleForm.MARK
    assert model.consume(event(EventKind.CAPTURE_STARTED, lane=Lane.HOLD, capture=CaptureState.RECORDING))
    assert model.view().form is VisibleForm.STATUS
    assert model.consume(event(EventKind.PARTIAL, sequence=2, lane=Lane.HOLD, text="unsettled"))
    assert model.view().form is VisibleForm.LIVE
    assert model.consume(event(EventKind.DOCUMENT_UPDATED, sequence=3, lane=Lane.HOLD,
                               document_id="d1", revision=1, text="settled"))
    assert model.consume(event(EventKind.CAPTURE_STOPPED, sequence=4, lane=Lane.HOLD))
    clock.advance(5.1)
    assert model.view().form is VisibleForm.DRAFT_BADGE
    assert model.view().form is not VisibleForm.MARK
    assert model.consume(SurfaceEvent(EventKind.CORRECTION_OPENED, document_id="d1", revision=1))
    assert model.view().form is VisibleForm.REVIEW


def test_state_table_notices_delivery_and_active_collapse_guards():
    clock = Clock(); model = LiveSurfaceModel(clock=clock)
    model.consume(event(EventKind.CAPTURE_STARTED, lane=Lane.TOGGLE, capture=CaptureState.RECORDING))
    assert not model.view().can_collapse
    model.consume(event(EventKind.CAPTURE_STOPPED, sequence=2, lane=Lane.TOGGLE))
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="d", revision=1, text="send"))
    model.consume(SurfaceEvent(EventKind.DELIVERY_STARTED, document_id="d", revision=2, text="send"))
    assert model.view().form is VisibleForm.STATUS and not model.view().can_collapse
    model.consume(SurfaceEvent(EventKind.DELIVERY_FINISHED, document_id="d", revision=3, text="send"))
    assert model.view().form is VisibleForm.STATUS
    clock.advance(3.1)
    assert model.view().form is VisibleForm.MARK
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.CONFIRMATION, message="Proceed?"))
    assert model.view().form is VisibleForm.STATUS and not model.view().can_collapse
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.ERROR, message="Microphone unavailable"))
    assert model.view().notice.kind is NoticeKind.ERROR
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.RESULT, message="late success"))
    assert model.view().notice.kind is NoticeKind.ERROR


def test_stale_sequences_stop_clear_lane_and_new_capture_are_dropped():
    model = LiveSurfaceModel(clock=Clock())
    assert model.consume(event(EventKind.CAPTURE_STARTED, lane=Lane.HOLD, capture=CaptureState.RECORDING))
    assert not model.consume(event(EventKind.PARTIAL, sequence=1, lane=Lane.HOLD, text="old"))
    assert model.consume(event(EventKind.PARTIAL, sequence=2, lane=Lane.HOLD, text="new"))
    assert model.consume(event(EventKind.LANE_CHANGED, sequence=3, lane=Lane.COMMAND))
    assert not model.consume(event(EventKind.PARTIAL, sequence=4, lane=Lane.HOLD, text="wrong lane"))
    assert model.consume(event(EventKind.CAPTURE_STOPPED, sequence=4, lane=Lane.COMMAND))
    assert not model.consume(event(EventKind.PARTIAL, sequence=5, lane=Lane.COMMAND, text="after stop"))
    assert model.consume(event(EventKind.CAPTURE_STARTED, capture_id="c2", sequence=1, lane=Lane.HOLD,
                               capture=CaptureState.RECORDING))
    assert not model.consume(event(EventKind.PARTIAL, capture_id="c1", sequence=6, lane=Lane.COMMAND, text="old capture"))
    assert model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="d", revision=2, text="current"))
    assert model.consume(SurfaceEvent(EventKind.DOCUMENT_CLEARED, document_id="d", revision=3))
    assert not model.consume(SurfaceEvent(EventKind.DOCUMENT_PARKED, document_id="d", revision=4, text="stale"))


def test_result_notice_expires_and_protected_review_wins_over_it():
    clock = Clock(); model = LiveSurfaceModel(clock=clock)
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.RESULT, message="Sent"))
    assert model.view().form is VisibleForm.STATUS
    clock.advance(3.1)
    assert model.view().notice.kind is NoticeKind.NONE
    model.consume(SurfaceEvent(EventKind.DOCUMENT_UPDATED, document_id="d", revision=1, text="draft"))
    model.consume(SurfaceEvent(EventKind.REVIEW_PINNED, document_id="d", revision=1))
    model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.RESULT, message="command complete"))
    assert model.view().form is VisibleForm.REVIEW
