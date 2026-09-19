"""Qt-thread owner for the live-surface model and its one widget."""
from __future__ import annotations

from collections import deque
import threading
from typing import Any, Callable

from samsara.live_surface.model import (CaptureState, EventKind, Lane, LiveSurfaceModel,
                                        NoticeKind, SurfaceEvent)
from samsara.ui import qt_runtime


class LiveSurfaceController:
    """Serialize producer events before rendering the single surface.

    Producers may call :meth:`post` from audio/model workers.  The model and
    widget are only touched by :meth:`drain`, which is posted to the Qt loop.
    """

    def __init__(self, app: Any, *, model: LiveSurfaceModel | None = None,
                 widget_factory: Callable[[Any], Any] | None = None) -> None:
        self.app = app
        self.model = model or LiveSurfaceModel()
        self._widget_factory = widget_factory
        self.widget = None
        self._events: deque[SurfaceEvent] = deque()
        self._lock = threading.Lock()
        self._scheduled = False
        self._capture_seq = 0
        self._active_capture: str | None = None

    def view(self):
        return self.model.view()

    def start(self) -> None:
        """Construct the widget on the caller's Qt thread, once."""
        if self.widget is not None:
            return
        if self._widget_factory is None:
            from samsara.ui.live_surface_qt import LiveSurfaceWidget
            self._widget_factory = LiveSurfaceWidget
        self.widget = self._widget_factory(self)
        self._connect_intents()
        self.widget.refresh(self.model.view(), animate=False)
        self.widget.show()

    def post(self, event: SurfaceEvent) -> None:
        """Queue a model event without exposing text to diagnostics."""
        with self._lock:
            self._events.append(event)
            if self._scheduled:
                return
            self._scheduled = True
        qt_runtime.post(self.drain)

    def drain(self) -> int:
        """Run on the Qt thread; process queued events and repaint once."""
        with self._lock:
            events, self._events = self._events, deque()
            self._scheduled = False
        accepted = 0
        while events:
            if self.model.consume(events.popleft()):
                accepted += 1
        if self.widget is not None:
            self.widget.refresh(self.model.view())
            self.widget.show()
        return accepted

    def begin_capture(self, lane: Lane, *, capture: CaptureState = CaptureState.RECORDING,
                      capture_id: str | None = None) -> str:
        self._capture_seq += 1
        capture_id = capture_id or f"surface-{self._capture_seq}"
        self._active_capture = capture_id
        self.post(SurfaceEvent(EventKind.CAPTURE_STARTED, capture_id=capture_id,
                               sequence=1, lane=lane, capture=capture))
        return capture_id

    def stop_capture(self, capture: CaptureState = CaptureState.OFF) -> None:
        if self._active_capture is None:
            return
        self.post(SurfaceEvent(EventKind.CAPTURE_STOPPED, capture_id=self._active_capture,
                               sequence=2 ** 31 - 1, capture=capture))
        self._active_capture = None

    def notice(self, kind: NoticeKind, message: str = "") -> None:
        self.post(SurfaceEvent(EventKind.NOTICE, notice=kind, message=message))

    def sync_draft(self, manager: Any) -> None:
        """Project the authoritative 227 document without keeping a UI copy."""
        document = getattr(manager, "draft_document", None)
        if document is None:
            return
        register = getattr(manager, "set_surface_review_action_fn", None)
        if callable(register):
            register(self._voice_review_action)
        text = document.text
        kind = EventKind.DOCUMENT_UPDATED if text else EventKind.DOCUMENT_CLEARED
        self.post(SurfaceEvent(kind, document_id=document.document_id,
                               revision=document.revision, text=text))
        if text and getattr(document, "manual_commit", False):
            self.post(SurfaceEvent(EventKind.DOCUMENT_PARKED, document_id=document.document_id,
                                   revision=document.revision))

    def document_snapshot(self) -> dict | None:
        """Return stable draft coordinates for the widget, without logging text."""
        manager = self._manager()
        document = getattr(manager, "draft_document", None)
        if document is None:
            return None
        return {"document_id": document.document_id, "revision": document.revision,
                "segments": tuple((segment.id, segment.text) for segment in document.segments)}

    def close(self) -> None:
        if self.widget is not None:
            self.widget.hide()
            self.widget.deleteLater()
            self.widget = None

    def scroll_draft(self, where: str) -> None:
        """Forward existing draft-scoped voice scrolling into the one surface."""
        if self.widget is None:
            return
        qt_runtime.post(lambda: self.widget is not None and self.widget.scroll_draft(where))

    def _voice_review_action(self, action: str, value: object) -> bool:
        if self.widget is None or action not in {"correct", "word"}:
            return False
        qt_runtime.post(lambda: self.widget is not None and self.widget.voice_review_action(action, value))
        return True

    # Widget intents deliberately call the existing side-effect owners.
    def _connect_intents(self) -> None:
        for name, callback in (("commit_requested", self._commit),
                               ("clear_requested", self._clear),
                               ("pause_requested", self._pause),
                               ("correct_word_requested", self._correct),
                               ("correction_apply_requested", self._apply_correction),
                               ("correction_cancel_requested", self._cancel_correction),
                               ("edit_receipt_requested", self._edit_receipt),
                               ("collapse_requested", self.widget.hide),
                               ("show_requested", self.widget.show)):
            signal = getattr(self.widget, name, None)
            if signal is not None:
                signal.connect(callback)

    def _manager(self):
        return getattr(self.app, "_session_mode_manager", None)

    def _commit(self) -> None:
        manager = self._manager()
        commit = getattr(manager, "commit_pending_dictation", None)
        if callable(commit):
            commit()
            self.sync_draft(manager)

    def _clear(self) -> None:
        manager = self._manager()
        clear = getattr(manager, "confirm_clear_draft", None)
        if callable(clear):
            clear("live surface")
            self.sync_draft(manager)

    def _pause(self) -> None:
        pause_hold = getattr(self.app, "pause_hold_capture", None)
        if callable(pause_hold) and pause_hold(source="live surface", consume_keyup=False):
            return
        manager = self._manager()
        pause_draft = getattr(manager, "pause_draft", None)
        if callable(pause_draft):
            pause_draft(source="live surface")
        stop = getattr(self.app, "stop_continuous_mode", None)
        if callable(stop) and getattr(self.app, "continuous_active", False):
            stop()
        else:
            self.stop_capture()

    def _correct(self, span: object) -> None:
        manager = self._manager()
        select = getattr(manager, "select_live_surface_word", None)
        if not callable(select):
            return
        result = select(span)
        if not result.get("ok"):
            if self.widget is not None:
                self.widget.correction_refused()
            return
        selection = result["selection"]
        # This is the sole proposal service.  It is pure and receives only
        # caller-owned snapshots, never audio or a model request.
        from samsara.correction_candidates import build_snapshot, suggest_candidates
        snapshot = build_snapshot(self._approved_corrections(), self._taught_vocabulary())
        candidates = suggest_candidates(str(selection["word"]),
                                        (int(selection["start"]), int(selection["end"])),
                                        language="en", snapshot=snapshot)
        self.sync_draft(manager)
        # Selection is a deliberate review operation, so it owns the R form
        # even if no audio capture happens to be active.
        self.model.consume(SurfaceEvent(EventKind.CORRECTION_OPENED))
        if self.widget is not None:
            self.widget.refresh(self.model.view(), animate=False)
            self.widget.show_candidates(selection, candidates)

    def _apply_correction(self, selection: object, replacement: str) -> None:
        manager = self._manager()
        apply = getattr(manager, "apply_live_surface_correction", None)
        if not callable(apply):
            return
        result = apply(selection, replacement)
        if not result.get("ok"):
            if self.widget is not None:
                self.widget.correction_refused()
            return
        self.sync_draft(manager)
        self.model.consume(SurfaceEvent(EventKind.CORRECTION_CLOSED))
        if self.widget is not None:
            self.widget.refresh(self.model.view(), animate=False)
            self.widget.correction_refused("Correction applied")

    def _cancel_correction(self) -> None:
        manager = self._manager()
        cancel = getattr(manager, "cancel_word_correction", None)
        if callable(cancel):
            cancel("live_surface_cancelled")

    def _edit_receipt(self, text: str) -> None:
        manager = self._manager()
        copy = getattr(manager, "copy_sent_receipt_as_draft", None)
        if not callable(copy):
            return
        result = copy(text)
        if result.get("ok"):
            self.sync_draft(manager)
        elif self.widget is not None:
            self.widget.correction_refused("Finish or clear the current draft first")

    def _approved_corrections(self):
        training = getattr(self.app, "voice_training_window", None)
        values = getattr(training, "corrections_dict", None) if training is not None else None
        return values if isinstance(values, dict) else ()

    def _taught_vocabulary(self):
        training = getattr(self.app, "voice_training_window", None)
        values = getattr(training, "custom_vocab", None) if training is not None else ()
        return values if isinstance(values, (tuple, list, set)) else ()


