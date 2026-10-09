"""Shared stand-in copy and accessible setup choice; thumbnails are sketches."""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QRadioButton,
    QButtonGroup, QFrame, QScrollArea,
)
from samsara import listening_display as choice
from samsara.ui import theme

STEP_LABEL_GAP = theme.SPACE_3 // 2  # 12px, matching tutorial 378.


class _Preview(QWidget):
    """Small illustrative picture, never a second running display."""
    def __init__(self, value, parent=None):
        super().__init__(parent)
        self.value = value
        self.setFixedSize(180, 86)
        self.setAccessibleName(choice.NAMES[value])
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
        painter.setBrush(theme.qcolor(theme.BG2))
        rect = QRectF(20, 26, 140, 34) if self.value == "compact" else QRectF(4, 4, 172, 78)
        painter.drawRoundedRect(rect, 10, 10)
        from samsara.ui.tray_qt import render_mark
        mark = render_mark("idle", "off", size=24)
        painter.drawImage(int(rect.x() + 9), int(rect.y() + 7), mark)
        painter.setPen(QPen(theme.qcolor(theme.TEXT_SECONDARY), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(int(rect.x() + 44), int(rect.y() + 17), int(rect.right() - 12), int(rect.y() + 17))
        if self.value == "full":
            for y, width in ((43, 136), (54, 117), (65, 94)):
                painter.drawLine(18, y, width, y)
        painter.end()


class ListeningDisplayPage(QScrollArea):
    """One radio group with both options pictured, keyboard and reader named."""
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAccessibleName(choice.QUESTION)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 16, 28, 16)
        self.group = QButtonGroup(self)
        selected = choice.display_choice(config, setup=True).value
        for index, value in enumerate(choice.VALUES):
            card = QFrame()
            theme.style_card(card)
            row = QHBoxLayout(card)
            row.setContentsMargins(14, 12, 14, 12)
            button = QRadioButton(choice.NAMES[value])
            button.setStyleSheet('QRadioButton { background: transparent; }')
            button.setMinimumHeight(44)
            button.setAccessibleName(choice.NAMES[value])
            button.setAccessibleDescription(choice.DESCRIPTIONS[value])
            button.setProperty("display_value", value)
            self.group.addButton(button, index)
            button.setChecked(value == selected)
            text = QVBoxLayout()
            text.addWidget(button)
            description = QLabel(choice.DESCRIPTIONS[value])
            description.setWordWrap(True)
            description.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
            text.addWidget(description)
            row.addLayout(text, 1)
            row.addWidget(_Preview(value))
            layout.addWidget(card)
        if self.group.checkedButton() is None:
            self.group.button(choice.VALUES.index(choice.PRESELECTED)).setChecked(True)
        layout.addStretch()
        self.setWidget(body)

    def save_to(self, config):
        config.update(choice.choice_updates(config, self.group.checkedButton().property("display_value"), chosen=True))


def setup_page(owner):
    owner._listening_display_page = ListeningDisplayPage(owner._config, owner._stack)
    return owner._listening_display_page


def settings_control(config):
    from samsara.ui.settings.presentation import CleanComboBox
    combo = CleanComboBox()
    for value in choice.VALUES:
        combo.addItem(choice.NAMES[value], value)
    combo.setCurrentIndex(choice.VALUES.index(choice.display_choice(config).value))
    combo.setAccessibleName(choice.LABEL)
    combo.setAccessibleDescription(choice.QUESTION)
    combo.setProperty("display_chosen", False)
    combo.currentIndexChanged.connect(lambda _: combo.setProperty("display_chosen", True))
    combo.activated.connect(lambda _: combo.setProperty("display_chosen", True))
    return combo
