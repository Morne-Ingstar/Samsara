"""Qt click-through overlay for Show Numbers — numbered pill labels.

Frameless, transparent, always-on-top, WindowTransparentForInput so physical
mouse clicks pass straight through to the app below. Does not steal focus.
"""

import ctypes
import ctypes.wintypes as _wt

from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont
from PySide6.QtWidgets import QApplication, QWidget

_PILL_BG  = QColor(18, 18, 22, 230)
_PILL_BD  = QColor(70, 70, 80, 200)
_TEXT_CLR = QColor(255, 255, 255, 255)

# ---------------------------------------------------------------------------
# DPI-aware physical-to-logical coordinate conversion
# ---------------------------------------------------------------------------

class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _wt.RECT),
        ("rcWork", _wt.RECT),
        ("dwFlags", ctypes.c_ulong),
    ]

_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    _wt.BOOL, _wt.HMONITOR, _wt.HDC, ctypes.POINTER(_wt.RECT), _wt.LPARAM
)


def _win32_monitor_rects() -> list:
    """Return physical monitor rects from Win32, sorted (left, top)."""
    rects = []

    def _cb(hmon, hdc, lprect, lparam):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = info.rcMonitor
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    return sorted(rects, key=lambda r: (r[0], r[1]))


def phys_to_logical(px: int, py: int) -> tuple:
    """Convert physical screen coordinates to Qt logical coordinates.

    UIA BoundingRectangle returns physical pixels. Qt uses logical (DPI-scaled)
    pixels. On a 200% DPI 4K monitor, physical (2000, 100) -> logical (1000, 50).
    Falls back to identity if conversion data is unavailable.
    """
    try:
        phys_rects = _win32_monitor_rects()
        qt_screens = sorted(
            QApplication.screens(),
            key=lambda s: (s.geometry().x(), s.geometry().y()),
        )
        if len(phys_rects) != len(qt_screens):
            return px, py

        for (pl, pt, pr, pb), screen in zip(phys_rects, qt_screens):
            if pl <= px < pr and pt <= py < pb:
                ratio = screen.devicePixelRatio()
                geo = screen.geometry()
                lx = geo.x() + (px - pl) / ratio
                ly = geo.y() + (py - pt) / ratio
                return round(lx), round(ly)
    except Exception:
        pass
    return px, py


class NumbersOverlayWindow(QWidget):
    """Fullscreen transparent click-through window spanning all monitors.

    Renders numbered pill labels via QPainter at absolute screen coordinates.
    Call update_labels() to refresh labels in place without recreating the window.
    """

    def __init__(self, labels: list) -> None:
        super().__init__(None)
        self._labels = labels   # list of [screen_x, screen_y, pill_w, pill_h, text]

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        virt = QRect()
        for screen in QApplication.screens():
            virt = virt.united(screen.geometry())
        self._virt = virt
        self.setGeometry(virt)

    def update_labels(self, labels: list) -> None:
        self._labels = labels
        self.update()

    def paintEvent(self, event) -> None:
        if not self._labels:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(font)

        ox = self._virt.x()
        oy = self._virt.y()

        for sx, sy, pw, ph, text in self._labels:
            rect = QRectF(sx - ox, sy - oy, pw, ph)

            path = QPainterPath()
            path.addRoundedRect(rect, 4.0, 4.0)
            painter.fillPath(path, _PILL_BG)

            painter.setPen(_PILL_BD)
            painter.drawPath(path)

            painter.setPen(_TEXT_CLR)
            painter.drawText(rect, Qt.AlignCenter, text)

        painter.end()
