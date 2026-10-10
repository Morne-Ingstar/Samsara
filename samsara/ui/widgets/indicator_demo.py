"""Illustrative listening displays only: no capture, config or real surface."""
import math

from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QLabel, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from samsara.ui import theme
from samsara.ui.live_surface_accessibility import system_reduced_motion
from samsara.ui.tray_qt import render_mark

# PLACEHOLDER COPY (490)
CAPTIONS = (
    "This is where Samsara shows that it is listening and what it did — keep it where you can always see it.",
    "Hold your dictation key to speak; the red mark shows that Samsara is listening.",
    "Let go of the key: your words are typed, and the display says so.",
    "After a command, the display tells you what Samsara did.",
    "Put the display where you can always see it; you can also drag the real one anywhere.",
)
POSITIONS = ("top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right")


class DesktopSketch(QWidget):
    """Shared scalable desktop drawing; all geometry is in logical pixels."""
    def __init__(self, parent=None, *, display="full"):
        super().__init__(parent)
        if display not in ("compact", "full"):
            raise ValueError(display)
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self.display = display
        self.beat = 0
        self.phase = 0.0
        self.still = False
        self.drawing_scale = 1.0
        self.setMinimumSize(320, 190)
        # PLACEHOLDER COPY (490)
        self.setAccessibleName(f"{display.title()} listening display illustration")

    def screen_rect(self):
        return QRectF(8, 8, self.width()/self.drawing_scale - 16, self.height()/self.drawing_scale - 16)

    def work_rect(self):
        return self.screen_rect().adjusted(0, 0, 0, -28)

    def target_rect(self, position, width=44, height=44):
        area = self.work_rect().adjusted(10, 10, -10, -10)
        vertical, horizontal = position.split("-")
        x = {"left": area.left(), "center": area.center().x() - width / 2,
             "right": area.right() - width}[horizontal]
        y = area.top() if vertical == "top" else area.bottom() - height
        return QRectF(x, y, width, height)

    def indicator_rect(self):
        receipt = bool(self.result_message())
        width = min(210 if self.display == "full" or receipt else (154 if self.beat == 1 else 104), self.work_rect().width() - 20)
        height = (96 if receipt else 60) if self.display == "full" else (68 if receipt else 36)
        start = self.target_rect("bottom-left", width, height)
        if self.beat != 4:
            return start
        end = self.target_rect("top-right", width, height)
        fraction = 1.0 if self.still else min(1.0, self.phase / 0.65)
        return QRectF(start.x() + (end.x() - start.x()) * fraction,
                      start.y() + (end.y() - start.y()) * fraction, width, height)

    def paint_desktop(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
        painter.setBrush(theme.qcolor(theme.BG2))
        painter.drawRoundedRect(self.screen_rect(), 8, 8)
        screen = self.screen_rect()
        painter.fillRect(QRectF(screen.x(), screen.bottom() - 28, screen.width(), 28), theme.qcolor(theme.BG1))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(theme.TEXT_PRIMARY))
        for x in (0, 8):
            for y in (0, 8):
                painter.drawRect(QRectF(screen.x() + 12 + x, screen.bottom() - 22 + y, 6, 6))
        window = QRectF(screen.x() + screen.width()*.18, screen.y() + 12,
                        screen.width()*.65, self.work_rect().height()-24)
        if self.beat == 3:
            window = self.work_rect().adjusted(4, 4, -4, -4)
        painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
        painter.setBrush(theme.qcolor(theme.BG0))
        painter.drawRoundedRect(window, 4, 4)
        painter.drawLine(window.topLeft() + QPointF(0, 19), window.topRight() + QPointF(0, 19))
        if self.beat >= 3 or (self.beat == 2 and (self.still or self.phase >= 0.4)):
            painter.setPen(theme.qcolor(theme.TEXT_PRIMARY))
            # PLACEHOLDER COPY (490): illustrative sample, not captured speech.
            painter.drawText(window.adjusted(12, 28, -12, -8), "A new thought.")

    def demo_view(self):
        from samsara.live_surface.model import LiveSurfaceModel, Lane, CaptureState
        model = LiveSurfaceModel(clock=lambda: 0)
        model.lane = Lane.COMMAND if self.beat == 3 else Lane.HOLD
        model.capture = (CaptureState.RECORDING if self.beat == 1 else
                         CaptureState.TRANSCRIBING if self.working() else CaptureState.OFF)
        return model.view()

    def working(self):
        return self.beat == 2 and not self.still and self.phase < 0.4

    def result_message(self):
        # The same producer wording that supplies both real displays.
        from samsara.session_modes import outcome_chip, COMMAND_CHIP_COPY
        if self.beat == 2 and not self.working():
            return outcome_chip("dictate_injected")[0]
        if self.beat == 3:
            # PLACEHOLDER COPY (490): illustrative action, no command is run.
            return COMMAND_CHIP_COPY["chain_done"].format(done="window maximised")
        return ""

    def state_words(self):
        from types import SimpleNamespace
        from samsara.ui.live_surface_qt import LiveSurfaceWidget
        view = self.demo_view()
        proxy = SimpleNamespace(_parked_idle=getattr(LiveSurfaceWidget, '_parked_idle', lambda view: False))
        return (LiveSurfaceWidget._lane_text(proxy, view),
                LiveSurfaceWidget._state_text(proxy, view))

    def paint_indicator(self, painter, rect):
        from types import SimpleNamespace
        from samsara.ui.live_surface_qt import render_device_mark, _mark_color
        from samsara.live_surface.feedback import FeedbackKind, FILL_TOKENS, paint_glyph
        from samsara.ui.listening_indicator import ListeningIndicator, _chip_fill
        header, subline = self.state_words()
        message = self.result_message()
        state = "recording" if self.beat == 1 else "transcribing" if self.working() else "idle"
        rotation = 0 if self.still else self.phase * 360 if state != "idle" else 0
        dpr = max(1.0, painter.transform().m11() * painter.device().devicePixelRatioF())
        painter.save()
        if self.display == "full":
            painter.setBrush(theme.qcolor(theme.BG1))
            painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
            painter.drawRoundedRect(rect, theme.CARD_RADIUS, theme.CARD_RADIUS)
            mark = render_device_mark("recording" if state == "recording" else "idle", "asleep",
                                      dpr=dpr, rotation=rotation, color=_mark_color(state))
            painter.drawImage(QPointF(rect.x()+8, rect.y()+12), mark)
            font = theme.qfont(theme.TYPE_BODY)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(theme.qcolor(theme.ACCENT))
            painter.drawText(rect.adjusted(52, 6, -8, -30), Qt.AlignmentFlag.AlignLeft, header)
            painter.setFont(theme.qfont(theme.TYPE_MIN))
            painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
            painter.drawText(QRectF(rect.x()+52, rect.y()+30, rect.width()-60, 22), subline)
            if message:
                band = QRectF(rect.x()+8, rect.y()+60, rect.width()-16, 28)
                fill = getattr(theme, FILL_TOKENS[FeedbackKind.SUCCESS])
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.qcolor(fill))
                painter.drawRoundedRect(band, 8, 8)
                ink = theme.qcolor(theme.ink_on(fill))
                paint_glyph(painter, FeedbackKind.SUCCESS,
                            QRectF(band.x()+4, band.y()+4, 20, 20), ink)
                painter.setPen(ink)
                painter.drawText(band.adjusted(32, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, message)
        else:
            # Reuse the real pill painter and colour resolver without constructing
            # a display widget, timers, controller, or native overlay window.
            proxy = SimpleNamespace(_pulse_step=0, _unlocked=False, _flash_bg=None,
                _session_mode_name=None, _thinking=self.working(), _snoozed=False,
                _command_mode=self.beat == 3, _listening=self.beat == 1, _mode_text=header)
            proxy._glyph_mark = lambda: ("recording" if state == "recording" else "idle", "asleep", rotation, 1.0)
            proxy._font = lambda: ListeningIndicator._font(proxy)
            pill = QRectF(rect.x(), rect.bottom()-36, 104, 36)
            bg, fg, label, glyph = ListeningIndicator._resolve_colors(proxy)
            ListeningIndicator._paint_pill(proxy, painter, pill, bg, fg, label, glyph)
            if message:
                proxy._chip_font = lambda: ListeningIndicator._chip_font(proxy)
                ListeningIndicator._paint_chip(proxy, painter,
                    QRectF(rect.x(), rect.y(), rect.width(), 26), message, "success")
        if self.beat == 1:
            painter.setPen(QPen(theme.qcolor(theme.RECORDING), 2))
            for index in range(6):
                level = 3 + (4 if self.still else 4*(1+math.sin(self.phase*24+index)))
                x, y = rect.right()-52+index*7, rect.bottom()-7
                painter.drawLine(QPointF(x, y), QPointF(x, y-level))
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.scale(self.drawing_scale, self.drawing_scale)
        try:
            self.paint_desktop(painter)
            self.paint_indicator(painter, self.indicator_rect())
        finally:
            painter.end()


class _StepDot(QPushButton):
    """Small painted dot inside a 44-pixel native keyboard/tap control."""
    def __init__(self):
        super().__init__()
        self.setFixedSize(44, 44)
        self.setProperty('walkthrough_detail', True)  # Enter advances the still, not setup.

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.ACCENT if self.isChecked() else theme.TEXT_SECONDARY), 1))
        painter.setBrush(theme.qcolor(theme.ACCENT) if self.isChecked() else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(17, 17, 10, 10))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
            painter.drawEllipse(QRectF(12, 12, 20, 20))
        painter.end()


