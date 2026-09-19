"""The one passive, accessible presentation of a live-surface view.

The widget stores only view position and a correction selection.  Draft text and
all edits remain with the revisioned document owned by ``SessionModeManager``.
"""
from __future__ import annotations

from html import escape
import re
from typing import Any

from PySide6.QtCore import QEasingCurve, QRect, QRectF, QPropertyAnimation, Qt, QUrl, Signal
from PySide6.QtGui import (QCloseEvent, QImage, QKeyEvent, QMouseEvent, QPainter,
                           QPaintEvent, QPainterPath, QPen, QRegion)
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTextBrowser, QWidget

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
_WORD = re.compile(r"[\w]+(?:['’\-][\w]+)*", re.UNICODE)


def _view_from(source: Any) -> SurfaceView:
    candidate = source.view() if callable(getattr(source, "view", None)) else source
    if not isinstance(candidate, SurfaceView):
        raise TypeError("LiveSurfaceWidget needs a LiveSurfaceModel/controller or SurfaceView")
    return candidate


def _capture_mark(view: SurfaceView) -> tuple[str, str]:
    capture = {
        CaptureState.OFF: "idle", CaptureState.WAKE_ARMED: "listening",
        CaptureState.HANDS_FREE_LISTENING: "listening", CaptureState.RECORDING: "recording",
        CaptureState.TRANSCRIBING: "recording", CaptureState.UNAVAILABLE: "idle",
    }[view.capture]
    if view.lane is not None and view.lane.value == "ava":
        capture = "ava"
    eye = "armed" if view.capture in (CaptureState.WAKE_ARMED,
                                       CaptureState.HANDS_FREE_LISTENING) else "asleep"
    return capture, eye


