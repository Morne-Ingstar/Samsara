"""Code-native animated PySide6 splash screen for Samsara.

``SplashScreenQt`` owns no Qt thread.  Widget creation and every public
update are marshalled through :mod:`qt_runtime`, so callers may use the
object freely while startup work runs on background threads.
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QApplication, QStyle, QWidget

from samsara.runtime import thread_registry
from samsara.ui import qt_runtime, theme
from samsara.ui.tray_qt import paint_mark

log = logging.getLogger(__name__)

_MIN_DISPLAY_S = 3.0
_COMPLETION_HOLD_MS = 450
_LOGICAL_W = 760.0
_LOGICAL_H = 430.0
_DISPLAY_SCALE = 0.85
_SIGNATURE_TEXT = "A Morne Ingstar Production"
_DETAIL_DELAY_MS = 8_000
_REASSURANCE_DELAY_MS = 15_000


def _color(value: str, alpha: int = 255) -> QColor:
    result = QColor(value)
    result.setAlpha(alpha)
    return result


def _system_reduced_motion() -> bool:
    """Use the platform style's animation preference when Qt exposes it."""
    try:
        app = QApplication.instance()
        if app is None:
            return False
        return not bool(app.style().styleHint(QStyle.StyleHint.SH_Widget_Animate))
    except (AttributeError, RuntimeError):
        # Older Qt/platform styles may not expose the hint.  Animation remains
        # conservative (30 fps) and callers can still opt out explicitly.
        return False