class LiveSurfaceIndicatorAdapter:
    """Old indicator call surface backed by the new controller, not a QWidget."""

    def __init__(self, controller: LiveSurfaceController) -> None:
        self.controller = controller
        self._lane = Lane.HOLD

    def set_mode(self, text: str) -> None:
        lower = str(text).lower()
        self._lane = Lane.CONTINUOUS if "continuous" in lower else (
            Lane.HANDS_FREE_DICTATE if "wake" in lower or "dictate" in lower else Lane.HOLD)

    def set_listening(self, active: bool) -> None:
        if active:
            self.controller.begin_capture(self._lane)
        else:
            self.controller.stop_capture()

    def set_wake_armed(self, armed: bool) -> None:
        self.controller.post(SurfaceEvent(EventKind.CAPTURE_CHANGED,
            capture_id=self.controller._active_capture, sequence=1,
            capture=CaptureState.WAKE_ARMED if armed else CaptureState.OFF))

    def flash_success(self) -> None:
        self.controller.notice(NoticeKind.RESULT, "Captured")

    def flash_error(self) -> None:
        self.controller.notice(NoticeKind.ERROR, "Recording failed")

    def show(self) -> None:
        if self.controller.widget is not None:
            self.controller.widget.show()

    def hide(self) -> None:
        if self.controller.widget is not None:
            self.controller.widget.hide()

    def destroy(self) -> None:
        self.controller.close()

    def __getattr__(self, _name: str):
        """Legacy decoration/placement calls are deliberately harmless."""
        return lambda *args, **kwargs: None
