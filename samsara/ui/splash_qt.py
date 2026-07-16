"""Code-native animated PySide6 splash screen for Samsara.

``SplashScreenQt`` owns no Qt thread.  Widget creation and every public
update are marshalled through :mod:`qt_runtime`, so callers may use the
object freely while startup work runs on background threads.
"""

from __future__ import annotations

import logging
import math
import threading
import time

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QStyle, QWidget

from samsara.runtime import thread_registry
from samsara.ui import qt_runtime

log = logging.getLogger(__name__)

_MIN_DISPLAY_S = 3.0
_COMPLETION_HOLD_MS = 450
_LOGICAL_W = 760.0
_LOGICAL_H = 430.0
_DISPLAY_SCALE = 0.85
_SIGNATURE_TEXT = "A Morne Ingstar Production"


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
    _progress_sig = Signal(object)
    _reduced_motion_sig = Signal(bool)
    _error_sig = Signal(str, str)
    _complete_sig = Signal(str, str)
    _close_sig = Signal()

    def __init__(self):
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
        self._progress: float | None = None
        self._reduced_motion = _system_reduced_motion()
        self._error = False
        self._complete = False
        self._completion_started_ms: int | None = None
        self._fade_started_ms: int | None = None

        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._frame_timer = QTimer(self)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.setInterval(33)  # hard cap at about 30 fps
        self._frame_timer.timeout.connect(self._on_frame)

        # Static knotwork is calculated once in logical coordinates.  The
        # procedural vortex remains deliberately small (three 121-point paths).
        self._knot_paths = self._make_knotwork()

        self._status_sig.connect(self._set_status)
        self._detail_sig.connect(self._set_detail)
        self._progress_sig.connect(self._set_progress)
        self._reduced_motion_sig.connect(self._set_reduced_motion)
        self._error_sig.connect(self._set_error)
        self._complete_sig.connect(self._set_complete)
        self._close_sig.connect(self._begin_close)

    @staticmethod
    def _make_knotwork() -> tuple[QPainterPath, ...]:
        paths: list[QPainterPath] = []
        centre = QPointF(380.0, 213.0)
        for phase in (0.0, math.pi / 2.0):
            path = QPainterPath()
            for index in range(145):
                angle = math.tau * index / 144.0
                radius = 142.0 + 35.0 * math.sin(3.0 * angle + phase)
                point = QPointF(
                    centre.x() + radius * math.cos(angle),
                    centre.y() + radius * 0.64 * math.sin(angle),
                )
                if index == 0:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
            paths.append(path)
        return tuple(paths)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._reduced_motion:
            self._frame_timer.start()

    def closeEvent(self, event):
        self._frame_timer.stop()
        event.accept()

    @Slot()
    def _on_frame(self):
        now = self._elapsed.elapsed()
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
        self._completion_started_ms = self._elapsed.elapsed()
        if not self._reduced_motion and self.isVisible():
            self._frame_timer.start()
        self.update()

    @Slot()
    def _begin_close(self):
        if self._completion_started_ms is not None:
            completion_age = self._elapsed.elapsed() - self._completion_started_ms
            remaining = _COMPLETION_HOLD_MS - completion_age
            if remaining > 0:
                QTimer.singleShot(remaining, self._begin_close)
                return
        if self._reduced_motion or not self.isVisible():
            self.close()
            return
        self._fade_started_ms = self._elapsed.elapsed()
        self._frame_timer.start()

    def paintEvent(self, event):  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.scale(self.width() / _LOGICAL_W, self.height() / _LOGICAL_H)
        self._paint_panel(painter)

        motion_s = 0.0 if self._reduced_motion else self._elapsed.elapsed() / 1000.0
        self._paint_knotwork(painter)
        self._paint_vortex(painter, motion_s)
        self._paint_progress(painter, motion_s)
        self._paint_left_sigil(painter, motion_s)
        self._paint_right_sigil(painter, motion_s)
        self._paint_text(painter)
        self._paint_completion_bloom(painter)
        painter.end()

    @staticmethod
    def _paint_panel(painter: QPainter):
        panel = QRectF(7.0, 7.0, 746.0, 416.0)
        gradient = QLinearGradient(0.0, 0.0, _LOGICAL_W, _LOGICAL_H)
        gradient.setColorAt(0.0, _color("#292c2e", 252))
        gradient.setColorAt(0.56, _color("#202326", 252))
        gradient.setColorAt(1.0, _color("#171a1d", 252))
        painter.setPen(QPen(_color("#5fcbd1", 30), 1.0))
        painter.setBrush(gradient)
        painter.drawRoundedRect(panel, 38.0, 38.0)

    def _paint_knotwork(self, painter: QPainter):
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_color("#4e9aa0", 18), 2.0))
        for path in self._knot_paths:
            painter.drawPath(path)
        painter.restore()

    def _paint_vortex(self, painter: QPainter, seconds: float):
        centre = QPointF(380.0, 213.0)
        palette = ("#70f1f1", "#f2c873", "#ef7668")
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for band, value in enumerate(palette):
            path = QPainterPath()
            phase = seconds * (0.56 + band * 0.04) + band * math.tau / 3.0
            for index in range(121):
                angle = math.tau * index / 120.0
                radius = (
                    76.0
                    + 13.0 * math.sin(3.0 * angle + phase)
                    + 3.5 * math.sin(7.0 * angle - phase * 0.7)
                )
                point = QPointF(
                    centre.x() + radius * math.cos(angle + phase * 0.1),
                    centre.y() + radius * math.sin(angle + phase * 0.1),
                )
                if index == 0:
                    path.moveTo(point)
                else:
                    path.lineTo(point)

            # Layered strokes provide a controlled glow without blur effects.
            painter.setPen(QPen(_color(value, 25), 11.0, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
            painter.setPen(QPen(_color(value, 105), 3.2, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
            painter.setPen(QPen(_color("#f8ffff", 195), 0.9))
            painter.drawPath(path)
        painter.restore()

    def _paint_progress(self, painter: QPainter, seconds: float):
        ring = QRectF(266.0, 99.0, 228.0, 228.0)
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_color("#6c818a", 70), 7.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, 0, 360 * 16)

        if self._error:
            span = 360.0
            start = 90.0
            active = "#ef7668"
        elif self._progress is None:
            span = 74.0
            start = 90.0 - (seconds * 42.0 % 360.0)
            active = "#70e8ec"
        else:
            span = 360.0 * self._progress
            start = 90.0
            active = "#f0d488" if self._complete else "#70e8ec"

        painter.setPen(QPen(_color(active, 30), 15.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, round(start * 16), round(-span * 16))
        painter.setPen(QPen(_color(active, 225), 5.0, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, round(start * 16), round(-span * 16))
        painter.restore()

    @staticmethod
    def _star_path(centre: QPointF, outer: float, inner: float,
                   points: int, rotation: float = 0.0) -> QPainterPath:
        path = QPainterPath()
        for index in range(points * 2 + 1):
            angle = rotation - math.pi / 2.0 + index * math.pi / points
            radius = outer if index % 2 == 0 else inner
            point = QPointF(centre.x() + math.cos(angle) * radius,
                            centre.y() + math.sin(angle) * radius)
            if index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        path.closeSubpath()
        return path

    def _paint_left_sigil(self, painter: QPainter, seconds: float):
        centre = QPointF(92.0, 330.0)
        flicker = 1.0 if self._reduced_motion else (
            0.92 + 0.08 * math.sin(seconds * 9.7) * math.sin(seconds * 4.1)
        )
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_color("#a9b9b6", 120), 2.0))
        painter.drawPath(self._star_path(centre, 43.0, 29.0, 8, seconds * 0.025))
        painter.setPen(QPen(_color("#d5a76f", 150), 1.4))
        painter.drawPath(self._star_path(centre, 32.0, 21.0, 4, -seconds * 0.035))

        glow = QRadialGradient(centre, 28.0)
        glow.setColorAt(0.0, _color("#ffd891", round(145 * flicker)))
        glow.setColorAt(0.45, _color("#f28a4a", round(65 * flicker)))
        glow.setColorAt(1.0, _color("#d75039", 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(centre, 29.0, 29.0)

        flame = QPainterPath()
        flame.moveTo(centre.x(), centre.y() + 20.0)
        flame.cubicTo(centre.x() - 20.0, centre.y() + 4.0,
                      centre.x() - 7.0, centre.y() - 8.0 * flicker,
                      centre.x() - 3.0, centre.y() - 22.0 * flicker)
        flame.cubicTo(centre.x() + 2.0, centre.y() - 11.0,
                      centre.x() + 22.0, centre.y() + 2.0,
                      centre.x(), centre.y() + 20.0)
        painter.setBrush(_color("#ffc46e", 220))
        painter.drawPath(flame)
        painter.restore()

    def _paint_right_sigil(self, painter: QPainter, seconds: float):
        centre = QPointF(668.0, 330.0)
        rotation = 0.0 if self._reduced_motion else seconds * 0.012
        painter.save()
        painter.setBrush(_color("#363c3e", 205))
        painter.setPen(QPen(_color("#9ca6a3", 130), 2.0))
        painter.drawEllipse(centre, 43.0, 43.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, 35.0, 35.0)
        painter.drawPath(self._star_path(centre, 30.0, 13.0, 6, rotation))
        painter.setPen(QPen(_color("#d3b884", 105), 1.2))
        painter.drawPath(self._star_path(centre, 22.0, 10.0, 6, -rotation))
        painter.restore()

    def _paint_text(self, painter: QPainter):
        painter.save()
        painter.setPen(_color("#6ee5e9"))
        painter.setFont(QFont("Segoe UI Variable Display", 29, QFont.Weight.DemiBold))
        painter.drawText(QRectF(0.0, 21.0, _LOGICAL_W, 47.0),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         "Samsara")

        painter.setPen(_color("#9da4a5", 190))
        painter.setFont(QFont("Segoe UI", 12, QFont.Weight.Normal, italic=True))
        painter.drawText(QRectF(0.0, 67.0, _LOGICAL_W, 25.0),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         "De-articulating Splines.")

        status_color = "#ef8a7f" if self._error else "#e3e7e6"
        painter.setPen(_color(status_color))
        painter.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        status = self._status
        if self._progress is not None and not self._complete and not self._error:
            status = f"{status}  [{round(self._progress * 100)}%]"
        painter.drawText(QRectF(145.0, 355.0, 470.0, 31.0),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         status)

        painter.setPen(_color("#a7adae", 190))
        painter.setFont(QFont("Segoe UI", 10))
        painter.drawText(QRectF(135.0, 386.0, 490.0, 24.0),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                         self._detail)

        # A deliberately quiet maker's mark, tucked inside the panel corner.
        signature_font = QFont("Segoe UI")
        signature_font.setPixelSize(18)
        painter.setFont(signature_font)
        painter.setPen(_color("#9aa3a4", 65))
        painter.drawText(QRectF(445.0, 388.0, 290.0, 32.0),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                         _SIGNATURE_TEXT)
        painter.restore()

    def _paint_completion_bloom(self, painter: QPainter):
        if self._completion_started_ms is None or self._reduced_motion:
            return
        age = self._elapsed.elapsed() - self._completion_started_ms
        if age < 0 or age > 800:
            return
        strength = math.sin(math.pi * age / 800.0)
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_color("#f2d891", round(100 * strength)),
                            8.0 + strength * 8.0))
        painter.drawEllipse(QPointF(380.0, 213.0),
                            121.0 + strength * 12.0,
                            121.0 + strength * 12.0)
        painter.restore()


class SplashScreenQt:
    """Thread-safe facade over the splash widget on the shared Qt runtime."""

    def __init__(self):
        self._start_time = time.time()
        self._widget: _SplashWidget | None = None
        self._widget_ready = threading.Event()

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