class _SplashWidget(QWidget):
    """Single, inexpensive QPainter scene capped at 30 frames per second."""

    _status_sig = Signal(str)
    _detail_sig = Signal(str)
    _ready_ladder_sig = Signal(object)
    _progress_sig = Signal(object)
    _reduced_motion_sig = Signal(bool)
    _error_sig = Signal(str, str)
    _complete_sig = Signal(str, str)
    _close_sig = Signal()

    def __init__(self, clock_ms=None):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen,
        )
        self.setObjectName("samsaraStartupSplash")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRectF(0, 0, 800, 600).toRect()
        # Keep the scene in its original logical coordinate system, but make
        # the default window 15% smaller.  QPainter's transform below scales
        # the whole scene uniformly, so the artwork keeps its proportions.
        target_width = _LOGICAL_W * _DISPLAY_SCALE
        target_height = _LOGICAL_H * _DISPLAY_SCALE
        fit = min(1.0, available.width() * 0.92 / target_width,
                  available.height() * 0.88 / target_height)
        width = max(442, round(target_width * fit))
        height = max(250, round(target_height * fit))
        self.setFixedSize(width, height)
        self.move(
            available.center().x() - width // 2,
            available.center().y() - height // 2,
        )

        self._status = "Starting Samsara"
        self._detail = "Preparing voice services..."
        self._ready_ladder = (
            ("hotkey: loading\u2026", "loading"),
            ("wake: checking\u2026", "checking"),
            ("Ava: checking\u2026", "checking"),
        )
        self._progress: float | None = None
        self._reduced_motion = _system_reduced_motion()
        self._error = False
        self._complete = False
        self._completion_started_ms: int | None = None
        self._fade_started_ms: int | None = None

        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._clock_ms = clock_ms or self._elapsed.elapsed
        self._frame_timer = QTimer(self)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.setInterval(33)  # hard cap at about 30 fps
        self._frame_timer.timeout.connect(self._on_frame)

        self._status_sig.connect(self._set_status)
        self._detail_sig.connect(self._set_detail)
        self._ready_ladder_sig.connect(self._set_ready_ladder)
        self._progress_sig.connect(self._set_progress)
        self._reduced_motion_sig.connect(self._set_reduced_motion)
        self._error_sig.connect(self._set_error)
        self._complete_sig.connect(self._set_complete)
        self._close_sig.connect(self._begin_close)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._reduced_motion:
            self._frame_timer.start()

    def closeEvent(self, event):
        self._frame_timer.stop()
        log.info("[SPLASH] closed")
        event.accept()

    @Slot()
    def _on_frame(self):
        now = self._now_ms()
        if self._fade_started_ms is not None:
            fraction = min(1.0, (now - self._fade_started_ms) / 260.0)
            self.setWindowOpacity(1.0 - fraction)
            if fraction >= 1.0:
                self._frame_timer.stop()
                self.close()
                return
        self.update()

    @Slot(str)
    def _set_status(self, text: str):
        self._status = str(text).strip() or "Starting Samsara"
        self.update()

    @Slot(object)
    def _set_ready_ladder(self, lines):
        self._ready_ladder = tuple((str(text), str(state)) for text, state in lines)
        self.update()

    @Slot(str)
    def _set_detail(self, text: str):
        self._detail = str(text).strip()
        self.update()

    @Slot(object)
    def _set_progress(self, value):
        if value is None:
            self._progress = None
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                log.debug("Ignoring invalid splash progress value %r", value)
                return
            if number > 1.0:
                number /= 100.0
            self._progress = max(0.0, min(1.0, number))
        self.update()

    @Slot(bool)
    def _set_reduced_motion(self, enabled: bool):
        self._reduced_motion = bool(enabled)
        if self._reduced_motion:
            self._frame_timer.stop()
        elif self.isVisible():
            self._frame_timer.start()
        self.update()

    @Slot(str, str)
    def _set_error(self, text: str, detail: str):
        self._error = True
        self._complete = False
        self._status = text.strip() or "Startup could not complete"
        self._detail = detail.strip()
        self.update()

    @Slot(str, str)
    def _set_complete(self, text: str, detail: str):
        self._error = False
        self._complete = True
        self._progress = 1.0
        self._status = text.strip() or "Samsara ready"
        self._detail = detail.strip()
        self._completion_started_ms = self._now_ms()
        if not self._reduced_motion and self.isVisible():
            self._frame_timer.start()
        self.update()

    @Slot()
    def _begin_close(self):
        if self._completion_started_ms is not None:
            completion_age = self._now_ms() - self._completion_started_ms
            remaining = _COMPLETION_HOLD_MS - completion_age
            if remaining > 0:
                QTimer.singleShot(remaining, self._begin_close)
                return
        if self._reduced_motion or not self.isVisible():
            self.close()
            return
        self._fade_started_ms = self._now_ms()
        self._frame_timer.start()

    def _now_ms(self) -> int:
        return int(self._clock_ms())

    def _compact(self) -> bool:
        return self.width() <= 442 or self.height() <= 250

    def _layout(self) -> dict[str, QRectF]:
        if self._compact():
            return {"word": QRectF(24, 8, self.width()-48, 34), "tag": QRectF(24, 44, self.width()-48, 22),
                    "ring": QRectF(self.width()/2-32, 75, 64, 64), "mark": QRectF(self.width()/2-22, 85, 44, 44),
                    "status": QRectF(24, 148, self.width()-48, 24), "strip": QRectF(24, 176, self.width()-48, 22),
                    "support": QRectF(24, 200, self.width()-48, 22), "credit": QRectF(54, 220, self.width()-108, 18)}
        return {"word": QRectF(24, 14, self.width()-48, 46), "tag": QRectF(24, 64, self.width()-48, 24),
                "ring": QRectF(self.width()/2-64, 104, 128, 128), "mark": QRectF(self.width()/2-45, 123, 90, 90),
                "status": QRectF(24, 248, self.width()-48, 26), "strip": QRectF(24, 280, self.width()-48, 24),
                "support": QRectF(24, 308, self.width()-48, 24), "credit": QRectF(54, 336, self.width()-108, 18)}

    def _support_text(self) -> str:
        if self._error or any(state in {"offline", "unavailable", "error"}
                              for _line, state in self._ready_ladder):
            return self._detail
        if self._now_ms() >= _REASSURANCE_DELAY_MS:
            return f"Still starting — {self._detail}" if self._detail else "Still starting — preparing voice services."
        if self._detail and self._now_ms() >= _DETAIL_DELAY_MS:
            return self._detail
        return ""

    @staticmethod
    def _segment(line: str, state: str, *, detailed: bool = False) -> tuple[str, str]:
        name = line.split(":", 1)[0].split()[0].title() or "Service"
        if state == "ready":
            return (f"{name} ✓ Ready" if detailed else f"{name} ✓"), theme.SUCCESS
        if state in {"offline", "unavailable", "error"}:
            return f"{name} × Unavailable", theme.ERROR
        return (f"{name} ◌ Working" if detailed else f"{name} ◌"), theme.WARNING

    def paintEvent(self, event):  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._paint_panel(painter)

        motion_s = 0.0 if self._reduced_motion else self._now_ms() / 1000.0
        layout = self._layout()
        self._paint_vortex(painter, motion_s, layout["mark"])
        self._paint_progress(painter, motion_s, layout["ring"])
        self._paint_text(painter)
        painter.end()

    @staticmethod
    def _paint_panel(painter: QPainter):
        panel = QRectF(7.0, 7.0, painter.device().width() - 14.0, painter.device().height() - 14.0)
        gradient = QLinearGradient(0.0, 0.0, panel.width(), panel.height())
        # The existing cool surface ladder (theme BG2 -> BG1 -> BG0).
        gradient.setColorAt(0.0, _color(theme.BG2, 252))
        gradient.setColorAt(0.56, _color(theme.BG1, 252))
        gradient.setColorAt(1.0, _color(theme.BG0, 252))
        painter.setPen(QPen(_color(theme.ACCENT, 30), 1.0))
        painter.setBrush(gradient)
        painter.drawRoundedRect(panel, 38.0, 38.0)

    #: The brand mark: cyan wheel, lid closed (tray_qt.APP_MARK). The splash
    #: never uses RECORDING -- nothing is being captured.
    _MARK = ("listening", "asleep")
    _MARK_SIZE = 150.0
    _MARK_SPIN_DEG_PER_S = 42.0

    def _paint_vortex(self, painter: QPainter, seconds: float, rect: QRectF | None = None):
        """The Samsara mark at the centre, drawn by the shared routine
        (tray_qt.paint_mark); the wheel spins while startup works."""
        rect = rect or self._layout()["mark"]
        capture, eye = self._MARK
        paint_mark(painter, rect, capture, eye, rotation=0.0)

    def _paint_progress(self, painter: QPainter, seconds: float, ring: QRectF | None = None):
        ring = ring or self._layout()["ring"]
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        stroke = 3.0 if self._compact() else 5.0
        painter.setPen(QPen(_color(theme.ICON_IDLE, 110), stroke, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, 0, 360 * 16)

        if self._error:
            span = 360.0
            start = 90.0
            active = theme.ERROR
        elif self._progress is None:
            span = 74.0
            start = 90.0 - (seconds * 42.0 % 360.0)
            active = theme.ACCENT
        else:
            span = 360.0 * self._progress
            start = 90.0
            active = theme.SUCCESS if self._complete else theme.ACCENT

        painter.setPen(QPen(_color(active, 35), stroke + 6.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, round(start * 16), round(-span * 16))
        painter.setPen(QPen(_color(active), stroke, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, round(start * 16), round(-span * 16))
        painter.restore()

    def _paint_text(self, painter: QPainter):
        painter.save()
        layout = self._layout()
        painter.setPen(_color(theme.ACCENT))
        painter.setFont(theme.qfont(28 if self._compact() else theme.TYPE_HERO, "Segoe UI Variable Display", QFont.Weight.DemiBold))
        painter.drawText(layout["word"],
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         "Samsara")

        painter.setPen(_color(theme.ICON_IDLE))
        painter.setFont(theme.qfont(theme.TYPE_BODY, weight=QFont.Weight.Normal, italic=True))
        painter.drawText(layout["tag"],
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         "De-articulating Splines.")

        strip = layout["strip"]
        cell_width = (strip.width() - 16.0) / 3.0
        for index, (line, state) in enumerate(self._ready_ladder[:3]):
            label, line_color = self._segment(
                line, state if state == "ready" else "unavailable" if self._error else state,
                detailed=self._error or state in {"offline", "unavailable", "error"},
            )
            painter.setPen(_color(line_color))
            painter.setFont(theme.qfont(theme.TYPE_MIN, weight=QFont.Weight.DemiBold))
            painter.drawText(QRectF(strip.x() + index * (cell_width + 8.0), strip.y(), cell_width, strip.height()),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                             label)

        status_color = theme.ERROR if self._error else theme.TEXT_PRIMARY
        painter.setPen(_color(status_color))
        painter.setFont(theme.qfont(theme.TYPE_HEADING, weight=QFont.Weight.DemiBold))
        status = self._status
        if self._progress is not None and not self._complete and not self._error:
            status = f"{status}  [{round(self._progress * 100)}%]"
        painter.drawText(layout["status"],
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         status)

        support = self._support_text()
        if support:
            painter.setPen(_color(theme.ICON_IDLE))
            painter.setFont(theme.qfont(theme.TYPE_BODY))
            painter.drawText(layout["support"], Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, support)

        # A deliberately quiet maker's mark, tucked inside the panel corner.
        signature_font = theme.qfont(theme.TYPE_MIN)
        painter.setFont(signature_font)
        painter.setPen(_color(theme.ICON_IDLE))
        painter.drawText(layout["credit"],
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                         _SIGNATURE_TEXT)
        painter.restore()

class SplashScreenQt:
    """Thread-safe facade over the splash widget on the shared Qt runtime."""

    def __init__(self):
        self._start_time = time.time()
        self._widget: _SplashWidget | None = None
        self._widget_ready = threading.Event()
        self._ready_ladder_observer = None

        qt_runtime.ensure_started()
        qt_runtime.post(self._create_widget)
        if not self._widget_ready.wait(timeout=5.0):
            log.warning("SplashScreenQt: widget not created within 5 s")

    def _create_widget(self):
        self._widget = _SplashWidget()
        self._widget.destroyed.connect(self._on_widget_destroyed)
        self._widget.show()
        self._widget_ready.set()

    def _on_widget_destroyed(self):
        self._widget = None

    def set_status(self, text: str):
        """Update the primary startup message. Thread-safe."""
        widget = self._widget
        if widget is not None:
            widget._status_sig.emit(str(text))
        observer = self._ready_ladder_observer
        if observer is not None:
            observer(str(text))

    def set_ready_ladder_observer(self, observer):
        """Observe status milestones without coupling this Qt facade to boot."""
        self._ready_ladder_observer = observer

    def set_ready_ladder(self, lines):
        """Set text-labelled capability states. Thread-safe."""
        widget = self._widget
        if widget is not None:
            widget._ready_ladder_sig.emit(tuple(lines))

    def set_detail(self, text: str):
        """Update the smaller explanatory line. Thread-safe."""
        widget = self._widget
        if widget is not None:
            widget._detail_sig.emit(str(text))

    def set_progress(self, value: float | int | None):
        """Set determinate progress (0..1 or 0..100); ``None`` is indeterminate."""
        widget = self._widget
        if widget is not None:
            widget._progress_sig.emit(value)

    def set_reduced_motion(self, enabled: bool):
        """Freeze decorative motion while retaining progress/status updates."""
        widget = self._widget
        if widget is not None:
            widget._reduced_motion_sig.emit(bool(enabled))

    def set_error(self, text: str, detail: str = ""):
        """Show a persistent error state without closing the splash."""
        widget = self._widget
        if widget is not None:
            widget._error_sig.emit(str(text), str(detail))

    def complete(self, text: str = "Samsara ready", detail: str = "Startup complete."):
        """Set progress to 100% and play a brief, restrained completion bloom."""
        widget = self._widget
        if widget is not None:
            widget._complete_sig.emit(str(text), str(detail))

    def close(self):
        """Dismiss the splash after its minimum display period, without blocking."""
        def _do_close():
            remaining = _MIN_DISPLAY_S - (time.time() - self._start_time)
            if remaining > 0:
                time.sleep(remaining)
            widget = self._widget
            if widget is not None:
                widget._close_sig.emit()

        thread_registry.spawn("splash-close", _do_close, daemon=True)
