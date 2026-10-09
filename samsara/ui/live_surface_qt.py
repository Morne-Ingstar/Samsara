"""The one passive, accessible presentation of a live-surface view.

The widget stores only view position and a correction selection.  Draft text and
all edits remain with the revisioned document owned by ``SessionModeManager``.
"""
from __future__ import annotations

from html import escape
from math import ceil
import re
from time import monotonic
from typing import Any, Callable

from PySide6.QtCore import QEvent, QEasingCurve, QRect, QRectF, QPropertyAnimation, QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QCloseEvent, QColor, QFont, QImage, QKeyEvent, QMouseEvent, QPainter,
                           QPaintEvent, QPainterPath, QPen, QRegion)
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTextBrowser, QWidget, QSizePolicy

from samsara.live_surface.model import (CaptureState, DocumentState, NoticeKind,
                                        PresentationState, SurfaceView, VisibleForm)
from samsara.ui import theme
from samsara.live_surface.feedback import (BAND_HEIGHT_DIP, FILL_TOKENS, KIND_WORDS,
                                           FeedbackKind, paint_glyph)
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer, system_reduced_motion
from samsara.ui.tray_qt import paint_mark


MARK_SIZE = 44
MARK_ART_SIZE = 34
COMPACT_WIDTH = 152
CONNECTOR = 4
CARD_RADIUS = 12
CARD_INSET = 8
HEADER_HEIGHT = 44
REGION_GAP = 8
STATUS_SIZE = (360, 132)
LIVE_SIZE = (500, 240)
REVIEW_SIZE = (500, 360)
RESIZE_DURATION_MS = 120
MARK_TWEEN_S = 0.30
MARK_TURN_S = {"listening": 3.0, "recording": 2.4, "transcribing": 2.4}
_WORD = re.compile(r"[\w]+(?:['’\-][\w]+)*", re.UNICODE)


def _view_from(source: Any) -> SurfaceView:
    candidate = source.view() if callable(getattr(source, "view", None)) else source
    if not isinstance(candidate, SurfaceView):
        raise TypeError("LiveSurfaceWidget needs a LiveSurfaceModel/controller or SurfaceView")
    return candidate


def _capture_mark(view: SurfaceView) -> tuple[str, str]:
    if view.snoozed:
        return "idle", "asleep"
    capture = {
        CaptureState.OFF: "idle", CaptureState.WAKE_ARMED: "listening",
        CaptureState.HANDS_FREE_LISTENING: "listening", CaptureState.RECORDING: "recording",
        CaptureState.TRANSCRIBING: "idle", CaptureState.UNAVAILABLE: "idle",
    }[view.capture]
    eye = "armed" if view.capture in (CaptureState.WAKE_ARMED,
                                       CaptureState.HANDS_FREE_LISTENING) else "asleep"
    return capture, eye


def _mark_mode(view: SurfaceView) -> str:
    if view.snoozed:
        return "idle"
    if view.capture is CaptureState.RECORDING:
        return "recording"
    if view.capture is CaptureState.TRANSCRIBING:
        return "transcribing"
    if view.text and view.document in (DocumentState.EDITABLE_DRAFT, DocumentState.PARKED_DRAFT):
        return "staged"
    if view.capture in (CaptureState.WAKE_ARMED, CaptureState.HANDS_FREE_LISTENING):
        return "listening"
    return "idle"


def _mark_color(mode: str) -> QColor:
    # These tokens follow the brand vocabulary in tray_qt without changing tray painting.
    return theme.qcolor({"idle": theme.ICON_IDLE, "listening": theme.ACCENT,
                         "recording": theme.RECORDING, "transcribing": theme.AVA,
                         "staged": theme.ACCENT}[mode])


def _blend_color(start: QColor, end: QColor, fraction: float) -> QColor:
    t = max(0.0, min(1.0, fraction))
    return QColor.fromRgb(*(round(a + (b - a) * t) for a, b in
                            zip(start.getRgb()[:3], end.getRgb()[:3])))


def render_device_mark(capture: str, eye: str, *, dpr: float,
                       rotation: float = 0.0, color: QColor | None = None) -> QImage:
    pixels = max(1, round(MARK_ART_SIZE * dpr))
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        paint_mark(painter, QRectF(0, 0, pixels, pixels), capture, eye, rotation,
                   small_max=None, brand=True)
        # Recolour the existing ring and eye silhouette on this surface only.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(image.rect(), color or theme.qcolor(
            theme.RECORDING if capture == "recording" else theme.ICON_IDLE))
    finally:
        painter.end()
    image.setDevicePixelRatio(dpr)
    return image


class _Mark(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.capture, self.eye, self.rotation = "off", "closed", 0.0
        self.color = theme.qcolor(theme.ICON_IDLE)
        self.setAccessibleName("Microphone state")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_state(self, capture: str, eye: str, rotation: float, color: QColor) -> None:
        self.capture, self.eye, self.rotation, self.color = capture, eye, rotation, QColor(color)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        image = render_device_mark(self.capture, self.eye, dpr=self.devicePixelRatioF(),
                                   rotation=self.rotation, color=self.color)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(theme.qcolor(theme.TEXT_SECONDARY), 1))
            painter.setBrush(theme.qcolor(theme.BG1))
            painter.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
            painter.drawImage(QRectF((self.width() - MARK_ART_SIZE) / 2,
                                     (self.height() - MARK_ART_SIZE) / 2,
                                     MARK_ART_SIZE, MARK_ART_SIZE), image,
                              QRectF(0, 0, image.width(), image.height()))
        finally:
            painter.end()


