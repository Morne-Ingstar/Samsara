"""Qt-thread owner for the live-surface model and its one widget."""
from __future__ import annotations

from samsara.dictionary_store import get_dictionary_store

from collections import deque
import threading
from typing import Any, Callable

from samsara.live_surface.model import (CaptureState, EventKind, Lane, LiveSurfaceModel,
                                        NoticeKind, SurfaceEvent, initial_capture_state)
from samsara.live_surface.caret import CaretLocator
from samsara.live_surface.placement import (Edge, Placement, Rect, Screen,
                                             place_inline_form, free_placement_for_mark,
                                             placement_for_screens, placement_record, placement_for_drop)
from samsara.ui import qt_runtime
from samsara.live_surface.feedback import Feedback, FeedbackKind, FeedbackPurpose, DEFAULT_TTL_MS, BAND_HEIGHT_DIP, feedback_for, remember_feedback


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
        self._active_lane: Lane | None = None
        self.idle_capture = CaptureState.OFF
        self.idle_lane = Lane.HOLD
        # The host supplies the platform lookup.  It is intentionally optional
        # on non-Windows and in deterministic tests.
        self._caret_locator = caret_locator or getattr(app, "live_surface_caret_locator", None)
        self._last_caret = None
        self._placement_anchor = None

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
        from samsara.failure_reasons import preserve_clipping
        from samsara.message_durations import message_protected
        with self._lock:
            events, self._events = self._events, deque()
            self._scheduled = False
        accepted = 0
        while events:
            event = events.popleft()
            if event.kind in (EventKind.CAPTURE_STARTED, EventKind.DOCUMENT_UPDATED):
                # These are the allowed placement reevaluation boundaries.
                self._last_caret = None
            view = self.model.view()
            # 891: check in queue order; the adapter remains a post-only bridge.
            if event.kind is EventKind.NOTICE and preserve_clipping(
                    event.message, event.feedback or event.notice, view.notice.owner,
                    message_protected(view.notice.message, view.notice.feedback,
                                      view.notice.expires_at, self.model._clock())):
                continue
            revision = view.feedback_revision
            if self.model.consume(event):
                accepted += 1
                view = self.model.view()
                if event.kind is EventKind.NOTICE and view.feedback_revision != revision:
                    remember_feedback(self.app, view.feedback_event.message, view.feedback_event)
        if self.widget is not None:
            self.widget.refresh(self.model.view(), animate=True)
            self._place_widget()
            self.widget.show()
        return accepted

    def refresh_expired(self) -> None:
        """Expire receipts without another utterance or a forced window show."""
        adapter = getattr(self, "_feedback_adapter", None)
        if adapter is not None:
            adapter.reconcile_interactions()
        self._refresh_capture_activity()
        if self.widget is not None and not self.widget.is_dragging:
            view = self.model.view()
            if view != self.widget.view:
                self.widget.refresh(view, animate=True)
                self._place_widget()

    def _refresh_capture_activity(self) -> None:
        # WakeConsumer owns this plain VAD flag, including silence closure.
        # Poll it on the existing 100ms surface timer, never infer speech from
        # a slow decoder's partial or touch audio from the UI thread.
        if self._active_capture is None or self._active_lane is not Lane.HANDS_FREE_DICTATE:
            return
        if self.model._active_capture != self._active_capture:
            return  # The queued start still owns ordering.
        state = (CaptureState.RECORDING if getattr(self.app, "is_speaking", False)
                 else CaptureState.HANDS_FREE_LISTENING)
        if self.model.capture is not state:
            self.model.consume(SurfaceEvent(EventKind.CAPTURE_CHANGED, capture=state))

    def begin_capture(self, lane: Lane, *, capture: CaptureState | None = None,
                      capture_id: str | None = None) -> str:
        self._capture_seq += 1
        capture_id = capture_id or f"surface-{self._capture_seq}"
        self._active_capture = capture_id
        self._active_lane = lane
        capture = initial_capture_state(lane) if capture is None else capture
        if lane is Lane.HANDS_FREE_DICTATE and getattr(self.app, "is_speaking", False):
            capture = CaptureState.RECORDING
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
        self._active_lane = None
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
                               ("clean_up_requested", self._clean_up),
                               ("clear_requested", self._clear),
                               ("dismiss_feedback_requested", self.dismiss_error),
                               ("report_problem_requested", self._report_problem),
                               ("dismiss_question_requested", self.dismiss_question),
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

    def _report_problem(self, notice) -> None:
        from samsara.bug_report.coordinator import report_problem
        if notice is not None:
            report_problem(self.app, notice.message, notice.feedback.value)

    def dismiss_question(self) -> None:
        adapter = getattr(self, "_feedback_adapter", None)
        if adapter is not None:
            adapter.dismiss_question()

    def dismiss_error(self) -> None:
        from samsara.clipboard_recovery import IMAGE_MESSAGE, BITMAP_MESSAGE, open_choices
        if self.widget is not None and self.model.view().notice.message.startswith((IMAGE_MESSAGE, BITMAP_MESSAGE)):
            if open_choices(self.widget, self.app):
                return
        self.model.dismiss_error()
        if self.widget is not None:
            self.widget.refresh(self.model.view(), animate=False)
            self._place_widget()

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
        if "seed" in raw:
            seed = raw.pop("seed")
            if not records:
                primary = next((screen for screen in screens if screen.primary), screens[0])
                records[primary.id] = seed
                raw.update(preferred_monitor=primary.id, monitors=records)
            self._save_placement(raw)
        resolved = placement_for_screens(records, screens,
            preferred_monitor=raw.get("preferred_monitor"))
        mark = self.widget.mark_rect
        animation = getattr(self.widget, '_animation', None)
        target = animation.endValue() if animation is not None and animation.state() == animation.State.Running else None
        dimensions = dict(width=target.width() if target is not None else self.widget.width(),
                          height=target.height() if target is not None else self.widget.height(),
                          mark_offset=(mark.x(), mark.y()))
        # No saved record: retain HEAD caret avoidance and its stable anchor.
        # A user dock or FREE drop always wins over automatic relocation.
        if not records:
            from samsara.live_surface.model import VisibleForm, REVIEW_SIZE_DIP
            anchor_key = (resolved.screen.id, resolved.placement)
            cached = self._placement_anchor is not None and self._placement_anchor[0] == anchor_key
            active = getattr(self.widget, "view", self.model.view()).form is not VisibleForm.MARK
            placement_dimensions = dict(dimensions)
            if active and not cached:
                # Reserve the supported draft/review envelope once when entering
                # the active form; ordinary receipts cannot move its header mark.
                placement_dimensions.update(width=max(500, dimensions["width"]),
                                            height=max(REVIEW_SIZE_DIP[1]+BAND_HEIGHT_DIP+20, dimensions["height"]))
            placed = place_inline_form(resolved.placement, resolved.screen, **placement_dimensions)
            if cached:
                anchor = free_placement_for_mark(self._placement_anchor[1], resolved.screen.work_area)
                placed = place_inline_form(anchor, resolved.screen, **dimensions)
            # A deliberate free drop wins over automatic caret avoidance.
            if (not cached and self._caret_locator is not None
                    and resolved.placement.edge is not Edge.FREE):
                caret = self._caret_locator.request()
                if caret is not None:
                    self._last_caret = caret
                if self._last_caret is not None:
                    from samsara.live_surface.placement import _overlaps, _expand, CARET_PADDING_DIP
                    protected = _expand(self._last_caret.rect, CARET_PADDING_DIP)
                    if _overlaps(placed.bounds, protected):
                        for edge in (resolved.placement.edge, Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT):
                            for t in (.15, .85, 0., 1.):
                                candidate = place_inline_form(Placement(edge, t), resolved.screen, **placement_dimensions)
                                if not _overlaps(candidate.bounds, protected):
                                    placed = candidate
                                    break
                            else:
                                continue
                            break
            if active and not cached:
                self._placement_anchor = (anchor_key, (placed.mark.x + 22, placed.mark.y + 22))
                anchor = free_placement_for_mark(self._placement_anchor[1], resolved.screen.work_area)
                placed = place_inline_form(anchor, resolved.screen, **dimensions)
            self.widget.apply_placement(placed)
            return
        # Only the current visible size participates in placement. Reusing a
        # FREE mark anchor for a dock would lose its edge on the next resize.
        placed = place_inline_form(resolved.placement, resolved.screen, **dimensions)
        self.widget.apply_placement(placed)

    def preview_move(self, bounds: Rect):
        """Read-only drag preview using exactly the release snap calculation."""
        if self.widget is None:
            return None
        mark = self.widget.mark_rect
        center = (bounds.x + mark.x() + 22, bounds.y + mark.y() + 22)
        screens = self._screens()
        if not screens:
            return None
        screen = next((s for s in screens if s.work_area.x <= center[0] <= s.work_area.right
                       and s.work_area.y <= center[1] <= s.work_area.bottom),
                      min(screens, key=lambda s: (s.work_area.center[0] - center[0]) ** 2
                          + (s.work_area.center[1] - center[1]) ** 2))
        placement = placement_for_drop(bounds, center, screen)
        return place_inline_form(placement, screen, width=bounds.width, height=bounds.height,
                                 mark_offset=(mark.x(), mark.y()))

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
        center = (float(cx), float(cy))
        bounds = detail.get("bounds")
        if not isinstance(bounds, Rect):
            mark = self.widget.mark_rect if self.widget is not None else None
            ox, oy = (mark.x(), mark.y()) if mark is not None else (0, 0)
            bounds = Rect(center[0] - 22 - ox, center[1] - 22 - oy,
                          self.widget.width() if self.widget is not None else 44,
                          self.widget.height() if self.widget is not None else 44)
        placement = placement_for_drop(bounds, center, screen)
        records[screen.id] = placement_record(placement)
        self._save_placement({"preferred_monitor": screen.id, "monitors": records})
        self._placement_anchor = None
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

    def _clean_up(self) -> None:
        from samsara.ava_edit import session
        manager = self._manager()
        staged = session.pending()
        if staged is not None and staged.draft_target is not None:
            session.apply(self.app)
        elif manager is not None and manager.dictate_pending_buffer:
            session.handle_utterance(self.app, "clean up")

    def _clear(self) -> None:
        from samsara.ava_edit import session
        staged = session.pending()
        if staged is not None and staged.draft_target is not None:
            session.discard(self.app)
            return
        hold = self._active_lane is Lane.HOLD
        generation = self._capture_seq
        if hold:
            # Use the existing capture owner: it cancels audio, releases ducking
            # and closes the stream. Retiring the token rejects delayed partials.
            cancel = getattr(self.app, "cancel_recording", None)
            if callable(cancel):
                cancel()
            else:
                session = getattr(self.app, "_streaming_session", None)
                if session is not None:
                    session.cancel()
            self.stop_capture()
        manager = self._manager()
        clear = getattr(manager, "confirm_clear_draft", None)
        if callable(clear):
            clear("live surface")
            self.sync_draft(manager)
        if hold:
            self.post(SurfaceEvent(EventKind.DOCUMENT_CLEARED))
            # Host cancellation queues its legacy error chip. Place the explicit
            # discard receipt after those callbacks, without letting it overwrite
            # a newer capture. No widget/lane may revive the discarded token.
            def cleared():
                if generation != self._capture_seq:
                    return
                self.post(SurfaceEvent(EventKind.NOTICE_CLEARED, owner="hold"))
                self.post(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.SUCCESS,
                                      message="cleared; nothing inserted", lane=Lane.HOLD, ttl_s=2.0))
            qt_runtime.post(cleared)

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
        self.drain()
        # Selection is a deliberate review operation, so it owns the R form
        # even if no audio capture happens to be active.
        self.model.consume(SurfaceEvent(EventKind.CORRECTION_OPENED))
        if self.widget is not None:
            self.widget.refresh(self.model.view(), animate=False)
            self.widget.show_candidates(selection, candidates)
            self._place_widget()

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
        from samsara.ui.correction_picker_qt import offer_correction_learning
        offer_correction_learning(self.app, selection.get("word", ""), replacement)

    def _cancel_correction(self) -> None:
        self.post(SurfaceEvent(EventKind.CORRECTION_CLOSED))
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
        training = get_dictionary_store(self.app)
        values = getattr(training, "corrections_dict", None) if training is not None else None
        return values if isinstance(values, dict) else ()

    def _taught_vocabulary(self):
        training = get_dictionary_store(self.app)
        values = getattr(training, "custom_vocab", None) if training is not None else ()
        return values if isinstance(values, (tuple, list, set)) else ()


