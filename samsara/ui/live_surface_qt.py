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
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QTextBrowser, QWidget, QSizePolicy, QGraphicsOpacityEffect

from samsara.live_surface.model import (CaptureState, DocumentState, Lane, NoticeKind,
                                        SurfaceView, VisibleForm)
from samsara.ui import theme
from samsara.live_surface.feedback import (FILL_TOKENS, KIND_WORDS,
                                           FeedbackKind, FeedbackPurpose, paint_glyph)
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer, system_reduced_motion
from samsara.ui.tray_qt import paint_mark
from samsara.ui.indicator_message_layout import message_lines, message_height
from samsara.draft_selection import select_range, highlighted


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
    """A content row with at most two readable lines and full hover text."""
    dismissed = Signal()
    question_dismissed = Signal()
    def __init__(self, parent, name, *, chip=False):
        super().__init__(parent)
        self.notice, self.chip = None, chip
        self.setAccessibleName("Live surface feedback")
        self.label = QLabel(self)
        self.label.setObjectName(name)
        self.label.setWordWrap(False)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.label.setFont(theme.qfont(theme.TYPE_BODY))
        self.label.setAccessibleName("Live surface feedback")
        self.dismiss = QPushButton("Dismiss", self)
        self.dismiss.setAccessibleName("Dismiss suggestion")
        self.dismiss.setToolTip("Cancel this suggestion")
        self.dismiss.clicked.connect(self.question_dismissed.emit)
        self.dismiss.hide()

    def _dismiss_width(self):
        return 76 if self.notice and self.notice.purpose is FeedbackPurpose.QUESTION and self.notice.owner.startswith("suggestion:") else 0

    def set_notice(self, notice):
        self.notice = notice
        self.dismiss.setVisible(bool(self._dismiss_width()))
        from samsara.ui.result_message_rendering import set_result_label
        message = notice.message if notice else ""
        self.setAccessibleName(message or "Live surface feedback")
        self.label.setAccessibleName(message or "Live surface feedback")
        self.setToolTip(message); self.label.setToolTip(message)
        if not self.chip and notice and notice.feedback in (FeedbackKind.ERROR, FeedbackKind.WARNING, FeedbackKind.LIVE):
            message = KIND_WORDS[notice.feedback] + ": " + message
        self._message = message
        lines, self.elided = message_lines(message, self.label.font(), max(1, self.width()-(42 if self.chip else 24)-self._dismiss_width()))
        set_result_label(self.label, '\n'.join(lines), theme.TEXT_PRIMARY)
        if self.label.textFormat() == Qt.TextFormat.RichText:
            self.label.setText(self.label.text().replace('\n', '<br>'))
        description = (KIND_WORDS[notice.feedback] + ": " + notice.message) if notice else ""
        self.setAccessibleDescription(description)
        self.label.setAccessibleDescription(description)

    def required_height(self, width):
        if not self.notice or not self.notice.message:
            return 0
        return max(36 if self._dismiss_width() else 0, message_height(self._message, self.label.font(), width-(42 if self.chip else 24)-self._dismiss_width()))

    def desired_width(self):
        return self.label.fontMetrics().horizontalAdvance(self.notice.message) + 42 + self._dismiss_width() if self.notice else 0

    def restyle(self):
        self.dismiss.setFont(theme.qfont(theme.TYPE_MIN))
        self.dismiss.setStyleSheet(f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 5px; padding: 0; font-size: {theme.TYPE_MIN}px; }} QPushButton:hover {{ border-color: {theme.ACCENT}; }}")
        kind = self.notice.feedback if self.notice else FeedbackKind.INFORMATION
        from samsara.result_messages import is_ava_chain_result
        fill = theme.AVA if self.notice and is_ava_chain_result(self.notice.message) else getattr(theme, FILL_TOKENS[kind])
        if not (self.notice and is_ava_chain_result(self.notice.message)):
            if kind is FeedbackKind.RUNNING:
                fill = theme.WARNING
            elif kind is FeedbackKind.INFORMATION:
                fill = theme.TEXT_SECONDARY
        self.ink = theme.qcolor(theme.TEXT_PRIMARY)
        self.fill = theme.qcolor(fill)
        size = theme.TYPE_BODY
        strong = kind in (FeedbackKind.ERROR, FeedbackKind.WARNING, FeedbackKind.RUNNING, FeedbackKind.QUESTION)
        font = theme.qfont(size); font.setBold(strong)
        self.label.setFont(font)
        # type-floor: exempt -- size selects only the TYPE_MIN and TYPE_BODY tokens above.
        self.label.setStyleSheet(f"QLabel#{self.label.objectName()} {{ color: {self.ink.name()}; font-size: {size}px; font-weight: {600 if strong else 400}; background: transparent; border: 0; }}")
        if self.notice:
            self.set_notice(self.notice)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dismissed.emit()
            event.accept(); return
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        self.label.setGeometry(30 if self.chip else 12, 0 if self.chip else 6,
                               max(1, self.width()-(42 if self.chip else 24)-self._dismiss_width()),
                               self.height() if self.chip else max(1, self.height()-12))
        self.dismiss.setGeometry(self.width()-76, max(0, (self.height()-28)//2), 70, 28)
        self.set_notice(self.notice)
        super().resizeEvent(event)

    def paintEvent(self, event):
        del event
        if not self.notice or not self.notice.message:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            if self.chip:
                tint = QColor(self.fill); tint.setAlphaF(.12)
                painter.setBrush(tint); painter.drawRoundedRect(QRectF(self.rect()), 13, 13)
                paint_glyph(painter, self.notice.feedback, QRectF(8, (self.height()-16)/2, 16, 16), self.fill)
            else:
                painter.setBrush(self.fill); painter.drawRoundedRect(QRectF(0, 6, 2, self.height()-12), 1, 1)
        finally:
            painter.end()



def style_cleanup_buttons(clean, clear):
    """One small button family, with room for both labels after polishing."""
    for button, labels, primary in ((clean, ("Clean up", "Accept?"), True),
                                    (clear, ("Clear draft", "Cancel"), False)):
        fill = theme.ACCENT if primary else theme.BG2
        ink = theme.TEXT_ON_ACCENT if primary else theme.TEXT_PRIMARY
        button.setFont(theme.qfont(theme.TYPE_MIN))
        button.setStyleSheet(f"QPushButton {{ background: {fill}; color: {ink}; border: 1px solid {theme.BORDER}; border-radius: 5px; font-family: '{theme.FONT_FAMILY}'; font-size: {theme.TYPE_MIN}px; padding: 0 {theme.SPACE_2}px; }} QPushButton:disabled {{ color: {theme.TEXT_DISABLED}; background: {theme.BG2}; }} QPushButton:hover {{ border-color: {theme.ACCENT}; }}")
        button.ensurePolished()
        padding = 2*theme.SPACE_2+2
        button.setProperty("labelPadding", padding)
        button.setMinimumWidth(max(button.fontMetrics().horizontalAdvance(t) for t in labels)+padding)
        button.setFixedHeight(max(26, button.fontMetrics().height()+6))


def cleanup_buttons(clean, clear, pending, *, reduced_motion=False):
    """Swap labels in place; opacity affects paint only, never geometry."""
    if clean.property("cleanupPending") == pending:
        return
    clean.setProperty("cleanupPending", pending)
    for button, label in ((clean, "Accept?" if pending else "Clean up"),
                          (clear, "Cancel" if pending else "Clear draft")):
        button.setText(label); button.setAccessibleName(label); button.setToolTip(label)
        previous = getattr(button, "_cleanup_fade", None)
        if previous is not None:
            previous.stop()
        if reduced_motion:
            continue
        effect = button.graphicsEffect()
        if effect is None:
            effect = QGraphicsOpacityEffect(button); button.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", button)
        animation.setDuration(140); animation.setStartValue(.25); animation.setEndValue(1.)
        button._cleanup_fade = animation
        animation.start()


def sync_cleanup_buttons(app):
    """Called on Qt's thread after a proposal outcome, for either display."""
    from samsara.ava_edit import session
    staged = session.pending()
    manager = getattr(app, "_session_mode_manager", None)
    active = bool(staged and staged.draft_target and staged.draft_target[0] is manager)
    surface = getattr(app, "live_surface", None)
    full = getattr(surface, "widget", None)
    if full is not None:
        cleanup_buttons(full._clean_up, full._clear, active, reduced_motion=full._reduced_motion)
    preview = getattr(app, "_dictate_preview", None)
    overlay = getattr(preview, "_overlay", None)
    compact = getattr(getattr(overlay, "_widget", None), "_w", None)
    if compact is not None:
        cleanup_buttons(compact._clean_up_btn, compact._clear_btn, active,
                        reduced_motion=compact._reduced_motion)


class LiveSurfaceWidget(QWidget):
    """One shaped HWND for M/D/S/L/R with review kept inside the same card."""

    commit_requested = Signal()
    clear_requested = Signal()
    dismiss_feedback_requested = Signal()
    dismiss_question_requested = Signal()
    clean_up_requested = Signal()
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
        from samsara.ui.passive_topmost import PassiveTopmost
        self._topmost_guard = PassiveTopmost(self)
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
        self._picker_page = 0
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
        self._band.dismissed.connect(self.dismiss_feedback_requested.emit)
        self._secondary_band.dismissed.connect(self.dismiss_feedback_requested.emit)
        self._band.question_dismissed.connect(self.dismiss_question_requested.emit)
        self._secondary_band.question_dismissed.connect(self.dismiss_question_requested.emit)
        self._chip = None
        self._band_height = 0
        self._state = QLabel(self); self._state.setObjectName("surfaceState"); self._state.setWordWrap(True)
        self._state.setAccessibleName("Live surface state")
        self._correction_hint = QLabel(self); self._correction_hint.setWordWrap(True)
        self._correction_hint.setAccessibleName("Correction choices")
        self._transcript = QTextBrowser(self)
        self._transcript.document().setDocumentMargin(12)
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
        self._clear = self._button("Clear draft", "Clear draft", self.clear_requested.emit)
        self._clean_up = self._button("Clean up draft", "Clean up", self.clean_up_requested.emit)
        style_cleanup_buttons(self._clean_up, self._clear)
        self._insert = self._button("Insert waiting text", "Insert text", self.commit_requested.emit)
        self._pause = self._button("Pause capture", "Pause", self.pause_requested.emit)
        self._correct = self._button("Correct a word", "Correct", self._request_correct)
        self._edit_receipt = self._button("Edit sent receipt as a new draft", "Edit as new draft", self._request_edit_receipt)
        self._collapse = self._button("Collapse surface", "Collapse", self.collapse_requested.emit)
        self._controls = (self._clear, self._clean_up, self._pause, self._correct, self._edit_receipt, self._collapse)
        self._picker_buttons = [self._button("Draft word", "", lambda n=n: self._choose_picker(n))
                                for n in range(4)]
        from samsara.ui.correction_picker_qt import WordCorrectionMenu
        self._word_menu = WordCorrectionMenu(self, getattr(controller, "app", None),
            self._apply_word_choice, self._cancel_correction,
            lambda: self._apply_view(self._view, animate=False))
        self._editor, self._apply, self._keep = self._word_menu._editor, self._word_menu.apply, self._word_menu.dismiss
        self._more = self._button("Next draft words", "Next words", self._more_clicked)
        self._cancel = self._button("Cancel replacement", "Cancel", self._cancel_editor)
        self._apply_view(self._view, animate=False)
        self._expiry_timer = QTimer(self)
        self._expiry_timer.setInterval(100)
        expire = getattr(controller, "refresh_expired", None)
        if callable(expire):
            self._expiry_timer.timeout.connect(expire)
            self._expiry_timer.start()

    def _button(self, name: str, label: str, callback) -> QPushButton:
        button = QPushButton(label, self); button.setAccessibleName(name)
        button.setFont(theme.qfont(theme.TYPE_BODY))
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus); button.setMinimumHeight(MARK_SIZE)
        button.clicked.connect(callback)
        return button

    def _button_width(self, button: QPushButton, minimum: int = MARK_SIZE) -> int:
        button.ensurePolished()
        return max(MARK_SIZE, minimum, button.minimumWidth(), button.sizeHint().width(),
                   button.fontMetrics().horizontalAdvance(button.text()) + 2*theme.SPACE_2 + 2)

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
        if self._animation is not None and self._animation.state() == QPropertyAnimation.State.Running:
            bounds = placed.bounds
            self._animation.setEndValue(QRect(round(bounds.x), round(bounds.y),
                                              round(bounds.width), round(bounds.height)))
            return
        bounds = placed.bounds
        self._placement_edge = placed.edge
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
        self._close_spelling()
        self._spin_timer.stop()
        self._motion_timer.stop()
        self._last_mark_time = None
        super().hideEvent(event)

    def _display_form(self) -> VisibleForm:
        # Correction is local UI intent; queued producer updates must never
        # leave its header visible without the body or a way to dismiss it.
        return VisibleForm.REVIEW if self._selection is not None or self._picker_active or self._word_menu.learning else self._view.form

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
        self._word_menu.finish()
        self._selection = None; self._picker_active = True; self._picker_page = 0; self._candidates = []
        self._apply_view(self._view, animate=False); self._announce("picker", "Choose a word to correct")

    def show_candidates(self, selection: dict[str, object], candidates: list[object]) -> None:
        self._close_spelling()
        self._selection, self._picker_active, self._candidates = dict(selection), False, list(candidates)[:12]
        self._word_menu.open(str(selection.get("word", "")), self._candidates)
        self._apply_view(self._view, animate=False); self._announce(("correction", selection.get("revision")), "Correction choices ready")

    def correction_refused(self, message: str = "That word changed. Select it again.") -> None:
        self._close_spelling()
        self._selection = None; self._picker_active = False; self._candidates = []
        if message != "Correction applied":
            self._word_menu.finish()
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
            self._close_spelling()
            if not self._word_menu.awaiting:
                self._word_menu.finish()
            self._selection = None; self._candidates = []
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
        notice = view.notice if view.notice.message else None
        echo = (notice and notice.purpose is FeedbackPurpose.RESULT
                and notice.feedback not in (FeedbackKind.ERROR, FeedbackKind.WARNING, FeedbackKind.LIVE)
                and notice.message.strip().casefold() == state.casefold())
        short = (notice and not echo and '\n' not in notice.message and '\r' not in notice.message
                 and notice.purpose is not FeedbackPurpose.QUESTION
                 and notice.kind not in (NoticeKind.PENDING, NoticeKind.CONFIRMATION, NoticeKind.ALIAS_OFFER)
                 and not (self._parked_idle(view) and notice.owner == 'draft'))
        if short and self._chip is None:
            self._chip = _FeedbackBand(self, "surfaceChip", chip=True)
            self._chip.dismissed.connect(self.dismiss_feedback_requested.emit)
            self._chip.question_dismissed.connect(self.dismiss_question_requested.emit)
        if self._chip is not None:
            if short:
                self._chip.set_notice(notice); self._chip.restyle()
            else:
                self._chip.hide(); self._chip.deleteLater(); self._chip = None
        self._band.set_notice(notice if notice and not (echo or short) else None)
        self._secondary_band.set_notice(view.secondary_notice)
        self._restyle()
        self._state.setAccessibleDescription(state)
        self.setAccessibleDescription("; ".join(part for part in (
            self._lane.text(), state, KIND_WORDS[notice.feedback] + ': ' + notice.message if notice else '',
            self._secondary_band.accessibleDescription(), view.document.value.replace("_", " ")) if part))
        self._provisional.setText(f"⋯ {view.provisional_text}" if view.provisional_text else "")
        self._badge.setText("Tap Insert text to send.")
        previous_content = (previous_text, previous.provisional_text)
        if previous_content != (view.text, view.provisional_text) and any(previous_content) and not self._follow_latest:
            self._unread_segments += 1
        target = self._form_rect(self._display_form())
        if self._dragging:
            return
        from samsara.live_surface.placement import Edge
        if getattr(self, '_placement_edge', None) is Edge.BOTTOM:
            target.moveBottom(self.geometry().bottom())
        if animate and self.isVisible() and not self._reduced_motion and self.geometry().size() != target.size(): self._animate_geometry(target)
        else: self.setGeometry(target); self._layout_children(); self._update_mask()
        self._announce_transition(previous, view); self.update()
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
                fill = f' style="background-color:{theme.ACCENT_DIM};"' if highlighted(index, self._selection) else ''
                parts.append(f'<a href="word:{index}"{fill}>{escape(word)}</a>'); at = match.end()
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
        limit = self._message_width_limit()
        bands = [b for b in (self._chip, self._band, self._secondary_band) if b is not None and b.notice]
        if bands:
            width = min(limit, max(width, min(820, max(b.desired_width()+2*CARD_INSET for b in bands))))
        parked = self._view.document is DocumentState.PARKED_DRAFT
        self._band_height = self._band.required_height(width-2*CARD_INSET)
        if self._chip is not None:
            self._band_height += self._chip.required_height(width-2*CARD_INSET)
        if self._view.secondary_notice:
            self._band_height += self._secondary_band.required_height(width-2*CARD_INSET)
        if form is VisibleForm.STATUS:
            return QRect(self.x(), self.y(), width, 76+self._band_height+self._cleanup_row_height())
        if self._cleanup_row_height() and parked and (not self._follow_latest or self._unread_segments):
            # Retain every existing header control and the full lane label.
            actions_width = sum(self._button_width(b, n) for b, n in
                                ((self._clear, 64), (self._insert, 132), (self._latest, 104)))
            title_width = max(self._lane.fontMetrics().horizontalAdvance(self._lane_text(self._view)),
                              self._state.fontMetrics().horizontalAdvance(self._state_text(self._view)))
            width = max(width, 2*CARD_INSET+MARK_SIZE+12+actions_width+title_width+4*REGION_GAP
                        )
            width = min(width, limit)
        if self._selection is not None or self._picker_active or self._word_menu.learning:
            rows = ceil(min(4, max(0, len(self._word_targets)-self._picker_page*4))/2)
            height = 2*CARD_INSET + 2*(MARK_SIZE+REGION_GAP) + rows*(MARK_SIZE+REGION_GAP) + MARK_SIZE
            if self._selection is not None or self._word_menu.learning:
                doc = self._transcript.document().clone()
                doc.setTextWidth(width - 2*CARD_INSET - 10)
                self._correction_transcript_height = min(120, max(52, ceil(doc.size().height()) + 4))
                doc.deleteLater()
                height = (2*CARD_INSET + HEADER_HEIGHT + REGION_GAP + self._band_height + 12
                          + self._correction_transcript_height + REGION_GAP + self._word_menu.sizeHint().height())
        else:
            # 492: use the rendered document, including explicit newlines and
            # actual font wrapping; no minimum envelope or empty notice band.
            doc = self._transcript.document().clone()
            doc.setDefaultFont(theme.qfont(theme.TYPE_BODY))
            # Reserve the themed bar even before Qt decides it is needed.
            doc.setTextWidth(width - 2*CARD_INSET - 10 - 2*self._transcript.frameWidth())
            document_height = ceil(doc.size().height())
            layout = doc.begin().layout()
            line_height = max(1, ceil(layout.lineAt(0).height())) if layout.lineCount() else 1
            margin = ceil(2 * doc.documentMargin())
            cap = margin + ((240 - margin) // line_height) * line_height
            text_height = min(cap, document_height + 4)
            doc.deleteLater()
            self._transcript_height = text_height
            return QRect(self.x(), self.y(), width,
                         2*CARD_INSET + HEADER_HEIGHT + REGION_GAP
                         + self._band_height + text_height + self._cleanup_row_height())
        return QRect(self.x(), self.y(), width, height + self._band_height + 20)

    def _message_width_limit(self):
        screens = getattr(self._controller, '_screens', None)
        if callable(screens) and screens():
            from samsara.live_surface.placement import placement_for_screens
            raw, records = self._controller._placement_config()
            resolved = placement_for_screens(records, screens(), preferred_monitor=raw.get('preferred_monitor'))
            return max(1, int(resolved.screen.work_area.width)-16)
        screen = self.screen() or QApplication.primaryScreen()
        return max(1, screen.availableGeometry().width()-16) if screen else 820

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
        if self._chip is not None: self._chip.hide()
        for child in (self._lane, self._band, self._secondary_band, self._state, self._correction_hint, self._transcript, self._provisional, self._badge, self._latest, self._insert,
                      *self._controls, *self._picker_buttons,
                      self._more, self._cancel): child.hide()
        if self._selection is None and not self._word_menu.learning:
            self._word_menu.hide()

    def _layout_children(self) -> None:
        bar = self._transcript.verticalScrollBar()
        position = bar.value()
        with QSignalBlocker(bar):
            self._layout_content()
            if self._view.capture is CaptureState.UNAVAILABLE:
                state = self._state.geometry()
                self._state.setGeometry(state.x()+26, state.y(), max(1, state.width()-26), state.height())
            bar.setValue(bar.maximum() if self._follow_latest else position)

    def _cleanup_visible(self) -> bool:
        return (getattr(self._controller, "idle_capture", None) is CaptureState.HANDS_FREE_LISTENING
                or self._view.capture is CaptureState.HANDS_FREE_LISTENING)

    def _cleanup_row_height(self) -> int:
        correcting = self._selection is not None or self._picker_active or self._word_menu.learning
        occupied = bool(self._view.text.strip()) or self._chip is not None
        return 34 if self._cleanup_visible() and occupied and not correcting else 0

    def _layout_content(self) -> None:
        form = self._display_form(); self._mark.setGeometry(0, 0, MARK_SIZE, MARK_SIZE); self._hide_children()
        if form is VisibleForm.MARK:
            self._state.setGeometry(MARK_SIZE+REGION_GAP, 0, self.width()-MARK_SIZE-2*REGION_GAP, MARK_SIZE)
            self._state.show(); return
        if form is VisibleForm.DRAFT_BADGE:
            self._mark.setGeometry(8, 8, MARK_SIZE, MARK_SIZE)
            clear_width = self._button_width(self._clear, 64)
            insert_width = self._button_width(self._insert, 132)
            self._state.setGeometry(64, 8, self.width()-72-clear_width-REGION_GAP, MARK_SIZE); self._state.show()
            self._clear.setGeometry(self.width()-8-clear_width, 8, clear_width, MARK_SIZE); self._clear.show()
            self._badge.setGeometry(16, 60, self.width()-24-insert_width-REGION_GAP, MARK_SIZE); self._badge.show()
            self._insert.setGeometry(self.width()-8-insert_width, 60, insert_width, MARK_SIZE); self._insert.show(); return
        content = self.card_content_rect
        self._mark.setGeometry(content.x(), content.y(), MARK_SIZE, MARK_SIZE)
        state_x = content.x()+MARK_SIZE+12
        clear_width = self._button_width(self._clear, 64)
        self._lane.setGeometry(state_x, content.y(), content.width()-MARK_SIZE-12-clear_width-REGION_GAP, HEADER_HEIGHT//2)
        self._state.setGeometry(state_x, content.y()+HEADER_HEIGHT//2, content.width()-MARK_SIZE-12-clear_width-REGION_GAP, HEADER_HEIGHT//2)
        self._lane.show(); self._state.show()
        self._state.setVisible(not self._parked_idle(self._view))
        cleanup_row = self._cleanup_row_height()
        band_y = content.y()+HEADER_HEIGHT+12+cleanup_row
        parked = self._view.document is DocumentState.PARKED_DRAFT
        primary_height = self._band.required_height(content.width())
        chip_height = self._chip.required_height(content.width()) if self._chip is not None else 0
        band_y += chip_height
        self._band.setGeometry(content.x(), band_y, content.width(), primary_height)
        self._band.setVisible(primary_height > 0)
        if self._view.secondary_notice:
            self._secondary_band.setGeometry(content.x(), band_y+primary_height,
                content.width(), self._secondary_band.required_height(content.width()))
            self._secondary_band.show()
        if form is VisibleForm.STATUS:
            clearable = self._view.capture is CaptureState.RECORDING or self._view.notice.kind in (
                NoticeKind.ERROR, NoticeKind.CONFIRMATION, NoticeKind.ALIAS_OFFER)
            if clearable:
                self._clear.setGeometry(content.right()+1-clear_width, content.y(), clear_width, MARK_SIZE); self._clear.show()
            right = self._clear.x()-REGION_GAP if clearable else content.right()+1
            if self._cleanup_visible():
                width = self._button_width(self._clean_up)
                self._clean_up.setEnabled(False)
                self._clean_up.setGeometry(right-width, content.y()+9, width, 26); self._clean_up.show()
                right -= width+REGION_GAP
                self._lane.setGeometry(state_x, content.y(), max(1, right-state_x), HEADER_HEIGHT//2)
                self._state.setGeometry(state_x, content.y()+HEADER_HEIGHT//2, max(1, right-state_x), HEADER_HEIGHT//2)
            if cleanup_row:
                self._clean_up.move(content.right()+1-self._clean_up.width(), content.y()+HEADER_HEIGHT+REGION_GAP)
                right = self._clear.x()-REGION_GAP if clearable else content.right()+1
                self._lane.setGeometry(state_x, content.y(), max(1, right-state_x), HEADER_HEIGHT//2)
                self._state.setGeometry(state_x, content.y()+HEADER_HEIGHT//2, max(1, right-state_x), HEADER_HEIGHT//2)
            self._layout_chip(right)
            return
        if form is VisibleForm.REVIEW and (self._selection is not None or self._picker_active or self._word_menu.learning):
            self._layout_chip(content.right()+1)
            self._layout_correction(content); return
        # Reserve each action's space once. Pending-text recovery is separate
        # from ordinary capture, whose final is inserted automatically.
        actions = [(self._clear, 64)]
        if self._cleanup_visible():
            self._clean_up.setEnabled(bool(self._view.text.strip()) and self._view.document in
                                      (DocumentState.EDITABLE_DRAFT, DocumentState.PARKED_DRAFT))
            if cleanup_row:
                width = self._button_width(self._clean_up)
                self._clear.setGeometry(content.right()+1-clear_width, content.y()+HEADER_HEIGHT+REGION_GAP, clear_width, 26)
                self._clear.show(); actions = []
                self._clean_up.setGeometry(self._clear.x()-REGION_GAP-width, content.y()+HEADER_HEIGHT+REGION_GAP, width, 26)
                self._clean_up.show()
            else:
                actions.insert(0, (self._clean_up, 0))
        if self._view.document is DocumentState.PARKED_DRAFT and self._view.text:
            actions.insert(0, (self._insert, 132))
        if not self._follow_latest or self._unread_segments:
            latest = "Latest" + (f" (+{self._unread_segments})" if self._unread_segments else "")
            self._latest.setToolTip(latest)
            self._latest.setText("Latest" if parked and self._chip is not None else latest)
            actions.append((self._latest, 104))
        right = content.right()+1
        for button, width in reversed(actions):
            width = self._button_width(button, width)
            right -= width
            button.setGeometry(right, content.y()+(9 if button in (self._clean_up, self._clear) else 0), width, 26 if button in (self._clean_up, self._clear) else MARK_SIZE); button.show()
            right -= REGION_GAP
        state_x = content.x()+MARK_SIZE+12
        self._lane.setGeometry(state_x, content.y(), max(1, right-state_x), HEADER_HEIGHT//2)
        self._state.setGeometry(state_x, content.y()+HEADER_HEIGHT//2, max(1, right-state_x), HEADER_HEIGHT//2)
        self._layout_chip(right)
        y = content.y()+HEADER_HEIGHT+REGION_GAP+self._band_height+cleanup_row
        if form in (VisibleForm.LIVE, VisibleForm.REVIEW):
            band_y = content.y()+HEADER_HEIGHT+REGION_GAP+cleanup_row+chip_height
            self._band.move(content.x(), band_y)
            if self._view.secondary_notice:
                self._secondary_band.move(content.x(), band_y+primary_height)
            y = content.y()+HEADER_HEIGHT+REGION_GAP+cleanup_row+self._band_height
        from samsara.live_surface.placement import Edge
        if getattr(self, '_placement_edge', None) is Edge.BOTTOM:
            # Keep settled text at its dock distance throughout the animation.
            y = content.bottom()+1-getattr(self, '_transcript_height', content.bottom()-y+1)
        available = content.bottom() - y + 1
        # Both settled and incoming words need the same scrollable viewport.
        # A capped QLabel otherwise clips long speech below a retained draft.
        self._transcript.setGeometry(content.x(), y, content.width(), available)
        self._transcript.show()

    def _layout_chip(self, right):
        if self._chip is None: return
        content = self.card_content_rect
        self._chip.setGeometry(content.x(), content.y()+HEADER_HEIGHT+REGION_GAP+self._cleanup_row_height(),
                               content.width(), self._chip.required_height(content.width()))
        self._chip.show()

    def _layout_row(self, controls, x: int, y: int, width: int) -> None:
        widths = [self._button_width(control) for control in controls]
        available = width - REGION_GAP*(len(controls)-1)
        base, rem = divmod(max(0, available-sum(widths)), len(controls))
        for index, control in enumerate(controls):
            w=widths[index]+base+(1 if index<rem else 0); control.setGeometry(x,y,w,MARK_SIZE); control.show(); x += w+REGION_GAP

    def _layout_correction(self, content: QRect) -> None:
        y=content.y()+HEADER_HEIGHT+REGION_GAP+self._band_height+12
        if self._selection is not None or self._word_menu.learning:
            height = getattr(self, '_correction_transcript_height', 80)
            self._transcript.setGeometry(content.x(), y, content.width(), height)
            self._transcript.show(); y += height + REGION_GAP
            self._word_menu.setGeometry(content.x(), y, content.width(), self._word_menu.sizeHint().height())
            self._word_menu.show()
            return
        self._correction_hint.setText("Choose a word to correct")
        self._correction_hint.setGeometry(content.x(), y, content.width(), MARK_SIZE)
        self._correction_hint.show(); y += MARK_SIZE + REGION_GAP
        start = self._picker_page * 4
        for index, button in enumerate(self._picker_buttons):
            if start + index >= len(self._word_targets):
                continue
            label = self._word_targets[start + index]["word"]
            button.setText(f"{start+index+1}. {label}")
            button.setGeometry(content.x()+(index%2)*(content.width()//2+REGION_GAP//2), y+(index//2)*(MARK_SIZE+REGION_GAP), self._button_width(button, content.width()//2-REGION_GAP//2), MARK_SIZE)
            button.show()
        y += (MARK_SIZE+REGION_GAP) * ceil(min(4, max(0, len(self._word_targets)-start))/2)
        self._layout_row((self._cancel,) + ((self._more,) if len(self._word_targets) > 4 else ()), content.x(), y, content.width())

    def _update_mask(self) -> None:
        if self._display_form() is VisibleForm.MARK:
            path=QPainterPath(); path.addRoundedRect(QRectF(0,0,self.width(),MARK_SIZE),CARD_RADIUS,CARD_RADIUS); region=QRegion(path.toFillPolygon().toPolygon())
        else:
            path=QPainterPath(); path.addRoundedRect(QRectF(self.card_rect),CARD_RADIUS,CARD_RADIUS); region=QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)

    def _restyle(self) -> None:
        self._transcript.verticalScrollBar().setStyleSheet(f"""
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {theme.BORDER}; border-radius: 4px; min-height: 24px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        self.setStyleSheet(f"LiveSurfaceWidget {{ background: transparent; }} QLabel,QTextBrowser {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; background: transparent; border: 0; }} QTextBrowser a {{ color: {theme.TEXT_PRIMARY}; text-decoration: none; }} QPushButton,QLineEdit {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: {CARD_RADIUS}px; font-size: {theme.TYPE_BODY}px; }} QPushButton {{ font-family: {theme.FONT_FAMILY}; padding: 0 {theme.SPACE_2}px; }} QPushButton#quietClear {{ background: transparent; border: 0; color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_MIN}px; }}")
        active = not self._view.snoozed and self._view.capture in (CaptureState.RECORDING, CaptureState.TRANSCRIBING)
        state_color = theme.ERROR if self._view.capture is CaptureState.UNAVAILABLE else theme.TEXT_PRIMARY if active else theme.TEXT_SECONDARY
        self._lane.setFont(theme.qfont(theme.TYPE_EMPHASIS, weight=QFont.Weight.Bold))
        self._lane.setStyleSheet(f"QLabel#surfaceLane {{ color: {theme.ACCENT}; font-size: {theme.TYPE_EMPHASIS}px; font-weight: 700; }}")
        self._state.setFont(theme.qfont(theme.TYPE_BODY, weight=QFont.Weight.Medium if active else QFont.Weight.Normal))
        self._state.setStyleSheet(f"QLabel#surfaceState {{ color: {state_color}; font-size: {theme.TYPE_BODY}px; font-weight: {500 if active else 400}; }}")
        self._band.restyle(); self._secondary_band.restyle()
        style_cleanup_buttons(self._clean_up, self._clear)
        if self._chip is not None: self._chip.restyle()
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
                y = self._transcript.y()-4
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
            self._drag_drop_bounds = None
            event.accept(); return
        if self._view.form in (VisibleForm.MARK,VisibleForm.DRAFT_BADGE) and self.mask().contains(event.position().toPoint()): self.show_requested.emit(); event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging and self._drag_offset is not None:
            from samsara.live_surface.placement import Rect
            origin = event.globalPosition().toPoint() - self._drag_offset
            bounds = Rect(origin.x(), origin.y(), self.width(), self.height())
            self._drag_drop_bounds = bounds
            preview = getattr(self._controller, "preview_move", None)
            placed = preview(bounds) if callable(preview) else None
            if placed is None:
                self.move(origin)
            else:
                self.move(round(placed.bounds.x), round(placed.bounds.y))
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            origin = self.mapToGlobal(self._mark.pos())
            bounds = getattr(self, "_drag_drop_bounds", None)
            center = ((bounds.x + self._mark.x() + MARK_SIZE / 2,
                       bounds.y + self._mark.y() + MARK_SIZE / 2) if bounds is not None else
                      (origin.x() + MARK_SIZE / 2, origin.y() + MARK_SIZE / 2))
            self.move_requested.emit({"center": center, "bounds": bounds})
            self.refresh(animate=False)
            place = getattr(self._controller, "_place_widget", None)
            if callable(place): place()
            event.accept(); return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self,event: QKeyEvent)->None:
        if event.key()==Qt.Key.Key_Escape:
            if self._dragging:
                self._dragging=False; self.setGeometry(self._drag_restore); event.accept(); return
            if self._selection is not None or self._picker_active or self._word_menu.learning:self._cancel_correction()
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
        selection = select_range(self._word_targets, index, self._segments(), self._selection,
            extend=bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier))
        if selection is not None:
            self.correct_word_requested.emit(selection)
    def _request_correct(self)->None:
        if self._selection is not None:self.correct_word_requested.emit(dict(self._selection))
        else:self.show_word_picker()
    def _request_edit_receipt(self)->None:
        if self._view.document is DocumentState.DELIVERED and self._view.text:
            self.edit_receipt_requested.emit(self._view.text)
    def _choose_picker(self,index:int)->None:
        number=self._picker_page*4+index
        if number<len(self._word_targets):self.correct_word_requested.emit(dict(self._word_targets[number]))
    def _more_clicked(self)->None:
        self._picker_page=(self._picker_page+1)%max(1,ceil(len(self._word_targets)/4))
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
        bar = self._transcript.verticalScrollBar()
        # Following must reach the true end, even if the viewport's top edge
        # falls within a line. Moving back from maximum also disables following.
        bar.setValue(bar.maximum())
    def _restore_transcript_scroll(self)->None:
        bar = self._transcript.verticalScrollBar()
        if self._follow_latest:
            self._scroll_to_latest()
        else:
            bar.setValue(self._scroll_position)
    def _apply_word_choice(self, text: str) -> None:
        if self._selection is not None:
            self.correction_apply_requested.emit(dict(self._selection), text)
    def _open_editor(self) -> None:
        self.focus_review(); self._editor.setFocus(); self._word_menu.begin_input()
    def _close_spelling(self) -> None:
        self._word_menu.close_input()
    def _apply_editor(self) -> None:
        self._word_menu._apply_editor()
    def _cancel_editor(self) -> None:
        self._cancel_correction()
    def _cancel_correction(self) -> None:
        self._word_menu.finish()
        self._selection=None; self._picker_active=False; self._candidates=[]
        self._restore_passive()
        self.correction_cancel_requested.emit(); self._apply_view(self._view,animate=False)
    def _announce(self,key:object,message:str)->None:self._announcer.announce_transition(key,message)
    @staticmethod
    def _parked_idle(view: SurfaceView) -> bool:
        return (view.document is DocumentState.PARKED_DRAFT and bool(view.text)
                and view.capture is CaptureState.OFF)

    def _lane_text(self, view: SurfaceView) -> str:
        if self._parked_idle(view):
            return "Kept draft"  # PLACEHOLDER COPY (492)
        labels = {"hold": "Hold", "toggle": "Toggle", "hands_free_dictate": "Hands-free",
                  "continuous": "Continuous", "command": "Command", "ava": "Ava"}
        return labels.get(view.lane.value, "") if view.lane is not None else ("Hold" if view.form is not VisibleForm.MARK else "")

    def _state_text(self, view: SurfaceView) -> str:
        if view.capture is CaptureState.UNAVAILABLE: return "microphone unavailable"
        if view.snoozed: return "Snoozed"
        if self._parked_idle(view) and view.form is not VisibleForm.DRAFT_BADGE:
            return ""
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