class _FeedbackBand(QWidget):
    def __init__(self, parent, name):
        super().__init__(parent)
        self.notice = None
        self.setAccessibleName("Live surface feedback")
        self.label = QLabel(self)
        self.label.setObjectName(name)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.label.setFont(theme.qfont(theme.TYPE_MIN, weight=QFont.Weight.Bold))
        self.label.setAccessibleName("Live surface feedback")

    def set_notice(self, notice):
        self.notice = notice
        from samsara.ui.result_message_rendering import set_result_label
        set_result_label(self.label, notice.message if notice else "", self.ink.name() if hasattr(self, "ink") else theme.TEXT_PRIMARY)
        description = (KIND_WORDS[notice.feedback] + ": " + notice.message) if notice else ""
        self.setAccessibleDescription(description)
        self.label.setAccessibleDescription(description)

    def required_height(self, width):
        return max(BAND_HEIGHT_DIP, self.label.heightForWidth(max(1, width-52)) + 16)

    def restyle(self):
        kind = self.notice.feedback if self.notice else FeedbackKind.INFORMATION
        from samsara.result_messages import is_ava_chain_result
        fill = theme.AVA if self.notice and is_ava_chain_result(self.notice.message) else getattr(theme, FILL_TOKENS[kind])
        self.ink = theme.qcolor(theme.ink_on(fill))
        self.fill = theme.qcolor(fill)
        self.label.setStyleSheet(f"QLabel#{self.label.objectName()} {{ color: {self.ink.name()}; font-size: {theme.TYPE_MIN}px; font-weight: 700; background: transparent; border: 0; }}")
        if self.notice:
            self.set_notice(self.notice)
        self.update()

    def resizeEvent(self, event):
        self.label.setGeometry(44, 8, max(1, self.width()-52), max(1, self.height()-16))
        super().resizeEvent(event)

    def paintEvent(self, event):
        del event
        if not self.notice or not self.notice.message:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(self.fill)
            painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
            paint_glyph(painter, self.notice.feedback, QRectF(8, (self.height()-28)/2, 28, 28), self.ink)
        finally:
            painter.end()


