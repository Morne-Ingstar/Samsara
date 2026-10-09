"""A static accessible QLabel whose one named word rarely turns in place."""
from __future__ import annotations

import random
import re
import math
import time

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QPainter, QPixmap, QRegion, QTextLayout, QTextOption
from PySide6.QtWidgets import QLabel, QWidget

from samsara.ui import splash_qt


class SpinWordLabel(QLabel):
    """Keep QLabel's rest painting, wrapping, metrics and accessibility.

    Animation transforms only the named word's ink in a transparent render
    of this label. No text/value changes or accessibility events occur.
    """

    def __init__(self, text, word="spins", parent=None, *, interval_bounds=(300_000, 600_000), duration_ms=700):
        super().__init__(text, parent)
        low, high = interval_bounds
        if not 0 < low <= high or duration_ms <= 0:
            raise ValueError("positive ordered intervals and duration required")
        self.interval_bounds = (int(low), int(high))
        self.word = word
        self._angle = 0.0
        self._layer = None
        self._capturing = False
        self._due_at = None
        self._word_rect = QRectF()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._scheduled_spin)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(int(duration_ms))
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(360.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._turn)
        self._animation.finished.connect(self._land)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(text)

    def setText(self, text):
        if hasattr(self, "_animation"):
            self._cancel()
        super().setText(text)
        self.setAccessibleName(text)
        if hasattr(self, "_timer"):
            self._arm()

    def _match(self):
        return re.search(r"(?<!\w)" + re.escape(self.word) + r"(?!\w)", self.text()) if self.word else None

    def _arm(self):
        self._timer.stop()
        if self.isVisible() and self._match() and not splash_qt._system_reduced_motion():
            delay = random.randint(*self.interval_bounds)
            self._due_at = time.monotonic() + delay / 1000
            self._timer.start(delay)

    def _scheduled_spin(self):
        if not self.isVisible() or splash_qt._system_reduced_motion():
            self._timer.stop()
            return
        # Platform timer resolution must never shorten the chosen interval.
        remaining = (self._due_at or 0) - time.monotonic()
        if remaining > 0:
            self._timer.start(max(1, math.ceil(remaining * 1000)))
            return
        self.spin_once()
        if self._layer is None:
            self._arm()

    def showEvent(self, event):
        super().showEvent(event)
        self._arm()

    def hideEvent(self, event):
        self._cancel()
        super().hideEvent(event)

    def _cancel(self):
        self._timer.stop()
        self._animation.stop()
        self._due_at = None
        self._layer = None
        self._angle = 0.0
        self.update()

    def _locate_word(self, match):
        rect = QRectF(self.contentsRect()).adjusted(self.margin(), self.margin(), -self.margin(), -self.margin())
        layout = QTextLayout(self.text(), self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap if self.wordWrap() else QTextOption.WrapMode.NoWrap)
        option.setAlignment(self.alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask)
        layout.setTextOption(option)
        lines = []
        layout.beginLayout()
        y = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            line.setPosition(QPointF(0, y))
            lines.append(line)
            y += line.height()
        layout.endLayout()
        top = rect.top()
        if self.alignment() & Qt.AlignmentFlag.AlignVCenter:
            top += (rect.height() - y) / 2
        elif self.alignment() & Qt.AlignmentFlag.AlignBottom:
            top += rect.height() - y
        for line in lines:
            if line.textStart() <= match.start() and match.end() <= line.textStart() + line.textLength():
                x1 = line.cursorToX(match.start())[0]
                x2 = line.cursorToX(match.end())[0]
                return QRectF(rect.left()+min(x1,x2), top+line.y(), abs(x2-x1), line.height()).toAlignedRect()
        return None

    def spin_once(self):
        if not self.isVisible() or self._layer is not None or splash_qt._system_reduced_motion():
            return
        match = self._match()
        if not match:
            return
        rect = self._locate_word(match)
        if rect is None:
            return
        self._timer.stop()
        self._word_rect = QRectF(rect)
        layer = QPixmap(self.size())
        layer.fill(Qt.GlobalColor.transparent)
        self._capturing = True
        try:
            self.render(layer, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        finally:
            self._capturing = False
        self._layer = layer
        self._animation.start()

    def _turn(self, angle):
        self._angle = float(angle)
        self.update()

    def _land(self):
        self._layer = None
        self._angle = 0.0
        self.update()
        self._arm()

    def resizeEvent(self, event):
        if self._layer is not None:
            self._cancel()
            self._arm()
        super().resizeEvent(event)

    def paintEvent(self, event):
        if self._capturing or self._layer is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        word = self._word_rect.toRect()
        painter.setClipRegion(QRegion(self.rect()).subtracted(QRegion(word)))
        painter.drawPixmap(0, 0, self._layer)
        painter.setClipping(False)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        center = self._word_rect.center()
        painter.translate(center)
        painter.rotate(self._angle)
        painter.translate(-center)
        painter.drawPixmap(word, self._layer, word)