def render_device_mark(capture: str, eye: str, *, dpr: float,
                       rotation: float = 0.0) -> QImage:
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
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.capture, self.eye, self.rotation = "off", "closed", 0.0
        self.setAccessibleName("Microphone state")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_state(self, capture: str, eye: str, rotation: float) -> None:
        self.capture, self.eye, self.rotation = capture, eye, rotation
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        image = render_device_mark(self.capture, self.eye, dpr=self.devicePixelRatioF(),
                                   rotation=self.rotation)
        painter = QPainter(self)
        try:
            painter.drawImage(QRectF((self.width() - MARK_ART_SIZE) / 2,
                                     (self.height() - MARK_ART_SIZE) / 2,
                                     MARK_ART_SIZE, MARK_ART_SIZE), image,
                              QRectF(0, 0, image.width(), image.height()))
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

    def __init__(self, controller: Any, *, reduced_motion: bool = False) -> None:
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self._controller, self._view = controller, _view_from(controller)
        self._reduced_motion, self._rotation, self._animation = reduced_motion, 0.0, None
        self._announcer = LiveSurfaceAnnouncer(self)
        self._follow_latest, self._unread_segments = True, 0
        self._word_targets: list[dict[str, object]] = []
        self._selection: dict[str, object] | None = None
        self._picker_active = False
        self._candidates: list[object] = []
        self._sent_receipts: list[str] = []
        self._picker_page = self._candidate_page = 0
        self._editor_mode = ""
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Samsara live surface")
        self.setAccessibleDescription("Passive microphone and draft status")

        self._mark = _Mark(self)
        self._mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._state = QLabel(self); self._state.setWordWrap(True)
        self._state.setAccessibleName("Live surface state")
        self._transcript = QTextBrowser(self)
        self._transcript.setOpenLinks(False); self._transcript.setOpenExternalLinks(False)
        self._transcript.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._transcript.setAccessibleName("Draft transcript")
        self._transcript.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._transcript.anchorClicked.connect(self._anchor_clicked)
        bar = self._transcript.verticalScrollBar()
        bar.valueChanged.connect(self._scroll_value_changed)
        self._provisional = QLabel(self); self._provisional.setWordWrap(True)
        self._provisional.setAccessibleName("Provisional transcript")
        self._badge = QLabel(self); self._badge.setText("▣  Draft")
        self._badge.setAccessibleName("Draft pending")
        self._latest = self._button("Read the latest draft text", "Latest", self._latest_clicked)
        self._latest.setAccessibleDescription("Resume following newly settled draft text")
        self._commit = self._button("Commit draft", "Commit", self.commit_requested.emit)
        self._clear = self._button("Clear draft", "Clear", self.clear_requested.emit)
        self._pause = self._button("Pause capture", "Pause", self.pause_requested.emit)
        self._correct = self._button("Correct a word", "Correct", self._request_correct)
        self._edit_receipt = self._button("Edit sent receipt as a new draft", "Edit as new draft", self._request_edit_receipt)
        self._collapse = self._button("Collapse surface", "Collapse", self.collapse_requested.emit)
        self._controls = (self._commit, self._clear, self._pause, self._correct, self._edit_receipt, self._collapse)
        self._picker_buttons = [self._button("Draft word", "", lambda n=n: self._choose_picker(n))
                                for n in range(4)]
        self._candidate_buttons = [self._button("Correction candidate", "", lambda n=n: self._choose_candidate(n))
                                   for n in range(4)]
        self._keep = self._button("Keep original word", "Keep", self._cancel_correction)
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
    def card_rect(self) -> QRect:
        return QRect() if self._view.form in (VisibleForm.MARK, VisibleForm.DRAFT_BADGE) else QRect(0, MARK_SIZE + CONNECTOR, self.width(), self.height() - MARK_SIZE - CONNECTOR)
    @property
    def card_content_rect(self) -> QRect: return self.card_rect.adjusted(CARD_INSET, CARD_INSET, -CARD_INSET, -CARD_INSET)
    def hit_region(self) -> QRegion: return self.mask()

    def refresh(self, view: SurfaceView | None = None, *, animate: bool = True) -> None:
        self._apply_view(_view_from(self._controller) if view is None else view, animate=animate)

    def set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = enabled
        if enabled and self._animation is not None: self._animation.stop(); self._animation = None

    def focus_review(self) -> None:
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False); self.show()
        for child in (*self.findChildren(QPushButton), *self.findChildren(QLineEdit),
                      *self.findChildren(QTextBrowser)):
            child.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._transcript.setFocus(Qt.FocusReason.ShortcutFocusReason)

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
        self._follow_latest = bar.value() >= bar.maximum()
        self.scroll_requested.emit(bar.value())

    def show_word_picker(self) -> None:
        self._selection = None; self._picker_active = True; self._picker_page = 0; self._candidates = []
        self._apply_view(self._view, animate=False); self._announce("picker", "Choose a word to correct")

    def show_candidates(self, selection: dict[str, object], candidates: list[object]) -> None:
        self._selection, self._picker_active, self._candidates, self._candidate_page = dict(selection), False, list(candidates)[:12], 0
        self._apply_view(self._view, animate=False); self._announce(("correction", selection.get("revision")), "Correction choices ready")

    def correction_refused(self, message: str = "That word changed. Select it again.") -> None:
        self._selection = None; self._picker_active = False; self._candidates = []
        self._apply_view(self._view, animate=False); self._announce("stale", message)

    def _apply_view(self, view: SurfaceView, *, animate: bool) -> None:
        previous, previous_text = self._view, self._view.text
        self._view = view; self._rotation = self._rotation if self._reduced_motion else self._rotation + 15.0
        if (view.document is DocumentState.DELIVERED and previous.document is not DocumentState.DELIVERED
                and view.text):
            self._sent_receipts.append(view.text)
            while len(self._sent_receipts) > 20 or sum(map(len, self._sent_receipts)) > 10_000:
                self._sent_receipts.pop(0)
        capture, eye = _capture_mark(view); self._mark.set_state(capture, eye, self._rotation)
        state = self._state_text(view)
        if self._selection is not None:
            state = f"Correct “{self._selection.get('word', '')}” — choose a replacement"
        elif self._picker_active:
            state = "Choose a word to correct"
        self._state.setText(state); self._render_transcript()
        self._provisional.setText(f"⋯ {view.provisional_text}" if view.provisional_text else "")
        words = len(view.text.split()); self._badge.setText(f"▤  Draft\n{words} word{'s' if words != 1 else ''}")
        if previous_text != view.text and previous_text and not self._follow_latest:
            self._unread_segments += 1
        target = self._form_rect(view.form)
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
        self._transcript.setHtml("<p>" + "".join(parts).replace("\n", "<br/>") + "</p>")

    def _form_rect(self, form: VisibleForm) -> QRect:
        if form is VisibleForm.MARK: return QRect(self.x(), self.y(), MARK_SIZE, MARK_SIZE)
        if form is VisibleForm.DRAFT_BADGE: return QRect(self.x(), self.y(), 120, MARK_SIZE)
        width, card_height = STATUS_SIZE if form is VisibleForm.STATUS else (LIVE_SIZE if form is VisibleForm.LIVE else REVIEW_SIZE)
        return QRect(self.x(), self.y(), width, MARK_SIZE + CONNECTOR + card_height)

    def _animate_geometry(self, target: QRect) -> None:
        animation = QPropertyAnimation(self, b"geometry", self); animation.setDuration(RESIZE_DURATION_MS)
        animation.setStartValue(self.geometry()); animation.setEndValue(target); animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(lambda _: (self._layout_children(), self._update_mask()))
        animation.finished.connect(lambda: (self._layout_children(), self._update_mask()))
        self._animation = animation; animation.start()

    def _hide_children(self) -> None:
        for child in (self._state, self._transcript, self._provisional, self._badge, self._latest,
                      *self._controls, *self._picker_buttons, *self._candidate_buttons,
                      self._keep, self._spell, self._edit, self._more, self._apply, self._cancel, self._editor): child.hide()

    def _layout_children(self) -> None:
        form = self._view.form; self._mark.setGeometry((self.width()-MARK_SIZE)//2, 0, MARK_SIZE, MARK_SIZE); self._hide_children()
        if form is VisibleForm.MARK: return
        if form is VisibleForm.DRAFT_BADGE:
            self._mark.setGeometry(0, 0, MARK_SIZE, MARK_SIZE); self._badge.setGeometry(MARK_SIZE, 0, 76, MARK_SIZE); self._badge.show(); return
        content = self.card_content_rect
        if form is VisibleForm.STATUS:
            control = self._clear if self._view.notice.kind is NoticeKind.ERROR else self._pause; width=112
            self._state.setGeometry(content.x(), content.y(), content.width()-width-REGION_GAP, content.height()); self._state.show()
            control.setGeometry(content.right()-width+1, content.y()+(content.height()-MARK_SIZE)//2, width, MARK_SIZE); control.show(); return
        self._state.setGeometry(content.x(), content.y(), content.width(), HEADER_HEIGHT); self._state.show()
        if form is VisibleForm.REVIEW and (self._selection is not None or self._picker_active): self._layout_correction(content); return
        transcript_h = 168 if form is VisibleForm.REVIEW else 96
        y = content.y()+HEADER_HEIGHT+REGION_GAP
        self._transcript.setGeometry(content.x(), y, content.width(), transcript_h); self._transcript.show()
        if form is VisibleForm.REVIEW:
            y += transcript_h+REGION_GAP; self._latest.setGeometry(content.x(), y, content.width(), MARK_SIZE)
            self._latest.setText("Latest" + (f" (+{self._unread_segments})" if self._unread_segments else "")); self._latest.setVisible(not self._follow_latest or self._unread_segments); y += MARK_SIZE+REGION_GAP
            controls = (self._clear,self._commit,self._pause,self._correct)
            if self._view.document is DocumentState.DELIVERED:
                controls = (self._edit_receipt, self._correct)
            self._layout_row(controls, content.x(), y, content.width())
        else:
            self._provisional.setGeometry(content.x(), y+transcript_h, content.width(), 24); self._provisional.show()
            self._layout_row((self._clear,self._commit,self._pause,self._correct), content.x(), y+transcript_h+24+REGION_GAP, content.width())

    def _layout_row(self, controls, x: int, y: int, width: int) -> None:
        available = width - REGION_GAP*(len(controls)-1); base, rem = divmod(available, len(controls))
        for index, control in enumerate(controls):
            w=base+(1 if index<rem else 0); control.setGeometry(x,y,w,MARK_SIZE); control.show(); x += w+REGION_GAP

    def _layout_correction(self, content: QRect) -> None:
        y=content.y()+HEADER_HEIGHT+REGION_GAP
        buttons = self._candidate_buttons if self._candidates else self._picker_buttons
        page = self._candidate_page if self._candidates else self._picker_page
        source = self._candidates if self._candidates else self._word_targets
        start=page*4
        for index, button in enumerate(buttons):
            choice = source[start+index] if start+index < len(source) else None
            if choice is None: button.hide(); continue
            label = getattr(choice, "replacement", None) if self._candidates else choice["word"]
            button.setText(f"{start+index+1}. {label}"); button.setAccessibleName(f"Choice {start+index+1}: {label}")
            button.setGeometry(content.x()+(index%2)*(content.width()//2+REGION_GAP//2), y+(index//2)*(MARK_SIZE+REGION_GAP), content.width()//2-REGION_GAP//2, MARK_SIZE); button.show()
        y += MARK_SIZE*2+REGION_GAP*2
        if self._selection is not None:
            self._layout_row((self._keep,self._spell,self._edit), content.x(), y, content.width()); y += MARK_SIZE+REGION_GAP
        if len(source)>start+4:
            self._more.setText("More"); self._more.setGeometry(content.x(), y, content.width(), MARK_SIZE); self._more.show()

    def _update_mask(self) -> None:
        if self._view.form is VisibleForm.MARK: region=QRegion(self._mark.geometry(), QRegion.RegionType.Ellipse)
        elif self._view.form is VisibleForm.DRAFT_BADGE:
            path=QPainterPath(); path.addRoundedRect(QRectF(0,0,self.width(),MARK_SIZE),CARD_RADIUS,CARD_RADIUS); region=QRegion(path.toFillPolygon().toPolygon())
        else:
            card=self.card_rect; region=QRegion(self._mark.geometry(),QRegion.RegionType.Ellipse); region |= QRegion(QRect((self.width()-CONNECTOR)//2,MARK_SIZE,CONNECTOR,CONNECTOR)); path=QPainterPath(); path.addRoundedRect(QRectF(card),CARD_RADIUS,CARD_RADIUS); region |= QRegion(path.toFillPolygon().toPolygon())
        self.setMask(region)

    def _restyle(self) -> None:
        self.setStyleSheet(f"LiveSurfaceWidget {{ background: transparent; }} QLabel,QTextBrowser {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; background: transparent; border: 0; }} QPushButton,QLineEdit {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: {CARD_RADIUS}px; font-size: {theme.TYPE_BODY}px; }}")
        self._state.setFont(theme.qfont(theme.TYPE_HEADING)); self._transcript.setFont(theme.qfont(theme.TYPE_BODY))
        font=theme.qfont(theme.TYPE_MIN); font.setItalic(True); self._provisional.setFont(font); self._badge.setFont(theme.qfont(theme.TYPE_MIN))
        self._provisional.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; border-top: 1px dashed {theme.BORDER};")

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        if self._view.form in (VisibleForm.MARK,VisibleForm.DRAFT_BADGE):
            if self._view.form is VisibleForm.MARK:return
            painter=QPainter(self)
            try: painter.setPen(QPen(theme.qcolor(theme.BORDER),1)); painter.setBrush(theme.qcolor(theme.BG1)); painter.drawRoundedRect(QRectF(.5,.5,self.width()-1,MARK_SIZE-1),CARD_RADIUS,CARD_RADIUS)
            finally:painter.end()
            return
        painter=QPainter(self)
        try:
            painter.setPen(QPen(theme.qcolor(theme.BORDER),1)); painter.setBrush(theme.qcolor(theme.BG1)); painter.drawRoundedRect(QRectF(self.card_rect).adjusted(.5,.5,-.5,-.5),CARD_RADIUS,CARD_RADIUS); painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(theme.qcolor(theme.BG2)); painter.drawRect((self.width()-CONNECTOR)//2,MARK_SIZE,CONNECTOR,CONNECTOR)
        finally:painter.end()

    def mousePressEvent(self,event: QMouseEvent)->None:
        if self._view.form in (VisibleForm.MARK,VisibleForm.DRAFT_BADGE) and self.mask().contains(event.position().toPoint()): self.show_requested.emit(); event.accept(); return
        super().mousePressEvent(event)

    def keyPressEvent(self,event: QKeyEvent)->None:
        if event.key()==Qt.Key.Key_Escape:
            if self._editor_mode:self._cancel_editor()
            elif self._selection is not None:self._cancel_correction()
            else:self._transcript.setFocus()
            event.accept(); return
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier: self.commit_requested.emit(); event.accept(); return
        if event.key()==Qt.Key.Key_Delete and event.modifiers() & Qt.KeyboardModifier.AltModifier: self.clear_requested.emit(); event.accept(); return
        if event.key()==Qt.Key.Key_Pause: self.pause_requested.emit(); event.accept(); return
        super().keyPressEvent(event)

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
        if self._candidates:self._candidate_page+=1
        else:self._picker_page+=1
        self._apply_view(self._view,animate=False)
    def _latest_clicked(self)->None:self._follow_latest=True; self._unread_segments=0; self._scroll_to_latest(); self._layout_children()
    def _scroll_value_changed(self,value:int)->None:
        bar=self._transcript.verticalScrollBar()
        if bar.maximum() and value<bar.maximum():self._follow_latest=False
    def _scroll_to_latest(self)->None:
        self._transcript.verticalScrollBar().setValue(self._transcript.verticalScrollBar().maximum())
    def _open_editor(self,mode:str)->None:
        self._editor_mode=mode; self._editor.clear(); self._editor.setAccessibleName("Spelled replacement" if mode=="spell" else "Edit replacement")
        content=self.card_content_rect; y=content.y()+HEADER_HEIGHT+REGION_GAP+MARK_SIZE*2+REGION_GAP*2
        self._editor.setGeometry(content.x(),y,content.width(),MARK_SIZE); self._editor.show(); self._layout_row((self._apply,self._cancel),content.x(),y+MARK_SIZE+REGION_GAP,content.width()); self._editor.setFocus(); self._announce("editor", "Enter a replacement, then apply")
    def _sanitize_spelling(self,value:str)->None:
        if self._editor_mode=="spell":
            cleaned="".join(ch for ch in value if ch.isalpha() or ch in " '-")
            if cleaned!=value:self._editor.blockSignals(True); self._editor.setText(cleaned); self._editor.blockSignals(False)
    def _apply_editor(self)->None:
        text=self._editor.text().strip()
        if text and self._selection is not None:self.correction_apply_requested.emit(dict(self._selection),text)
    def _cancel_editor(self)->None:self._editor_mode=""; self._editor.hide(); self._apply.hide(); self._cancel.hide(); self._layout_children()
    def _cancel_correction(self)->None:self._selection=None; self._picker_active=False; self._candidates=[]; self._editor_mode=""; self.correction_cancel_requested.emit(); self._apply_view(self._view,animate=False)
    def _announce(self,key:object,message:str)->None:self._announcer.announce_transition(key,message)
    def _state_text(self,view:SurfaceView)->str:
        if view.notice.message:return view.notice.message
        if view.capture is CaptureState.RECORDING:return "Recording"
        if view.capture is CaptureState.TRANSCRIBING:return "Transcribing"
        if view.capture is CaptureState.UNAVAILABLE:return "Microphone unavailable"
        if view.document is DocumentState.PARKED_DRAFT:return "Draft parked"
        if view.document is not DocumentState.EMPTY:return "Draft pending"
        if view.capture in (CaptureState.WAKE_ARMED,CaptureState.HANDS_FREE_LISTENING):return "Listening"
        return "Microphone idle"
    def _announce_transition(self,previous:SurfaceView,current:SurfaceView)->None:
        if previous.capture is not current.capture:self._announce(("capture",current.capture),self._state_text(current))
        elif previous.document is not current.document and current.document is DocumentState.PARKED_DRAFT:self._announce(("parked",current.document_id,current.revision),"Draft parked")
        elif previous.notice.kind is not current.notice.kind and current.notice.kind in (NoticeKind.ERROR,NoticeKind.CONFIRMATION):self._announce(("notice",current.notice.kind,current.notice.message),self._state_text(current))