class LiveSurfaceWidget(QWidget):
    """One shaped HWND for M/D/S/L/R with review kept inside the same card."""

    commit_requested = Signal()
    clear_requested = Signal()
    pause_requested = Signal()
    correct_word_requested = Signal(object)
    correction_apply_requested = Signal(object, str)
    correction_cancel_requested = Signal()
    edit_receipt_requested = Signal(str)
    scroll_requested = Signal(int)
    collapse_requested = Signal()
    move_requested = Signal(object)
    show_requested = Signal()

    def __init__(self, controller: Any, *, reduced_motion: bool | None = None,
                 motion_clock: Callable[[], float] = monotonic) -> None:
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowStaysOnTopHint)
        super().__init__(None, flags)
        self._controller, self._view = controller, _view_from(controller)
        self._motion_override = reduced_motion
        self._reduced_motion = system_reduced_motion() if reduced_motion is None else reduced_motion
        self._rotation, self._animation = 0.0, None
        self._motion_clock = motion_clock
        self._last_mark_time: float | None = None
        self._color = _mark_color(_mark_mode(self._view))
        self._color_from = QColor(self._color)
        self._color_to = QColor(self._color)
        self._color_started = motion_clock()
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(33)
        self._spin_timer.timeout.connect(self._advance_mark)
        self._motion_timer = QTimer(self)
        self._motion_timer.setInterval(1000)
        self._motion_timer.timeout.connect(self._check_motion_preference)
        self._announcer = LiveSurfaceAnnouncer(self)
        self._follow_latest, self._unread_segments = True, 0
        self._scroll_position = 0
        self._word_targets: list[dict[str, object]] = []
        self._selection: dict[str, object] | None = None
        self._picker_active = False
        self._candidates: list[object] = []
        self._sent_receipts: list[str] = []
        self._picker_page = self._candidate_page = 0
        self._editor_mode = ""
        self._dragging = False
        self._drag_offset = None
        self._drag_restore = QRect()
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Samsara live surface")
        self.setAccessibleDescription("Passive microphone and draft status")

        self._mark = _Mark(self)
        self._mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._lane = QLabel(self); self._lane.setObjectName("surfaceLane")
        self._lane.setAccessibleName("Live surface lane")
        self._band = _FeedbackBand(self, "surfaceFeedback")
        self._secondary_band = _FeedbackBand(self, "surfaceFailure")
        self._band_height = BAND_HEIGHT_DIP
        self._state = QLabel(self); self._state.setObjectName("surfaceState"); self._state.setWordWrap(True)
        self._state.setAccessibleName("Live surface state")
        self._correction_hint = QLabel(self); self._correction_hint.setWordWrap(True)
        self._correction_hint.setAccessibleName("Correction choices")
        self._transcript = QTextBrowser(self)
        self._transcript.setOpenLinks(False); self._transcript.setOpenExternalLinks(False)
        self._transcript.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._transcript.setAccessibleName("Draft transcript")
        self._transcript.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._transcript.anchorClicked.connect(self._anchor_clicked)
        bar = self._transcript.verticalScrollBar()
        # valueChanged also fires during Qt's deferred document layout. Track
        # explicit scroll actions, including wheel/trackpad input, instead.
        bar.sliderMoved.connect(self._scroll_value_changed)
        bar.actionTriggered.connect(lambda _: self._scroll_value_changed(bar.sliderPosition()))
        # Qt can reset the scroll position after emitting rangeChanged during
        # layout. Follow after that layout completes, not inside its callback.
        bar.rangeChanged.connect(lambda *_: QTimer.singleShot(0, self, self._restore_transcript_scroll))
        bar.installEventFilter(self)
        self._transcript.viewport().installEventFilter(self)
        self._provisional = QLabel(self); self._provisional.setWordWrap(True)
        self._provisional.setObjectName("surfaceProvisional")
        self._provisional.setAccessibleName("Provisional transcript")
        self._badge = QLabel(self); self._badge.setText("▣  Draft")
        self._badge.setAccessibleName("Draft pending")
        self._latest = self._button("Read the latest draft text", "Latest", self._latest_clicked)
        self._latest.setAccessibleDescription("Resume following newly settled draft text")
        self._clear = self._button("Clear draft", "Clear", self.clear_requested.emit)
        self._clear.setObjectName("quietClear")
        self._insert = self._button("Insert waiting text", "Insert text", self.commit_requested.emit)
        self._pause = self._button("Pause capture", "Pause", self.pause_requested.emit)
        self._correct = self._button("Correct a word", "Correct", self._request_correct)
        self._edit_receipt = self._button("Edit sent receipt as a new draft", "Edit as new draft", self._request_edit_receipt)
        self._collapse = self._button("Collapse surface", "Collapse", self.collapse_requested.emit)
        self._controls = (self._clear, self._pause, self._correct, self._edit_receipt, self._collapse)
        self._picker_buttons = [self._button("Draft word", "", lambda n=n: self._choose_picker(n))
                                for n in range(4)]
        self._candidate_buttons = [self._button("Correction candidate", "", lambda n=n: self._choose_candidate(n))
                                   for n in range(4)]
        self._keep = self._button("Keep original word", "Keep original", self._cancel_correction)
        self._spell = self._button("Spell replacement", "Spell", lambda: self._open_editor("spell"))
        self._edit = self._button("Edit replacement", "Edit", lambda: self._open_editor("edit"))
        self._more = self._button("More correction choices", "More", self._more_clicked)
        self._apply = self._button("Apply replacement", "Apply", self._apply_editor)
        self._cancel = self._button("Cancel replacement", "Cancel", self._cancel_editor)
        self._editor = QLineEdit(self); self._editor.setAccessibleName("Replacement text")
        self._editor.setPlaceholderText("Say letters, or type a replacement")
        self._editor.setMaxLength(120)
        self._editor.textChanged.connect(self._sanitize_spelling)
        self._apply_view(self._view, animate=False)
        self._expiry_timer = QTimer(self)
        self._expiry_timer.setInterval(100)
        expire = getattr(controller, "refresh_expired", None)
        if callable(expire):
            self._expiry_timer.timeout.connect(expire)
            self._expiry_timer.start()

    def _button(self, name: str, label: str, callback) -> QPushButton:
        button = QPushButton(label, self); button.setAccessibleName(name)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus); button.setMinimumHeight(MARK_SIZE)
        button.clicked.connect(callback)
        return button

    @property
    def view(self) -> SurfaceView: return self._view
    @property
    def announcer(self) -> LiveSurfaceAnnouncer: return self._announcer
    @property
    def mark_rect(self) -> QRect: return self._mark.geometry()
    @property
    def is_dragging(self) -> bool: return self._dragging
    @property
    def card_rect(self) -> QRect:
        return self.rect()
    @property
    def card_content_rect(self) -> QRect: return self.card_rect.adjusted(CARD_INSET, CARD_INSET, -CARD_INSET, -CARD_INSET)
    def hit_region(self) -> QRegion: return self.mask()

    def apply_placement(self, placed) -> None:
        """Apply controller-owned, already-clamped geometry without focus."""
        if self._dragging:
            return
        if self._animation is not None:
            self._animation.stop()
        bounds = placed.bounds
        self.setGeometry(round(bounds.x), round(bounds.y), round(bounds.width), round(bounds.height))
        self._layout_children(); self._update_mask()

    def refresh(self, view: SurfaceView | None = None, *, animate: bool = True) -> None:
        self._apply_view(_view_from(self._controller) if view is None else view, animate=animate)

    def set_reduced_motion(self, enabled: bool) -> None:
        self._motion_override = enabled
        self._apply_motion_preference(enabled)

    def _apply_motion_preference(self, enabled: bool) -> None:
        if self._reduced_motion == enabled:
            return
        self._reduced_motion = enabled
        if enabled and self._animation is not None: self._animation.stop(); self._animation = None
        self._update_mark()

    def _check_motion_preference(self) -> None:
        if self._motion_override is None:
            self._apply_motion_preference(system_reduced_motion())

    def _update_mark(self) -> None:
        now = self._motion_clock()
        mode = _mark_mode(self._view)
        target = _mark_color(mode)
        if self._reduced_motion:
            self._color = target
            self._color_from = QColor(target)
            self._color_to = QColor(target)
            self._rotation = 0.0
            self._spin_timer.stop()
        elif target != self._color_to:
            self._color_from = QColor(self._color)
            self._color_to = target
            self._color_started = now
        capture, eye = _capture_mark(self._view)
        self._mark.set_state(capture, eye, self._rotation, self._color)
        if self.isVisible() and not self._reduced_motion and (mode in MARK_TURN_S or self._color != self._color_to):
            if not self._spin_timer.isActive(): self._spin_timer.start()
        else:
            self._spin_timer.stop()

    def _advance_mark(self) -> None:
        if self._reduced_motion or not self.isVisible():
            return
        now = self._motion_clock()
        mode = _mark_mode(self._view)
        if self._last_mark_time is not None and mode in MARK_TURN_S:
            elapsed = max(0.0, now - self._last_mark_time)
            self._rotation = (self._rotation + elapsed * 360.0 / MARK_TURN_S[mode]) % 360.0
        self._last_mark_time = now
        self._color = _blend_color(self._color_from, self._color_to,
                                   (now - self._color_started) / MARK_TWEEN_S)
        capture, eye = _capture_mark(self._view)
        self._mark.set_state(capture, eye, self._rotation, self._color)
        if mode not in MARK_TURN_S and self._color == self._color_to:
            self._spin_timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._last_mark_time = self._motion_clock()
        self._motion_timer.start()
        self._update_mark()

    def hideEvent(self, event) -> None:
        self._spin_timer.stop()
        self._motion_timer.stop()
        self._last_mark_time = None
        super().hideEvent(event)

    def _display_form(self) -> VisibleForm:
        # Correction is local UI intent; queued producer updates must never
        # leave its header visible without the body or a way to dismiss it.
        return VisibleForm.REVIEW if self._selection is not None or self._picker_active else self._view.form

    def focus_review(self) -> None:
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False); self.show()
        for child in (*self.findChildren(QPushButton), *self.findChildren(QLineEdit),
                      *self.findChildren(QTextBrowser)):
            child.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._transcript.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _restore_passive(self) -> None:
        if self.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus:
            return
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        for child in (*self.findChildren(QPushButton), self._editor, self._transcript):
            child.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if visible: self.show()

    def scroll_draft(self, where: str) -> None:
        """Use the same draft-scoped scroll route as existing voice phrases."""
        bar = self._transcript.verticalScrollBar()
        step = max(1, self._transcript.viewport().height() - MARK_SIZE)
        if where == "top":
            bar.setValue(bar.minimum())
        elif where == "bottom":
            self._latest_clicked()
            return
        elif where == "up":
            bar.setValue(max(bar.minimum(), bar.value() - step))
        elif where == "down":
            bar.setValue(min(bar.maximum(), bar.value() + step))
        else:
            return
        self._scroll_position = bar.value()
        self._follow_latest = bar.value() >= bar.maximum()
        self.scroll_requested.emit(bar.value())
        self._layout_children()

    def voice_review_action(self, action: str, value: object) -> None:
        """Deliver a routed, whole-utterance review-picker request on Qt."""
        if action == "correct":
            needle = str(value).casefold()
            self.show_word_picker()
            for target in self._word_targets:
                if str(target["word"]).casefold() == needle:
                    self.correct_word_requested.emit(dict(target))
                    return
            self._announce("word-missing", "That word is no longer in the draft")
        elif action == "word":
            self.show_word_picker()
            index = int(value) - 1
            if 0 <= index < len(self._word_targets):
                self.correct_word_requested.emit(dict(self._word_targets[index]))
            else:
                self._announce("word-missing", "That word number is not in the draft")

    def show_word_picker(self) -> None:
        self._editor_mode = ""
        self._selection = None; self._picker_active = True; self._picker_page = 0; self._candidates = []
        self._apply_view(self._view, animate=False); self._announce("picker", "Choose a word to correct")

    def show_candidates(self, selection: dict[str, object], candidates: list[object]) -> None:
        self._editor_mode = ""
        self._selection, self._picker_active, self._candidates, self._candidate_page = dict(selection), False, list(candidates)[:12], 0
        self._apply_view(self._view, animate=False); self._announce(("correction", selection.get("revision")), "Correction choices ready")

    def correction_refused(self, message: str = "That word changed. Select it again.") -> None:
        self._editor_mode = ""
        self._selection = None; self._picker_active = False; self._candidates = []
        self._restore_passive()
        self._apply_view(self._view, animate=False); self._announce("stale", message)

    def _apply_view(self, view: SurfaceView, *, animate: bool) -> None:
        # setHtml(), font changes and resize all move the scrollbar. Only an
        # actual reader scroll should stop following incoming text.
        bar = self._transcript.verticalScrollBar()
        position = bar.value()
        self._scroll_position = position
        with QSignalBlocker(bar):
            self._apply_view_content(view, animate=animate)
            bar.setValue(bar.maximum() if self._follow_latest else position)
        # Layout signals were blocked above; always restore after deferred Qt
        # document/font layout, including when rangeChanged was suppressed.
        QTimer.singleShot(0, self, self._restore_transcript_scroll)

    def _apply_view_content(self, view: SurfaceView, *, animate: bool) -> None:
        self._advance_mark()  # settle the previous state before a new colour target
        previous, previous_text = self._view, self._view.text
        new_utterance = ((view.capture_id is not None and view.capture_id != previous.capture_id)
                         or view.capture is CaptureState.RECORDING and previous.capture is not CaptureState.RECORDING)
        if new_utterance or view.document_id != previous.document_id or not (view.text or view.provisional_text):
            self._follow_latest, self._unread_segments = True, 0
        if self._selection is not None and (view.document_id != self._selection.get("document_id")
                or view.revision != self._selection.get("revision")):
            self._selection = None; self._candidates = []; self._editor_mode = ""
            self._restore_passive()
            self.correction_cancel_requested.emit()
        self._view = view
        if (view.document is DocumentState.DELIVERED and previous.document is not DocumentState.DELIVERED
                and view.text):
            self._sent_receipts.append(view.text)
            while len(self._sent_receipts) > 20 or sum(map(len, self._sent_receipts)) > 10_000:
                self._sent_receipts.pop(0)
        self._update_mark()
        state = self._state_text(view)
        self._state.setText(state); self._lane.setText(self._lane_text(view)); self._render_transcript()
        self._lane.setAccessibleDescription(self._lane.text())
        self._band.set_notice(view.notice if view.notice.message else None)
        self._secondary_band.set_notice(view.secondary_notice)
        self._band.restyle(); self._secondary_band.restyle()
        self._state.setAccessibleDescription(state)
        self.setAccessibleDescription("; ".join(part for part in (
            self._lane.text(), state, self._band.accessibleDescription(),
            self._secondary_band.accessibleDescription(), view.document.value.replace("_", " ")) if part))
        self._provisional.setText(f"⋯ {view.provisional_text}" if view.provisional_text else "")
        self._badge.setText("Tap Insert text to send.")
        previous_content = (previous_text, previous.provisional_text)
        if previous_content != (view.text, view.provisional_text) and any(previous_content) and not self._follow_latest:
            self._unread_segments += 1
        target = self._form_rect(self._display_form())
        if self._dragging:
            return
        if animate and self.isVisible() and not self._reduced_motion and self.geometry().size() != target.size(): self._animate_geometry(target)
        else: self.resize(target.size()); self._layout_children(); self._update_mask()
        self._restyle(); self._announce_transition(previous, view); self.update()
        if self._follow_latest: self._scroll_to_latest()

    def _segments(self) -> list[tuple[str, str]]:
        snapshot = getattr(self._controller, "document_snapshot", None)
        if callable(snapshot):
            value = snapshot()
            if value and value.get("document_id") == self._view.document_id and value.get("revision") == self._view.revision:
                return [(str(a), str(b)) for a, b in value.get("segments", ())]
        return [("rendered", self._view.text)] if self._view.text else []

    def _render_transcript(self) -> None:
        self._word_targets = []
        occurrence: dict[str, int] = {}
        parts: list[str] = []
        for segment_id, text in self._segments():
            at = 0
            for match in _WORD.finditer(text):
                parts.append(escape(text[at:match.start()]))
                word = match.group(0); index = len(self._word_targets); folded = word.casefold()
                occurrence[folded] = occurrence.get(folded, 0) + 1
                self._word_targets.append({"document_id": self._view.document_id, "revision": self._view.revision,
                                           "segment_id": segment_id, "start": match.start(), "end": match.end(),
                                           "word": word, "occurrence": occurrence[folded] - 1})
                parts.append(f'<a href="word:{index}">{escape(word)}</a>'); at = match.end()
            parts.append(escape(text[at:]))
        if self._view.provisional_text:
            if self._view.text:
                parts.append("<br/>")
            parts.append(f'<span style="color: {theme.TEXT_SECONDARY}; font-style: italic;">⋯ '
                         + escape(self._view.provisional_text) + '</span>')
        self._transcript.setHtml(
            f"<style>body,a {{ color: {theme.TEXT_PRIMARY}; text-decoration: none; }}</style><p>"
            + "".join(parts).replace("\n", "<br/>") + "</p>")

    def _form_rect(self, form: VisibleForm) -> QRect:
        if form is VisibleForm.MARK: return QRect(self.x(), self.y(), COMPACT_WIDTH, MARK_SIZE)
        if form is VisibleForm.DRAFT_BADGE: return QRect(self.x(), self.y(), LIVE_SIZE[0], 112)
        width = STATUS_SIZE[0] if form is VisibleForm.STATUS else LIVE_SIZE[0]
        self._band_height = self._band.required_height(width-2*CARD_INSET)
        if self._view.secondary_notice:
            self._band_height += REGION_GAP + self._secondary_band.required_height(width-2*CARD_INSET)
        if form is VisibleForm.STATUS:
            return QRect(self.x(), self.y(), STATUS_SIZE[0], 76+self._band_height)
        width = LIVE_SIZE[0] if form is VisibleForm.LIVE else REVIEW_SIZE[0]
        if self._selection is not None or self._picker_active:
            source = self._candidates if self._selection is not None else self._word_targets
            page = self._candidate_page if self._selection is not None else self._picker_page
            rows = 1 if self._editor_mode else ceil(min(4, max(0, len(source)-page*4))/2)
            height = 2*CARD_INSET + 2*(MARK_SIZE+REGION_GAP) + rows*(MARK_SIZE+REGION_GAP) + MARK_SIZE
        else:
            # A live panel grows for content, but does not retain a blank
            # review-sized slab for a one-line draft.
            chars_per_line = 50
            lines = max(1, ceil((len(self._view.text) + len(self._view.provisional_text)) / chars_per_line))
            height = 64 + min(10, lines) * 24 + (24 if self._view.provisional_text else 0) + 20
            height = max(132, min(REVIEW_SIZE[1], height))
        return QRect(self.x(), self.y(), width, height + self._band_height + 20)

    def _animate_geometry(self, target: QRect) -> None:
        if self._animation is not None:
            self._animation.stop()
            self._animation.deleteLater()
        animation = QPropertyAnimation(self, b"geometry", self); animation.setDuration(RESIZE_DURATION_MS)
        animation.setStartValue(self.geometry()); animation.setEndValue(target); animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(lambda _: (self._layout_children(), self._update_mask()))
        animation.finished.connect(lambda: (self._layout_children(), self._update_mask()))
        self._animation = animation; animation.start()

    def _hide_children(self) -> None:
        for child in (self._lane, self._band, self._secondary_band, self._state, self._correction_hint, self._transcript, self._provisional, self._badge, self._latest, self._insert,
                      *self._controls, *self._picker_buttons, *self._candidate_buttons,
                      self._keep, self._spell, self._edit, self._more, self._apply, self._cancel, self._editor): child.hide()

    def _layout_children(self) -> None:
        bar = self._transcript.verticalScrollBar()
        position = bar.value()
        with QSignalBlocker(bar):
            self._layout_content()
            if self._view.capture is CaptureState.UNAVAILABLE:
                state = self._state.geometry()
                self._state.setGeometry(state.x()+26, state.y(), max(1, state.width()-26), state.height())
            bar.setValue(bar.maximum() if self._follow_latest else position)

    def _layout_content(self) -> None:
        form = self._display_form(); self._mark.setGeometry(0, 0, MARK_SIZE, MARK_SIZE); self._hide_children()
        if form is VisibleForm.MARK:
            self._state.setGeometry(MARK_SIZE+REGION_GAP, 0, self.width()-MARK_SIZE-2*REGION_GAP, MARK_SIZE)
            self._state.show(); return
        if form is VisibleForm.DRAFT_BADGE:
            self._mark.setGeometry(8, 8, MARK_SIZE, MARK_SIZE)
            self._state.setGeometry(64, 8, self.width()-152, MARK_SIZE); self._state.show()
            self._clear.setGeometry(self.width()-72, 8, 64, MARK_SIZE); self._clear.show()
            self._badge.setGeometry(16, 60, self.width()-164, MARK_SIZE); self._badge.show()
            self._insert.setGeometry(self.width()-140, 60, 132, MARK_SIZE); self._insert.show(); return
        content = self.card_content_rect
        self._mark.setGeometry(content.x(), content.y(), MARK_SIZE, MARK_SIZE)
        state_x = content.x()+MARK_SIZE+12
        self._lane.setGeometry(state_x, content.y(), content.width()-MARK_SIZE-12-72, 24)
        self._state.setGeometry(state_x, content.y()+24, content.width()-MARK_SIZE-12-72, 24)
        self._lane.show(); self._state.show()
        band_y = content.y()+HEADER_HEIGHT+12
        primary_height = self._band.required_height(content.width())
        self._band.setGeometry(content.x(), band_y, content.width(), primary_height)
        self._band.show()
        if self._view.secondary_notice:
            self._secondary_band.setGeometry(content.x(), band_y+primary_height+REGION_GAP,
                content.width(), self._secondary_band.required_height(content.width()))
            self._secondary_band.show()
        if form is VisibleForm.STATUS:
            clearable = self._view.capture is CaptureState.RECORDING or self._view.notice.kind in (
                NoticeKind.ERROR, NoticeKind.CONFIRMATION, NoticeKind.ALIAS_OFFER)
            if clearable:
                self._clear.setGeometry(content.right()-63, content.y(), 64, MARK_SIZE); self._clear.show()
            return
        if form is VisibleForm.REVIEW and (self._selection is not None or self._picker_active):
            self._layout_correction(content); return
        # Reserve each action's space once. Pending-text recovery is separate
        # from ordinary capture, whose final is inserted automatically.
        actions = [(self._clear, 64)]
        if self._view.document is DocumentState.PARKED_DRAFT and self._view.text:
            actions.insert(0, (self._insert, 132))
        if not self._follow_latest or self._unread_segments:
            self._latest.setText("Latest" + (f" (+{self._unread_segments})" if self._unread_segments else ""))
            actions.insert(0, (self._latest, 104))
        right = content.right()+1
        for button, width in reversed(actions):
            right -= width
            button.setGeometry(right, content.y(), width, MARK_SIZE); button.show()
            right -= REGION_GAP
        state_x = content.x()+MARK_SIZE+12
        self._lane.setGeometry(state_x, content.y(), max(1, right-state_x), 24)
        self._state.setGeometry(state_x, content.y()+24, max(1, right-state_x), 24)
        y = content.y()+HEADER_HEIGHT+REGION_GAP+self._band_height+22
        available = content.bottom() - y + 1
        # Both settled and incoming words need the same scrollable viewport.
        # A capped QLabel otherwise clips long speech below a retained draft.
        self._transcript.setGeometry(content.x(), y, content.width(), available)
        self._transcript.show()

    def _layout_row(self, controls, x: int, y: int, width: int) -> None:
        available = width - REGION_GAP*(len(controls)-1); base, rem = divmod(available, len(controls))
        for index, control in enumerate(controls):
            w=base+(1 if index<rem else 0); control.setGeometry(x,y,w,MARK_SIZE); control.show(); x += w+REGION_GAP

    def _layout_correction(self, content: QRect) -> None:
        y=content.y()+HEADER_HEIGHT+REGION_GAP+self._band_height+12
        hint = (f'Correct “{self._selection.get("word", "") }” — choose a replacement'
                if self._selection is not None else "Choose a word to correct")
        if self._selection is not None and not self._candidates:
            hint = f'No suggestions for “{self._selection.get("word", "") }”. Keep original, spell or edit.'
        self._correction_hint.setText(hint)
        self._correction_hint.setGeometry(content.x(), y, content.width(), MARK_SIZE)
        self._correction_hint.show(); y += MARK_SIZE + REGION_GAP
        if self._editor_mode:
            self._editor.setGeometry(content.x(), y, content.width(), MARK_SIZE)
            self._editor.show()
            self._layout_row((self._apply, self._cancel), content.x(), y+MARK_SIZE+REGION_GAP, content.width())
            return
        selecting = self._selection is not None
        buttons = self._candidate_buttons if selecting else self._picker_buttons
        page = self._candidate_page if selecting else self._picker_page
        source = self._candidates if selecting else self._word_targets
        start=page*4
        for index, button in enumerate(buttons):
            choice = source[start+index] if start+index < len(source) else None
            if choice is None: button.hide(); continue
            label = getattr(choice, "replacement", None) if selecting else choice["word"]
            button.setText(f"{start+index+1}. {label}"); button.setAccessibleName(f"Choice {start+index+1}: {label}")
            button.setGeometry(content.x()+(index%2)*(content.width()//2+REGION_GAP//2), y+(index//2)*(MARK_SIZE+REGION_GAP), content.width()//2-REGION_GAP//2, MARK_SIZE); button.show()
        y += (MARK_SIZE+REGION_GAP) * ceil(min(4, max(0, len(source)-start))/2)
        if self._selection is not None:
            self._layout_row((self._keep,self._spell,self._edit,self._more), content.x(), y, content.width())
        else:
            self._cancel.setText("Cancel")
            self._layout_row((self._cancel,self._more), content.x(), y, content.width())
        self._more.setEnabled(len(source)>4)

    def _update_mask(self) -> None:
        if self._display_form() is VisibleForm.MARK:
            path=QPainterPath(); path.addRoundedRect(QRectF(0,0,self.width(),MARK_SIZE),CARD_RADIUS,CARD_RADIUS); region=QRegion(path.toFillPolygon().toPolygon())
        else:
            path=QPainterPath(); path.addRoundedRect(QRectF(self.card_rect),CARD_RADIUS,CARD_RADIUS); region=QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)

    def _restyle(self) -> None:
        self.setStyleSheet(f"LiveSurfaceWidget {{ background: transparent; }} QLabel,QTextBrowser {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; background: transparent; border: 0; }} QTextBrowser a {{ color: {theme.TEXT_PRIMARY}; text-decoration: none; }} QPushButton,QLineEdit {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: {CARD_RADIUS}px; font-size: {theme.TYPE_BODY}px; }} QPushButton#quietClear {{ background: transparent; border: 0; color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_MIN}px; }}")
        active = not self._view.snoozed and self._view.capture in (CaptureState.RECORDING, CaptureState.TRANSCRIBING)
        state_color = theme.ERROR if self._view.capture is CaptureState.UNAVAILABLE else theme.TEXT_PRIMARY if active else theme.TEXT_SECONDARY
        self._lane.setFont(theme.qfont(theme.TYPE_EMPHASIS, weight=QFont.Weight.Bold))
        self._lane.setStyleSheet(f"QLabel#surfaceLane {{ color: {theme.ACCENT}; font-size: {theme.TYPE_EMPHASIS}px; font-weight: 700; }}")
        self._state.setFont(theme.qfont(theme.TYPE_BODY, weight=QFont.Weight.Medium if active else QFont.Weight.Normal))
        self._state.setStyleSheet(f"QLabel#surfaceState {{ color: {state_color}; font-size: {theme.TYPE_BODY}px; font-weight: {500 if active else 400}; }}")
        self._band.restyle(); self._secondary_band.restyle()
        self._transcript.setFont(theme.qfont(theme.TYPE_BODY))
        self._provisional.setFont(theme.qfont(theme.TYPE_BODY, italic=True)); self._badge.setFont(theme.qfont(theme.TYPE_MIN))
        self._provisional.setStyleSheet(f"QLabel#surfaceProvisional {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px; font-style: italic; }}")

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        if self._display_form() is VisibleForm.MARK:
            painter=QPainter(self)
            try: painter.setPen(QPen(theme.qcolor(theme.BORDER),1)); painter.setBrush(theme.qcolor(theme.BG1)); painter.drawRoundedRect(QRectF(.5,.5,self.width()-1,MARK_SIZE-1),CARD_RADIUS,CARD_RADIUS)
            finally:painter.end()
            return
        painter=QPainter(self)
        try:
            painter.setPen(QPen(theme.qcolor(theme.BORDER),1)); painter.setBrush(theme.qcolor(theme.BG1)); painter.drawRoundedRect(QRectF(self.card_rect).adjusted(.5,.5,-.5,-.5),CARD_RADIUS,CARD_RADIUS)
            if self._display_form() in (VisibleForm.LIVE, VisibleForm.REVIEW):
                y = CARD_INSET+HEADER_HEIGHT+REGION_GAP+self._band_height+16
                painter.setPen(QPen(theme.qcolor(theme.BORDER_FAINT), 1))
                painter.drawLine(CARD_INSET, y, self.width()-CARD_INSET, y)
            if self._view.capture is CaptureState.UNAVAILABLE:
                paint_glyph(painter, FeedbackKind.ERROR, QRectF(self._state.x()-26, self._state.y(), 24, 24), theme.qcolor(theme.ERROR))
        finally:painter.end()

    def mousePressEvent(self,event: QMouseEvent)->None:
        # The mark/header is the intentional drag handle. Transcript words
        # retain their correction interaction and never start a move.
        if event.button() == Qt.MouseButton.LeftButton and self._mark.geometry().contains(event.position().toPoint()):
            if self._animation is not None:
                self._animation.stop()
            self._dragging = True; self._drag_restore = self.geometry()
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept(); return
        if self._view.form in (VisibleForm.MARK,VisibleForm.DRAFT_BADGE) and self.mask().contains(event.position().toPoint()): self.show_requested.emit(); event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            origin = self.mapToGlobal(self._mark.pos())
            self.move_requested.emit({"center": (origin.x() + MARK_SIZE / 2,
                                                  origin.y() + MARK_SIZE / 2)})
            self.refresh(animate=False)
            place = getattr(self._controller, "_place_widget", None)
            if callable(place): place()
            event.accept(); return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self,event: QKeyEvent)->None:
        if event.key()==Qt.Key.Key_Escape:
            if self._dragging:
                self._dragging=False; self.setGeometry(self._drag_restore); event.accept(); return
            if self._editor_mode:self._cancel_editor()
            elif self._selection is not None or self._picker_active:self._cancel_correction()
            else:self._transcript.setFocus()
            event.accept(); return
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier: self.commit_requested.emit(); event.accept(); return
        if event.key()==Qt.Key.Key_Delete and event.modifiers() & Qt.KeyboardModifier.AltModifier: self.clear_requested.emit(); event.accept(); return
        if event.key()==Qt.Key.Key_Pause: self.pause_requested.emit(); event.accept(); return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.Wheel and watched in
                (self._transcript.viewport(), self._transcript.verticalScrollBar())):
            self._follow_latest = False
            QTimer.singleShot(0, self, lambda: self._scroll_value_changed(
                self._transcript.verticalScrollBar().value()))
        return super().eventFilter(watched, event)

    def closeEvent(self,event:QCloseEvent)->None: self.hide(); self.collapse_requested.emit(); event.ignore()
    def _anchor_clicked(self,url:QUrl)->None:
        try:index=int(url.toString().split(":",1)[1])
        except (ValueError,IndexError):return
        if 0<=index<len(self._word_targets): self.correct_word_requested.emit(dict(self._word_targets[index]))
    def _request_correct(self)->None:
        if self._selection is not None:self.correct_word_requested.emit(dict(self._selection))
        else:self.show_word_picker()
    def _request_edit_receipt(self)->None:
        if self._view.document is DocumentState.DELIVERED and self._view.text:
            self.edit_receipt_requested.emit(self._view.text)
    def _choose_picker(self,index:int)->None:
        number=self._picker_page*4+index
        if number<len(self._word_targets):self.correct_word_requested.emit(dict(self._word_targets[number]))
    def _choose_candidate(self,index:int)->None:
        number=self._candidate_page*4+index
        if self._selection is not None and number<len(self._candidates):self.correction_apply_requested.emit(dict(self._selection),str(getattr(self._candidates[number],"replacement","")))
    def _more_clicked(self)->None:
        if self._selection is not None:self._candidate_page=(self._candidate_page+1)%max(1,ceil(len(self._candidates)/4))
        else:self._picker_page=(self._picker_page+1)%max(1,ceil(len(self._word_targets)/4))
        self._apply_view(self._view,animate=False)
    def _latest_clicked(self)->None:self._follow_latest=True; self._unread_segments=0; self._scroll_to_latest(); self._layout_children()
    def _scroll_value_changed(self,value:int)->None:
        bar=self._transcript.verticalScrollBar()
        self._scroll_position = value
        self._follow_latest = value >= bar.maximum()
        if self._follow_latest:
            self._unread_segments = 0
        QTimer.singleShot(0, self, self._layout_children)
    def _scroll_to_latest(self)->None:
        self._transcript.verticalScrollBar().setValue(self._transcript.verticalScrollBar().maximum())
    def _restore_transcript_scroll(self)->None:
        bar = self._transcript.verticalScrollBar()
        if self._follow_latest:
            self._scroll_to_latest()
        else:
            bar.setValue(self._scroll_position)
    def _open_editor(self,mode:str)->None:
        self._editor_mode=mode; self._editor.clear(); self._editor.setAccessibleName("Spelled replacement" if mode=="spell" else "Edit replacement")
        self._apply_view(self._view, animate=False)
        self.focus_review(); self._editor.setFocus(); self._announce("editor", "Enter a replacement, then apply")
    def _sanitize_spelling(self,value:str)->None:
        if self._editor_mode=="spell":
            cleaned="".join(ch for ch in value if ch.isalpha() or ch in " '-")
            if cleaned!=value:self._editor.blockSignals(True); self._editor.setText(cleaned); self._editor.blockSignals(False)
    def _apply_editor(self)->None:
        text=self._editor.text().strip()
        if text and self._selection is not None:self.correction_apply_requested.emit(dict(self._selection),text)
    def _cancel_editor(self)->None:
        if not self._editor_mode:self._cancel_correction(); return
        self._editor_mode=""; self._apply_view(self._view,animate=False)
    def _cancel_correction(self)->None:
        self._selection=None; self._picker_active=False; self._candidates=[]; self._editor_mode=""
        self._restore_passive()
        self.correction_cancel_requested.emit(); self._apply_view(self._view,animate=False)
    def _announce(self,key:object,message:str)->None:self._announcer.announce_transition(key,message)
    def _lane_text(self, view: SurfaceView) -> str:
        labels = {"hold": "Hold", "toggle": "Toggle", "hands_free_dictate": "Hands-free",
                  "continuous": "Continuous", "command": "Command", "ava": "Ava"}
        return labels.get(view.lane.value, "") if view.lane is not None else ("Hold" if view.form is not VisibleForm.MARK else "")

    def _state_text(self, view: SurfaceView) -> str:
        if view.capture is CaptureState.UNAVAILABLE: return "microphone unavailable"
        if view.snoozed: return "Snoozed"
        if view.capture is CaptureState.RECORDING: return "recording"
        if view.capture is CaptureState.TRANSCRIBING: return "transcribing"
        if view.form is VisibleForm.DRAFT_BADGE:
            words = len(view.text.split())
            return f"{words} word{'s' if words != 1 else ''} waiting"
        if view.document is DocumentState.DELIVERED: return "inserted"
        if view.text: return "staged"
        if view.capture in (CaptureState.WAKE_ARMED, CaptureState.HANDS_FREE_LISTENING): return "listening"
        return "Idle"

    def _announce_transition(self, previous: SurfaceView, current: SurfaceView) -> None:
        if previous.lane is not current.lane or previous.capture is not current.capture or previous.snoozed != current.snoozed:
            self._announce(("state", current.lane, current.capture, current.snoozed),
                           " — ".join(p for p in (self._lane_text(current), self._state_text(current)) if p))
        if current.feedback_revision != previous.feedback_revision and current.feedback_event and current.feedback_event.message:
            notice = current.feedback_event
            self._announce(("feedback", current.feedback_revision), KIND_WORDS[notice.feedback] + ": " + notice.message)
        if previous.document is not current.document and current.document is DocumentState.PARKED_DRAFT:
            self._announce(("parked", current.document_id, current.revision), "Draft parked")