class LiveSurfaceIndicatorAdapter:
    """Old indicator call surface backed by the new controller, not a QWidget."""
    _records_feedback = True

    def __init__(self, controller: LiveSurfaceController) -> None:
        self.controller = controller
        self._lane = Lane.HOLD
        self._session_lane = None
        self._wake_armed = False
        self._capture_id = None
        self._device_lost = False
        self._interactions = {}
        self._dismissals = {}
        controller._feedback_adapter = self

    def set_mode(self, text: str) -> None:
        lower = str(text).lower()
        self._lane = (Lane.AVA if "ava" in lower else Lane.COMMAND if "command" in lower
                      else Lane.CONTINUOUS if "continuous" in lower else Lane.TOGGLE if "toggle" in lower else
                      Lane.HANDS_FREE_DICTATE if "wake" in lower or "dictate" in lower else Lane.HOLD)
        if self._session_lane is None:
            self.controller.post(SurfaceEvent(EventKind.LANE_CHANGED, lane=self._lane))

    def set_session_mode(self, name, _color=None) -> None:
        lower = str(name or "").lower()
        self._session_lane = (Lane.AVA if "ava" in lower else
                              Lane.COMMAND if "command" in lower else
                              Lane.HANDS_FREE_DICTATE) if name else None
        self.controller.idle_capture = (CaptureState.HANDS_FREE_LISTENING if name
                                       else CaptureState.OFF)
        self.controller.idle_lane = self._session_lane or self._lane
        self.clear_outcome()
        self.controller.post(SurfaceEvent(EventKind.LANE_CHANGED,
                                         lane=self._session_lane or self._lane,
                                         capture=self.controller.idle_capture
                                         if self.controller._active_capture is None else None))
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
        # Wake availability belongs to its toggle, not the activity surface.
        # Only entering a real session may advertise hands-free listening.
        self._wake_armed = armed

    def set_agent_waiting(self, label: str, active: bool) -> None:
        if active:
            self.show_feedback(label, "accent", None,
                               Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, "hermes"))
        else:
            self.clear_feedback("hermes")

    def set_snoozed(self, snoozed: bool) -> None:
        self.controller.post(SurfaceEvent(EventKind.SNOOZE_CHANGED, snoozed=bool(snoozed)))

    def flash_wake(self) -> None:
        self.controller.notice(NoticeKind.RESULT, "Wake phrase heard")

    def set_thinking(self, active: bool, label=None, *, owner=None) -> None:
        previous_owner = getattr(self, "_ava_wait_owner", None)
        self._ava_wait_owner = owner if active else None
        if (active == getattr(self, "_thinking", False)
                and (not active or (label or "Ava") == self._thinking_label)
                and previous_owner is owner):
            return
        self._thinking = active
        if active:
            self._thinking_label = label or "Ava"
            self._thinking_owner = "ava" if owner is not None else "thinking"
            self.show_feedback(self._thinking_label + " thinking", "accent", None,
                               Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING, self._thinking_owner))
        else:
            self.clear_feedback(self._thinking_owner)

    def show_outcome(self, label: str, kind: str, ttl_ms=DEFAULT_TTL_MS) -> None:
        self.show_feedback(label, kind, ttl_ms, feedback_for(kind))

    def show_feedback(self, label: str, kind: str, ttl_ms, feedback: Feedback) -> None:
        from samsara.bug_report.coordinator import remember
        app = getattr(self.controller, "app", None)
        if app is not None:
            remember(app, label, kind)
        self.reconcile_interactions(queued=True)
        if not label:
            self.clear_feedback(feedback.owner)
            return
        if kind == "live":
            return  # Capture already owns the recording indication and token.
        notice = {FeedbackKind.SUCCESS: NoticeKind.SUCCESS, FeedbackKind.WARNING: NoticeKind.WARNING,
                  FeedbackKind.ERROR: NoticeKind.ERROR, FeedbackKind.INFORMATION: NoticeKind.INFORMATION,
                  FeedbackKind.QUESTION: NoticeKind.PENDING, FeedbackKind.RUNNING: NoticeKind.RUNNING}[feedback.kind]
        if feedback.alive is not None:
            if not feedback.alive():
                return  # The producer resolved before this queued handoff.
            self._interactions[feedback.owner] = feedback.alive
        if feedback.dismiss is not None:
            self._dismissals[feedback.owner] = feedback.dismiss
        self.controller.post(SurfaceEvent(EventKind.NOTICE, notice=notice,
            message=str(label), ttl_s=None if ttl_ms is None else float(ttl_ms) / 1000,
            persistent=ttl_ms is None, feedback=feedback.kind, purpose=feedback.purpose,
            owner=feedback.owner, resolves=feedback.resolves))

    def clear_feedback(self, owner: str) -> None:
        self.controller.post(SurfaceEvent(EventKind.NOTICE_CLEARED, owner=owner))
        self._interactions.pop(owner, None)
        self._dismissals.pop(owner, None)

    def dismiss_question(self) -> bool:
        owner = self.controller.view().notice.owner
        dismiss = self._dismissals.get(owner)
        if dismiss is None or not dismiss():
            return False
        self.show_feedback("Cancelled", "warning", DEFAULT_TTL_MS, Feedback(FeedbackKind.WARNING))
        return True

    def reconcile_interactions(self, *, queued: bool = False) -> None:
        for owner, alive in tuple(self._interactions.items()):
            if not alive():
                event = SurfaceEvent(EventKind.NOTICE_CLEARED, owner=owner)
                if queued:
                    self.controller.post(event)
                else:
                    self.controller.model.consume(event)
                self._interactions.pop(owner, None)
                self._dismissals.pop(owner, None)

    def set_device_lost(self, lost: bool) -> None:
        lost = bool(lost)
        if lost == self._device_lost:
            return
        self._device_lost = lost
        self.controller.post(SurfaceEvent(EventKind.DEVICE_CHANGED, device_lost=lost))

    def clear_outcome(self) -> None:
        self.clear_feedback("")

    def flash_success(self) -> None:
        self.show_outcome("Captured", "success", 1000)

    def flash_error(self) -> None:
        self.show_outcome("Recording failed", "error", 2000)

    def show(self) -> None:
        if self.controller.widget is not None:
            self.controller.widget.show()

    def hide(self) -> None:
        # Idle visibility and legacy teardown cannot hide unsent words.
        view = self.controller.view()
        if (self._session_lane is None and self.controller.widget is not None
                and view.notice.kind is NoticeKind.NONE and not view.text):
            self.controller.widget.hide()

    def destroy(self) -> None:
        self.controller.close()

    def __getattr__(self, _name: str):
        """Legacy decoration/placement calls are deliberately harmless."""
        return lambda *args, **kwargs: None
