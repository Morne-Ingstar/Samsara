"""Qt-thread owner for the live-surface model and its one widget."""
from __future__ import annotations

from collections import deque
import threading
from typing import Any, Callable

from samsara.live_surface.model import (CaptureState, EventKind, Lane, LiveSurfaceModel,
                                        NoticeKind, SurfaceEvent)
from samsara.live_surface.caret import CaretLocator
from samsara.live_surface.placement import (Edge, Placement, Rect, Screen,
                                             place_inline_form, free_placement_for_mark,
                                             placement_for_screens, placement_record)
from samsara.ui import qt_runtime


class LiveSurfaceController:
    """Serialize producer events before rendering the single surface.

    Producers may call :meth:`post` from audio/model workers.  The model and
    widget are only touched by :meth:`drain`, which is posted to the Qt loop.
    """

    def __init__(self, app: Any, *, model: LiveSurfaceModel | None = None,
                 widget_factory: Callable[[Any], Any] | None = None,
                 caret_locator: CaretLocator | None = None) -> None:
        self.app = app
        self.model = model or LiveSurfaceModel()
        self._widget_factory = widget_factory
        self.widget = None
        self._events: deque[SurfaceEvent] = deque()
        self._lock = threading.Lock()
        self._scheduled = False
        self._capture_seq = 0
        self._active_capture: str | None = None
        self.idle_capture = CaptureState.OFF
        self.idle_lane = Lane.HOLD
        # The host supplies the platform lookup.  It is intentionally optional
        # on non-Windows and in deterministic tests.
        self._caret_locator = caret_locator or getattr(app, "live_surface_caret_locator", None)
        self._last_caret = None

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
        self._place_widget()
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
            event = events.popleft()
            if event.kind in (EventKind.CAPTURE_STARTED, EventKind.DOCUMENT_UPDATED):
                # These are the allowed placement reevaluation boundaries.
                self._last_caret = None
            if self.model.consume(event):
                accepted += 1
        if self.widget is not None:
            self.widget.refresh(self.model.view(), animate=False)
            self._place_widget()
            self.widget.show()
        return accepted

    def refresh_expired(self) -> None:
        """Expire receipts without another utterance or a forced window show."""
        if self.widget is not None and not self.widget.is_dragging:
            view = self.model.view()
            if view != self.widget.view:
                self.widget.refresh(view, animate=False)
                self._place_widget()

    def begin_capture(self, lane: Lane, *, capture: CaptureState = CaptureState.RECORDING,
                      capture_id: str | None = None) -> str:
        self._capture_seq += 1
        capture_id = capture_id or f"surface-{self._capture_seq}"
        self._active_capture = capture_id
        self.post(SurfaceEvent(EventKind.CAPTURE_STARTED, capture_id=capture_id,
                               sequence=1, lane=lane, capture=capture))
        return capture_id

    def stop_capture(self, capture: CaptureState | None = None, *,
                     capture_id: str | None = None) -> None:
        if self._active_capture is None or (capture_id is not None and capture_id != self._active_capture):
            return
        self.post(SurfaceEvent(EventKind.CAPTURE_STOPPED, capture_id=self._active_capture,
                               sequence=2 ** 31 - 1, capture=capture or self.idle_capture))
        self._active_capture = None
        if self.idle_capture is CaptureState.HANDS_FREE_LISTENING:
            self.post(SurfaceEvent(EventKind.LANE_CHANGED, lane=self.idle_lane))

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
                               ("move_requested", self._move_requested),
                               ("collapse_requested", self.widget.hide),
                               ("show_requested", self.widget.show)):
            signal = getattr(self.widget, name, None)
            if signal is not None:
                signal.connect(callback)

    def _placement_config(self) -> tuple[dict, dict]:
        ui = (getattr(self.app, "config", {}) or {}).get("ui", {}) or {}
        surface = ui.get("live_surface", {}) or {}
        raw = surface.get("placement", {}) or {}
        return dict(raw), dict(raw.get("monitors", {}) or {})

    def _screens(self) -> list[Screen]:
        from PySide6.QtGui import QGuiApplication
        result = []
        for index, screen in enumerate(QGuiApplication.screens()):
            area = screen.availableGeometry()
            result.append(Screen(screen.name() or f"screen-{index}",
                                 Rect(area.x(), area.y(), area.width(), area.height()),
                                 screen is QGuiApplication.primaryScreen()))
        return result

    def _place_widget(self) -> None:
        if self.widget is None or not callable(getattr(self.widget, "apply_placement", None)):
            return
        if self.widget.is_dragging:
            return
        screens = self._screens()
        if not screens:
            return
        raw, records = self._placement_config()
        resolved = placement_for_screens(records, screens,
            preferred_monitor=raw.get("preferred_monitor"))
        mark = self.widget.mark_rect
        dimensions = dict(width=self.widget.width(), height=self.widget.height(),
                          mark_offset=(mark.x(), mark.y()))
        placed = place_inline_form(resolved.placement, resolved.screen, **dimensions)
        # A deliberate free drop wins over automatic caret avoidance.
        if self._caret_locator is not None and resolved.placement.edge is not Edge.FREE:
            caret = self._caret_locator.request()
            if caret is not None:
                self._last_caret = caret
            if self._last_caret is not None:
                from samsara.live_surface.placement import _overlaps, _expand, CARET_PADDING_DIP
                protected = _expand(self._last_caret.rect, CARET_PADDING_DIP)
                if _overlaps(placed.bounds, protected):
                    for edge in (resolved.placement.edge, Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT):
                        for t in (.15, .85, 0., 1.):
                            candidate = place_inline_form(Placement(edge, t), resolved.screen, **dimensions)
                            if not _overlaps(candidate.bounds, protected):
                                placed = candidate
                                break
                        else:
                            continue
                        break
        self.widget.apply_placement(placed)

    def _move_requested(self, detail: object) -> None:
        if not isinstance(detail, dict):
            return
        screens = self._screens()
        if not screens:
            return
        cx, cy = detail.get("center", (None, None))
        try:
            screen = next((item for item in screens if item.work_area.x <= float(cx) <= item.work_area.right
                           and item.work_area.y <= float(cy) <= item.work_area.bottom), screens[0])
        except (TypeError, ValueError):
            return
        old, records = self._placement_config()
        placement = free_placement_for_mark((float(cx), float(cy)), screen.work_area)
        records[screen.id] = placement_record(placement)
        self._save_placement({"preferred_monitor": screen.id, "monitors": records})
        self._place_widget()

    def _save_placement(self, placement: dict) -> None:
        config = getattr(self.app, "config", None)
        if not isinstance(config, dict):
            return
        ui = dict(config.get("ui", {}) or {})
        surface = dict(ui.get("live_surface", {}) or {})
        surface["placement"] = placement; ui["live_surface"] = surface
        update = getattr(self.app, "update_config_and_save", None)
        if callable(update):
            update({"ui": ui})
        else:
            config["ui"] = ui
            save = getattr(self.app, "save_config", None)
            if callable(save): save()

    def reset_placement(self) -> bool:
        """One recovery action: bottom-centre, visible, draft untouched."""
        screens = self._screens()
        if not screens:
            return False
        raw, records = self._placement_config()
        screen = next((item for item in screens if item.id == raw.get("preferred_monitor")),
                      next((item for item in screens if item.primary), screens[0]))
        records[screen.id] = placement_record(Placement(Edge.BOTTOM, .5))
        self._save_placement({"preferred_monitor": screen.id, "monitors": records})
        if self.widget is not None:
            self._place_widget(); self.widget.show()
        return True

    def move_to_foreground_screen(self) -> bool:
        """Move the saved position to the foreground display without changing its edge.

        A host may supply the active display name.  When that is unavailable,
        deliberately use the primary display rather than inferring a screen from
        a global coordinate that may belong to a different DPI space.
        """
        screens = self._screens()
        if not screens:
            return False
        wanted = str(getattr(self.app, "foreground_screen_name", "") or "")
        screen = next((item for item in screens if item.id == wanted), None)
        screen = screen or next((item for item in screens if item.primary), screens[0])
        raw, records = self._placement_config()
        current = records.get(screen.id, placement_record(Placement(Edge.BOTTOM, .5)))
        records[screen.id] = current
        self._save_placement({"preferred_monitor": screen.id, "monitors": records})
        if self.widget is not None:
            self._place_widget(); self.widget.show()
        return True

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
        self._session_lane = None
        self._wake_armed = False
        self._capture_id = None

    def set_mode(self, text: str) -> None:
        lower = str(text).lower()
        self._lane = (Lane.AVA if "ava" in lower else Lane.COMMAND if "command" in lower
                      else Lane.CONTINUOUS if "continuous" in lower else
                      Lane.HANDS_FREE_DICTATE if "wake" in lower or "dictate" in lower else Lane.HOLD)
        if self._session_lane is None:
            self.controller.post(SurfaceEvent(EventKind.LANE_CHANGED, lane=self._lane))

    def set_session_mode(self, name, _color=None) -> None:
        lower = str(name or "").lower()
        self._session_lane = (Lane.AVA if "ava" in lower else
                              Lane.COMMAND if "command" in lower else
                              Lane.HANDS_FREE_DICTATE) if name else None
        self.controller.idle_capture = (CaptureState.HANDS_FREE_LISTENING if name else
                                       CaptureState.WAKE_ARMED if self._wake_armed else CaptureState.OFF)
        self.controller.idle_lane = self._session_lane or self._lane
        self.controller.post(SurfaceEvent(EventKind.LANE_CHANGED,
                                         lane=self._session_lane or self._lane))
        if self.controller._active_capture is None:
            self.controller.post(SurfaceEvent(EventKind.CAPTURE_CHANGED,
                                             capture=self.controller.idle_capture))

    def set_command_mode(self, active: bool) -> None:
        self.set_session_mode("COMMAND" if active else None)

    def set_listening(self, active: bool) -> None:
        if active:
            # Streaming producers own their token through the final result.
            # A delayed legacy indicator call must not retire that producer.
            if self.controller._active_capture is None:
                self._capture_id = self.controller.begin_capture(self._session_lane or self._lane)
        else:
            if self._capture_id is not None:
                self.controller.stop_capture(capture_id=self._capture_id)
                self._capture_id = None

    def set_wake_armed(self, armed: bool) -> None:
        self._wake_armed = armed
        if self._session_lane is None:
            self.controller.idle_capture = CaptureState.WAKE_ARMED if armed else CaptureState.OFF
            if self.controller._active_capture is None:
                self.controller.post(SurfaceEvent(EventKind.CAPTURE_CHANGED,
                                                  capture=self.controller.idle_capture))

    def flash_wake(self) -> None:
        self.controller.notice(NoticeKind.RESULT, "Wake phrase heard")

    def set_thinking(self, active: bool) -> None:
        if active:
            self.controller.post(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.RESULT,
                                              message="Ava thinking", ttl_s=60.0))
        else:
            self.clear_outcome()

    def show_outcome(self, label: str, kind: str, ttl_ms=1000) -> None:
        if not label:
            self.clear_outcome()
            return
        if kind == "live":
            return  # Capture already owns the recording indication and token.
        notice = NoticeKind.ERROR if kind == "error" else NoticeKind.RESULT
        # These are transient outcome chips, including a refused optional-AI
        # action. They are not persistent microphone faults or confirmations.
        ttl = 60.0 if kind == "pending" else min(2.0 if kind == "error" else 1.0,
                                                  (ttl_ms or 1000) / 1000)
        self.controller.post(SurfaceEvent(EventKind.NOTICE, notice=notice,
                                          message=str(label), ttl_s=ttl))

    def clear_outcome(self) -> None:
        self.controller.post(SurfaceEvent(EventKind.NOTICE_CLEARED, notice=NoticeKind.RESULT))

    def flash_success(self) -> None:
        self.controller.notice(NoticeKind.RESULT, "Captured")

    def flash_error(self) -> None:
        self.show_outcome("Recording failed", "error", 2000)

    def show(self) -> None:
        if self.controller.widget is not None:
            self.controller.widget.show()

    def hide(self) -> None:
        # Legacy preview/session teardown must not remove the active mode badge.
        if self._session_lane is None and self.controller.widget is not None:
            self.controller.widget.hide()

    def destroy(self) -> None:
        self.controller.close()

    def __getattr__(self, _name: str):
        """Legacy decoration/placement calls are deliberately harmless."""
        return lambda *args, **kwargs: None
