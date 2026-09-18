"""The one passive, accessible presentation of a live-surface view.

This module is intentionally a renderer.  ``LiveSurfaceWidget`` has no draft
buffer and never chooses a form: it asks its supplied model/controller for a
``SurfaceView`` and reports human intent through signals.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QRect, QRectF, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import (QCloseEvent, QImage, QMouseEvent, QPainter, QPaintEvent,
                           QPainterPath, QPen, QRegion)
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from samsara.live_surface.model import (CaptureState, DocumentState, NoticeKind,
                                        PresentationState, SurfaceView, VisibleForm)
from samsara.ui import theme
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer
from samsara.ui.tray_qt import paint_mark


MARK_SIZE = 44
MARK_ART_SIZE = 24
CONNECTOR = 4
CARD_RADIUS = 12
CARD_INSET = 8
HEADER_HEIGHT = 44
REGION_GAP = 8
STATUS_SIZE = (360, 64)
LIVE_SIZE = (500, 240)
REVIEW_SIZE = (500, 360)
RESIZE_DURATION_MS = 120


def _view_from(source: Any) -> SurfaceView:
    """Accept a model, a controller exposing ``view()``, or a view in tests."""
    candidate = source.view() if callable(getattr(source, "view", None)) else source
    if not isinstance(candidate, SurfaceView):
        raise TypeError("LiveSurfaceWidget needs a LiveSurfaceModel/controller or SurfaceView")
    return candidate


def _capture_mark(view: SurfaceView) -> tuple[str, str]:
    """Map model truth to the shared mark renderer's existing vocabulary."""
    capture = {
        CaptureState.OFF: "idle",
        CaptureState.WAKE_ARMED: "listening",
        CaptureState.HANDS_FREE_LISTENING: "listening",
        CaptureState.RECORDING: "recording",
        CaptureState.TRANSCRIBING: "recording",
        CaptureState.UNAVAILABLE: "idle",
    }[view.capture]
    if view.lane is not None and view.lane.value == "ava":
        capture = "ava"
    eye = "armed" if view.capture in (CaptureState.WAKE_ARMED,
                                       CaptureState.HANDS_FREE_LISTENING) else "asleep"
    return capture, eye


def render_device_mark(capture: str, eye: str, *, dpr: float,
                       rotation: float = 0.0) -> QImage:
    """Render a 24-DIP mark from a device-resolution source image.

    ``paint_mark`` receives physical pixels before the image receives its DPR,
    so a 150% mark is drawn at 36 px rather than scaling a cached 24-px frame.
    """
    pixels = max(1, round(MARK_ART_SIZE * dpr))
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        paint_mark(painter, QRectF(0, 0, pixels, pixels), capture, eye, rotation,
                   small_max=None, brand=True)
    finally:
        painter.end()
    image.setDevicePixelRatio(dpr)
    return image


