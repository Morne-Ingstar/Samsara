"""Settings control adapters: native state and callbacks remain canonical."""
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QSignalBlocker
from PySide6.QtGui import QPainter, QPainterPath, QPen, QValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
    QRadioButton, QButtonGroup, QSizePolicy,
)
from samsara.ui import theme


def _tick(painter, x, y, unit):
    path = QPainterPath()
    path.moveTo(x - unit, y)
    path.lineTo(x - unit / 4, y + unit * .75)
    path.lineTo(x + unit * 1.25, y - unit * .75)
    painter.drawPath(path)


class SwitchSkin(QObject):
    """Paint the existing native checkbox, retaining every connected handler.

    A paint/event adapter avoids replacing a consent-controlled checkbox or
    copying its state. Qt still exposes the original CheckBox accessibility
    role, Space handling, checked state, name and disabled state.
    """
    def __init__(self, checkbox):
        super().__init__(checkbox)
        self.checkbox = checkbox
        self.pressed = False
        checkbox.setFixedSize(theme.SPACE_7, theme.HIT_TARGET_MIN)
        checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        checkbox.installEventFilter(self)
        checkbox.toggled.connect(checkbox.update)

    def eventFilter(self, widget, event):
        kind = event.type()
        if kind == QEvent.Type.Paint:
            self.paint(widget)
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            if widget.isEnabled():
                self.pressed = True
                widget.setFocus(Qt.FocusReason.MouseFocusReason)
                widget.setDown(True)
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            clicked = self.pressed and widget.rect().contains(event.position().toPoint())
            self.pressed = False
            widget.setDown(False)
            if clicked and widget.isEnabled():
                widget.click()
            return True
        if kind in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.FocusIn,
                    QEvent.Type.FocusOut, QEvent.Type.EnabledChange):
            widget.update()
        return False

    @staticmethod
    def paint(widget):
        painter = QPainter(widget)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        on, enabled = widget.isChecked(), widget.isEnabled()
        fill = (theme.ACCENT_HOVER if widget.underMouse() else theme.ACCENT) if on else (
            theme.HOVER_WASH if widget.underMouse() else theme.BG2)
        if not enabled:
            fill = theme.ACCENT_DISABLED if on else theme.BG1
        track_height = theme.SPACE_3 + theme.SPACE_1 / 2
        track = QRectF((widget.width() - theme.SPACE_6) / 2,
                       (widget.height() - track_height) / 2, theme.SPACE_6, track_height)
        painter.setPen(QPen(theme.qcolor(theme.BORDER if enabled else theme.BORDER_FAINT), 1))
        painter.setBrush(theme.qcolor(fill))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob = QRectF(track.right() - theme.SPACE_3 - 2 if on else track.left() + 2,
                      track.top() + 2, theme.SPACE_3, theme.SPACE_3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor((theme.TEXT_ON_ACCENT if on else theme.TEXT_PRIMARY)
                                     if enabled else theme.TEXT_DISABLED))
        painter.drawEllipse(knob)
        painter.setPen(QPen(theme.qcolor(fill), 2))
        center = knob.center()
        unit = theme.SPACE_1 / 2
        if on:
            _tick(painter, center.x(), center.y(), unit)
        else:
            painter.drawLine(QPointF(center.x() - unit, center.y()),
                             QPointF(center.x() + unit, center.y()))
        if widget.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
            clearance = theme.SPACE_1 / 2
            painter.drawRoundedRect(track.adjusted(-clearance, -clearance, clearance, clearance),
                                    track.height() / 2 + clearance, track.height() / 2 + clearance)


def switch(checkbox):
    if not hasattr(checkbox, '_calm_switch'):
        checkbox._calm_switch = SwitchSkin(checkbox)
    return checkbox


class PercentSpinBox(QDoubleSpinBox):
    """Fraction-valued native spin box with percent text and editing.

    Range, precision, step, valueChanged and value() stay in stored units.
    The source field remains hidden and synchronized for factory closures.
    """
    def textFromValue(self, value):
        return self.locale().toString(value * 100, 'f', max(0, self.decimals() - 2))

    def valueFromText(self, text):
        value, valid = self.locale().toDouble(text.removesuffix('%').strip())
        return value / 100 if valid else self.value()

    def validate(self, text, position):
        plain = text.removesuffix('%').strip()
        if not plain or plain in ('-', self.locale().decimalPoint()):
            return QValidator.State.Intermediate, text, position
        value, valid = self.locale().toDouble(plain)
        state = (QValidator.State.Acceptable if valid and self.minimum() <= value / 100 <= self.maximum()
                 else QValidator.State.Invalid)
        return state, text, position

    @classmethod
    def adapt(cls, source, owner):
        editor = cls()
        editor.setDecimals(source.decimals())
        editor.setRange(source.minimum(), source.maximum())
        editor.setSingleStep(source.singleStep())
        editor.setSuffix('%')
        editor.setValue(source.value())
        editor.setEnabled(source.isEnabled())
        editor.setAccessibleName(source.accessibleName())
        editor.setAccessibleDescription(source.accessibleDescription())
        editor.setKeyboardTracking(source.keyboardTracking())
        editor.setObjectName(source.objectName())
        source.setParent(owner._source_store)
        source.hide()
        source.valueChanged.connect(editor.setValue)
        editor.valueChanged.connect(source.setValue)
        editor._canonical_control = source
        return editor