class IndicatorDemo(QWidget):
    """12-second loop, or five manually reachable reduced-motion stills."""
    beat_changed = Signal(int)
    BEAT_MS = 2400
    LOOP_MS = 12000

    def __init__(self, parent=None, *, reduced_motion=None, display="full"):
        super().__init__(parent)
        self.reduced_motion = system_reduced_motion() if reduced_motion is None else bool(reduced_motion)
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self.setMinimumWidth(540)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; font-family: {theme.FONT_FAMILY};")
        # PLACEHOLDER COPY (490)
        self.setAccessibleName(f"{display.title()} listening display demonstration")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 16, 28, 16)
        layout.setSpacing(12)
        self.desktop = DesktopSketch(self, display=display)
        self.desktop.still = self.reduced_motion
        layout.addWidget(self.desktop, 1)
        self.caption = QLabel()
        self.caption.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        self.caption.setWordWrap(True)
        self.caption.setMinimumHeight(44)
        layout.addWidget(self.caption)
        self.steps = []
        row = QHBoxLayout()
        row.setSpacing(0)
        row.addStretch()
        names = ("rest", "listening", "typed result", "command result", "placement")
        for index in range(5):
            button = _StepDot()
            button.setCheckable(True)
            button.setAccessibleName(f"Step {index+1} of 5: {names[index]}")
            button.clicked.connect(lambda checked=False, i=index: self.set_frame(i))
            row.addWidget(button)
            self.steps.append(button)
        row.addStretch()
        layout.addLayout(row)
        for button in self.steps:
            button.setVisible(self.reduced_motion)
        self._elapsed = None if self.reduced_motion else QElapsedTimer()
        self.timer = None if self.reduced_motion else QTimer(self)
        if self.timer is not None:
            self.timer.setInterval(40)
            self.timer.timeout.connect(self._tick)
        self._observed_window = None
        self.set_frame(0)

    @property
    def beat(self):
        return self.desktop.beat

    def set_frame(self, beat, phase=1.0):
        if not 0 <= beat < 5:
            raise ValueError(beat)
        changed = beat != self.desktop.beat
        self.desktop.beat, self.desktop.phase = beat, phase
        self.caption.setText(CAPTIONS[beat])
        self.caption.setAccessibleName(CAPTIONS[beat])
        for index, button in enumerate(self.steps):
            button.setChecked(index == beat)
        self.desktop.update()
        if changed:
            self.beat_changed.emit(beat)

    def seek(self, milliseconds):
        elapsed = milliseconds % self.LOOP_MS
        self.set_frame(elapsed // self.BEAT_MS, (elapsed % self.BEAT_MS) / self.BEAT_MS)

    def _tick(self):
        if not self.isVisible() or not self.window().isVisible() or self.window().isMinimized():
            self.timer.stop()
            return
        self.seek(self._elapsed.elapsed())

    def _restart(self):
        self.set_frame(0, 0)
        if self.timer is not None and self.isVisible() and not self.window().isMinimized():
            self._elapsed.start()
            self.timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        window = self.window()
        if self._observed_window is not window:
            if self._observed_window is not None:
                self._observed_window.removeEventFilter(self)
            self._observed_window = window
            window.installEventFilter(self)
        self._restart()

    def _stop(self):
        if self.timer is not None:
            self.timer.stop()
        if self._elapsed is not None:
            self._elapsed.invalidate()
        if self._observed_window is not None:
            self._observed_window.removeEventFilter(self)
            self._observed_window = None

    def hideEvent(self, event):
        self._stop()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._observed_window and event.type() == QEvent.Type.WindowStateChange:
            if watched.isMinimized() and self.timer is not None:
                self.timer.stop()
            elif self.isVisible():
                self._restart()
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        self._stop()
        event.ignore()
        self.hide()