class _Mark(QWidget):
    """A 44-DIP target with a 24-DIP device-resolution brand mark."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.capture = "off"
        self.eye = "closed"
        self.rotation = 0.0
        self.setAccessibleName("Microphone state")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_state(self, capture: str, eye: str, rotation: float) -> None:
        self.capture, self.eye, self.rotation = capture, eye, rotation
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        dpr = self.devicePixelRatioF()
        image = render_device_mark(self.capture, self.eye, dpr=dpr, rotation=self.rotation)
        left = (self.width() - MARK_ART_SIZE) / 2
        top = (self.height() - MARK_ART_SIZE) / 2
        painter = QPainter(self)
        try:
            painter.drawImage(QRectF(left, top, MARK_ART_SIZE, MARK_ART_SIZE), image,
                              QRectF(0, 0, image.width(), image.height()))
        finally:
            painter.end()


class LiveSurfaceWidget(QWidget):
    """One shaped HWND for M/D/S/L/R, with real readable and actionable children."""

    commit_requested = Signal()
    clear_requested = Signal()
    pause_requested = Signal()
    correct_word_requested = Signal(object)
    scroll_requested = Signal(int)
    collapse_requested = Signal()
    move_requested = Signal(object)
    show_requested = Signal()

    def __init__(self, controller: Any, *, reduced_motion: bool = False) -> None:
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self._controller = controller
        self._view = _view_from(controller)
        self._reduced_motion = reduced_motion
        self._rotation = 0.0
        self._animation: QPropertyAnimation | None = None
        self._announcer = LiveSurfaceAnnouncer(self)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Samsara live surface")
        self.setAccessibleDescription("Passive microphone and draft status")

        self._mark = _Mark(self)
        self._mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._state = QLabel(self)
        self._state.setWordWrap(True)
        self._state.setAccessibleName("Live surface state")
        self._transcript = QLabel(self)
        self._transcript.setWordWrap(True)
        self._transcript.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._transcript.setAccessibleName("Transcript")
        self._provisional = QLabel(self)
        self._provisional.setWordWrap(True)
        self._provisional.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        self._provisional.setAccessibleName("Provisional transcript")
        self._badge = QLabel(self)
        self._badge.setText("▣  Draft")
        self._badge.setAccessibleName("Draft pending")

        self._commit = self._button("Commit draft", "Commit", self.commit_requested.emit)
        self._clear = self._button("Clear draft", "Clear", self.clear_requested.emit)
        self._pause = self._button("Pause capture", "Pause", self.pause_requested.emit)
        self._correct = self._button("Correct selected word", "Correct", self._request_correct)
        self._collapse = self._button("Collapse surface", "Collapse", self.collapse_requested.emit)
        self._controls = (self._commit, self._clear, self._pause, self._correct, self._collapse)
        self._apply_view(self._view, animate=False)

    def _button(self, name: str, label: str, callback) -> QPushButton:
        button = QPushButton(label, self)
        button.setAccessibleName(name)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setMinimumHeight(MARK_SIZE)
        button.clicked.connect(callback)
        return button

    @property
    def view(self) -> SurfaceView:
        return self._view

    @property
    def announcer(self) -> LiveSurfaceAnnouncer:
        return self._announcer

    @property
    def mark_rect(self) -> QRect:
        return self._mark.geometry()

    @property
    def card_rect(self) -> QRect:
        if self._view.form in (VisibleForm.MARK, VisibleForm.DRAFT_BADGE):
            return QRect()
        return QRect(0, MARK_SIZE + CONNECTOR, self.width(), self.height() - MARK_SIZE - CONNECTOR)

    @property
    def card_content_rect(self) -> QRect:
        """The §3 all-sides 8-DIP inset that contains every card child."""
        return self.card_rect.adjusted(CARD_INSET, CARD_INSET, -CARD_INSET, -CARD_INSET)

    def hit_region(self) -> QRegion:
        return self.mask()

    def refresh(self, view: SurfaceView | None = None, *, animate: bool = True) -> None:
        """Render a current model projection; partial-only refreshes stay quiet."""
        next_view = _view_from(self._controller) if view is None else view
        self._apply_view(next_view, animate=animate)

    def set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = enabled
        if enabled and self._animation is not None:
            self._animation.stop()
            self._animation = None

    def focus_review(self) -> None:
        """The sole opt-in route which permits normal keyboard focus."""
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.show()
        self._commit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._clear.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pause.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._correct.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._collapse.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._commit.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _apply_view(self, view: SurfaceView, *, animate: bool) -> None:
        previous = self._view
        self._view = view
        self._rotation = self._rotation if self._reduced_motion else self._rotation + 15.0
        capture, eye = _capture_mark(view)
        self._mark.set_state(capture, eye, self._rotation)
        self._mark.setAccessibleName(self._state_text(view))
        self._state.setText(self._state_text(view))
        self._transcript.setText(view.text)
        self._provisional.setText(f"⋯ {view.provisional_text}" if view.provisional_text else "")
        words = len(view.text.split())
        self._badge.setText(f"▤  Draft\n{words} word{'s' if words != 1 else ''}")
        target = self._form_rect(view.form)
        if animate and self.isVisible() and not self._reduced_motion and self.geometry().size() != target.size():
            self._animate_geometry(target)
        else:
            self.resize(target.size())
            self._layout_children()
            self._update_mask()
        self._restyle()
        self._announce_transition(previous, view)
        self.update()

    def _form_rect(self, form: VisibleForm) -> QRect:
        if form is VisibleForm.MARK:
            return QRect(self.x(), self.y(), MARK_SIZE, MARK_SIZE)
        if form is VisibleForm.DRAFT_BADGE:
            return QRect(self.x(), self.y(), 120, MARK_SIZE)
        width, card_height = STATUS_SIZE if form is VisibleForm.STATUS else (
            LIVE_SIZE if form is VisibleForm.LIVE else REVIEW_SIZE)
        return QRect(self.x(), self.y(), width, MARK_SIZE + CONNECTOR + card_height)

    def _animate_geometry(self, target: QRect) -> None:
        animation = QPropertyAnimation(self, b"geometry", self)
        animation.setDuration(RESIZE_DURATION_MS)
        animation.setStartValue(self.geometry())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(lambda _: (self._layout_children(), self._update_mask()))
        animation.finished.connect(lambda: (self._layout_children(), self._update_mask()))
        self._animation = animation
        animation.start()

    def _layout_children(self) -> None:
        form = self._view.form
        self._mark.setGeometry((self.width() - MARK_SIZE) // 2, 0, MARK_SIZE, MARK_SIZE)
        for child in (self._state, self._transcript, self._provisional, self._badge, *self._controls):
            child.hide()
        if form is VisibleForm.MARK:
            return
        if form is VisibleForm.DRAFT_BADGE:
            self._mark.setGeometry(0, 0, MARK_SIZE, MARK_SIZE)
            self._badge.setGeometry(MARK_SIZE, 0, 76, MARK_SIZE)
            self._badge.show()
            return
        content = self.card_content_rect
        if form is VisibleForm.STATUS:
            control = self._clear if self._view.notice.kind is NoticeKind.ERROR else self._pause
            control_width = 112
            state_width = content.width() - control_width - REGION_GAP
            self._state.setGeometry(content.x(), content.y(), state_width, content.height())
            self._state.show()
            control.setGeometry(content.right() - control_width + 1,
                                content.y() + (content.height() - MARK_SIZE) // 2,
                                control_width, MARK_SIZE)
            control.show()
            return
        self._state.setGeometry(content.x(), content.y(), content.width(), HEADER_HEIGHT)
        self._state.show()
        viewport = 240 if form is VisibleForm.REVIEW else 120
        transcript_y = content.y() + HEADER_HEIGHT + REGION_GAP
        self._transcript.setGeometry(content.x(), transcript_y, content.width(), viewport - 24)
        self._transcript.show()
        self._provisional.setGeometry(content.x(), transcript_y + viewport - 24,
                                     content.width(), 24)
        self._provisional.show()
        action_y = transcript_y + viewport + REGION_GAP
        controls = (self._clear, self._commit, self._pause, self._correct)
        available = content.width() - REGION_GAP * (len(controls) - 1)
        base_width, remainder = divmod(available, len(controls))
        widths = tuple(base_width + (1 if index < remainder else 0)
                       for index in range(len(controls)))
        x = content.x()
        for control, width in zip(controls, widths):
            control.setGeometry(x, action_y, width, MARK_SIZE)
            control.show()
            x += width + REGION_GAP

    def _update_mask(self) -> None:
        form = self._view.form
        if form is VisibleForm.MARK:
            region = QRegion(self._mark.geometry(), QRegion.RegionType.Ellipse)
        elif form is VisibleForm.DRAFT_BADGE:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, self.width(), MARK_SIZE), CARD_RADIUS, CARD_RADIUS)
            region = QRegion(path.toFillPolygon().toPolygon())
        else:
            card = self.card_rect
            region = QRegion(self._mark.geometry(), QRegion.RegionType.Ellipse)
            region |= QRegion(QRect((self.width() - CONNECTOR) // 2, MARK_SIZE, CONNECTOR, CONNECTOR))
            path = QPainterPath()
            path.addRoundedRect(QRectF(card), CARD_RADIUS, CARD_RADIUS)
            region |= QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)

    def _restyle(self) -> None:
        form = self._view.form
        self.setStyleSheet(
            f"LiveSurfaceWidget {{ background: transparent; }}"
            f"QLabel {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; }}"
            f"QLabel#meta {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_MIN}px; }}"
            f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; "
            f"border: 1px solid {theme.BORDER}; border-radius: {CARD_RADIUS}px; "
            f"font-size: {theme.TYPE_BODY}px; }}"
        )
        self._state.setFont(theme.qfont(theme.TYPE_HEADING))
        self._transcript.setFont(theme.qfont(theme.TYPE_BODY))
        provisional_font = theme.qfont(theme.TYPE_MIN)
        provisional_font.setItalic(True)
        self._provisional.setFont(provisional_font)
        self._badge.setFont(theme.qfont(theme.TYPE_MIN))
        self._provisional.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; border-top: 1px dashed {theme.BORDER};"
        )
        if form is VisibleForm.DRAFT_BADGE:
            self._badge.setStyleSheet(f"background: transparent; color: {theme.TEXT_PRIMARY};")

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        if self._view.form in (VisibleForm.MARK, VisibleForm.DRAFT_BADGE):
            if self._view.form is VisibleForm.MARK:
                return
            painter = QPainter(self)
            try:
                painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
                painter.setBrush(theme.qcolor(theme.BG1))
                painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, MARK_SIZE - 1),
                                        CARD_RADIUS, CARD_RADIUS)
            finally:
                painter.end()
            return
        painter = QPainter(self)
        try:
            painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
            painter.setBrush(theme.qcolor(theme.BG1))
            card = QRectF(self.card_rect).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRoundedRect(card, CARD_RADIUS, CARD_RADIUS)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor(theme.BG2))
            painter.drawRect((self.width() - CONNECTOR) // 2, MARK_SIZE, CONNECTOR, CONNECTOR)
        finally:
            painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._view.form in (VisibleForm.MARK, VisibleForm.DRAFT_BADGE) and self.mask().contains(event.position().toPoint()):
            self.show_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.hide()
        self.collapse_requested.emit()
        event.ignore()

    def _request_correct(self) -> None:
        self.correct_word_requested.emit((0, len(self._view.text)))

    def _state_text(self, view: SurfaceView) -> str:
        if view.notice.message:
            return view.notice.message
        if view.capture is CaptureState.RECORDING:
            return "Recording"
        if view.capture is CaptureState.TRANSCRIBING:
            return "Transcribing"
        if view.capture is CaptureState.UNAVAILABLE:
            return "Microphone unavailable"
        if view.document is DocumentState.PARKED_DRAFT:
            return "Draft parked"
        if view.document is not DocumentState.EMPTY:
            return "Draft pending"
        if view.capture in (CaptureState.WAKE_ARMED, CaptureState.HANDS_FREE_LISTENING):
            return "Listening"
        return "Microphone idle"

    def _announce_transition(self, previous: SurfaceView, current: SurfaceView) -> None:
        if previous.capture is not current.capture:
            self._announcer.announce_transition(("capture", current.capture), self._state_text(current))
        elif previous.document is not current.document and current.document is DocumentState.PARKED_DRAFT:
            self._announcer.announce_transition(("parked", current.document_id, current.revision), "Draft parked")
        elif previous.notice.kind is not current.notice.kind and current.notice.kind in (
                NoticeKind.ERROR, NoticeKind.CONFIRMATION):
            self._announcer.announce_transition(("notice", current.notice.kind, current.message if hasattr(current, 'message') else current.notice.message), self._state_text(current))