class Segment(QRadioButton):
    def __init__(self, text, cluster):
        super().__init__(text, cluster)
        self.cluster = cluster
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self.setAccessibleName(text)
        self.setFixedSize(max(theme.HIT_TARGET_MIN,
                             self.fontMetrics().horizontalAdvance(text) + theme.SPACE_4),
                          theme.HIT_TARGET_MIN)

    def hitButton(self, point):
        return self.rect().contains(point)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inset = theme.SPACE_1 / 2
        box = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        fill = theme.BG2 if self.isChecked() else (theme.HOVER_WASH if self.underMouse() else theme.BG1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(fill))
        painter.drawRoundedRect(box, theme.CONTROL_RADIUS, theme.CONTROL_RADIUS)
        ink = theme.TEXT_PRIMARY if self.isEnabled() else theme.TEXT_DISABLED
        painter.setPen(theme.qcolor(ink))
        painter.setFont(self.font())
        painter.drawText(self.rect().adjusted(0, 0, -theme.SPACE_2, 0),
                         Qt.AlignmentFlag.AlignCenter, self.text())
        if self.isChecked():
            painter.setPen(QPen(theme.qcolor(ink), 2))
            _tick(painter, self.width() - theme.SPACE_1, self.height() / 2, theme.SPACE_1 / 2)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
            painter.drawRoundedRect(box, theme.CONTROL_RADIUS, theme.CONTROL_RADIUS)

    def keyPressEvent(self, event):
        forward = (Qt.Key.Key_Right, Qt.Key.Key_Down)
        backward = (Qt.Key.Key_Left, Qt.Key.Key_Up)
        if event.key() in forward + backward:
            buttons = self.cluster.buttons
            index = buttons.index(self)
            for distance in range(1, len(buttons) + 1):
                candidate = buttons[(index + distance * (1 if event.key() in forward else -1)) % len(buttons)]
                if candidate.isEnabled():
                    candidate.click()
                    # Consent may reject the proposed route; focus the actual choice.
                    selected = self.cluster.group.checkedButton()
                    if selected: selected.setFocus()
                    break
            event.accept()
        else:
            super().keyPressEvent(event)


class SegmentedChoice(QFrame):
    """One visible native radio group driving the existing canonical combo."""
    def __init__(self, combo):
        super().__init__()
        self.setObjectName('settingsSegments')
        self.combo = combo
        self._canonical_control = combo
        self.buttons = []
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.values = QHBoxLayout(self)
        self.values.setContentsMargins(1, 1, 1, 1)
        self.values.setSpacing(0)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        combo.setParent(self)
        combo.hide()
        combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for index in range(combo.count()):
            button = Segment(combo.itemText(index), self)
            self.group.addButton(button, index)
            self.values.addWidget(button)
            self.buttons.append(button)
        self.group.idClicked.connect(self.choose)
        combo.currentIndexChanged.connect(self.refresh)
        combo.installEventFilter(self)
        self.setFixedSize(sum(button.width() for button in self.buttons) + 2, theme.HIT_TARGET_MIN + 2)
        self.refresh()

    def choose(self, index):
        self.combo.setCurrentIndex(index)
        self.refresh()

    def refresh(self, *_args):
        for index, button in enumerate(self.buttons):
            with QSignalBlocker(button):
                button.setChecked(index == self.combo.currentIndex())
            item = self.combo.model().item(index) if hasattr(self.combo.model(), 'item') else None
            button.setEnabled(self.combo.isEnabled() and (item is None or item.isEnabled()))
            button.setAccessibleDescription(self.accessibleName() + '. ' + self.accessibleDescription())
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus if button.isChecked() else Qt.FocusPolicy.NoFocus)
        self.setFocusProxy(self.group.checkedButton())

    def eventFilter(self, widget, event):
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.DynamicPropertyChange):
            self.refresh()
        return False


def short_choice(widget, key):
    # Hardware, language, theme and size lists can grow or describe external state.
    stable = {'tts_feedback_level', 'tts_engine', 'adv_ducking_recording_mode',
              'speech_pace', 'cleanup_mode', 'adv_device', 'adv_perf_mode',
              'adv_threshold_mode', 'providerRoute', 'ava_memory_mode', 'listening_display',
              'continuous_commit_trigger', 'ava_personality', 'mode', 'cmd_mode'}
    if (isinstance(widget, QComboBox) and key in stable and 2 <= widget.count() <= 3
            and sum(len(widget.itemText(index)) for index in range(widget.count())) <= 34):
        return SegmentedChoice(widget)
    return widget
