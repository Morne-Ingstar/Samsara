"""Qt click-through overlay for Show Numbers — numbered pill labels.

Frameless, transparent, always-on-top, WindowTransparentForInput so physical
mouse clicks pass straight through to the app below. Does not steal focus.
"""

from PySide6.QtCore import Qt, QRect, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QFont
from PySide6.QtWidgets import QApplication, QWidget

_PILL_BG  = QColor(18, 18, 22, 230)
_PILL_BD  = QColor(70, 70, 80, 200)
_TEXT_CLR = QColor(255, 255, 255, 255)


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
