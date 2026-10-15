"""Persistent, catalogue-backed command details inside the floating reference."""
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QPalette
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from samsara.ui import theme
from samsara.ui.settings import sidebar

# Owner-editable stand-in; descriptions themselves always come from the catalogue.
NO_DESCRIPTION = "No description is available for this command."


class _RunButton(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)


def style_search(edit):
    """Use Settings' search field, including its keyboard-only ring and glyph."""
    edit.setObjectName('settingsSearchField')
    edit.setStyleSheet(sidebar.search_qss())
    edit.setFixedHeight(theme.HIT_TARGET_MIN)
    edit.setTextMargins(sidebar.ICON_BOX + theme.SPACE_2 - sidebar.FOCUS_RING - sidebar._LINE_EDIT_MARGIN, 0, 0, 0)
    edit.setProperty('settingsInputTextInset', sidebar.ICON_INSET + sidebar.ICON_BOX + theme.SPACE_2)
    sidebar.SearchGlyph(edit)


class CommandRowDelegate(QStyledItemDelegate):
    """Equal-height rows; full phrasings remain available in the detail card."""
    def sizeHint(self, option, index):
        return QSize(0, theme.HIT_TARGET_MIN)

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        hovered = bool(styled.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.fillRect(styled.rect, theme.qcolor(
            theme._rgba(theme.ACCENT, 0.14) if selected else theme.BG2 if hovered else theme.BG0))
        painter.setFont(styled.font)
        painter.setPen(theme.qcolor(theme.ACCENT) if selected else styled.palette.color(QPalette.ColorRole.Text))
        text = styled.fontMetrics.elidedText(styled.text, Qt.TextElideMode.ElideRight, styled.rect.width() - theme.SPACE_1)
        painter.drawText(styled.rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        if index.row() < index.model().rowCount() - 1:
            painter.setPen(theme.qcolor(theme.BORDER_FAINT))
            painter.drawLine(styled.rect.bottomLeft(), styled.rect.bottomRight())
        painter.restore()


class CommandDetailCard(QFrame):
    def __init__(self, window, parent):
        super().__init__(parent)
        self.window = window
        self.origin = None
        self.phrase = None
        self.setObjectName('commandDetailCard')
        self.setAccessibleName('Command details')
        self.setStyleSheet(
            f'QFrame#commandDetailCard{{background:{theme.BG1};border:1px solid {theme.BORDER};'
            f'border-radius:{theme.CONTROL_RADIUS}px;}}'
            f'QLabel{{background:transparent;border:none;color:{theme.TEXT_PRIMARY};}}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_2, theme.SPACE_2, theme.SPACE_2, theme.SPACE_2)
        layout.setSpacing(theme.SPACE_1)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet(f'QScrollArea,QScrollArea>QWidget>QWidget{{background:{theme.BG1};border:none;}}' + theme.SCROLLBAR_QSS)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.column = QVBoxLayout(self.body)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(theme.SPACE_1)
        self.labels = []
        for _ in range(4):
            label = QLabel()
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            self.column.addWidget(label)
            self.labels.append(label)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        actions = QHBoxLayout()
        actions.setSpacing(theme.SPACE_1)
        self.run_button = _RunButton('Run it')  # owner-editable stand-in
        button_height = theme.HIT_TARGET_MIN - 2  # QSS height excludes the border.
        self.run_button.setStyleSheet(theme.primary_button_qss() +
            f'QPushButton{{min-height:{button_height}px;max-height:{button_height}px;}}')
        self.run_button.setFixedHeight(theme.HIT_TARGET_MIN)
        self.run_button.clicked.connect(self.run)
        actions.addWidget(self.run_button)
        actions.addStretch()
        self.close_button = QPushButton('✕')
        self.close_button.setAccessibleName('Close command details')
        self.close_button.setToolTip('Close command details')
        self.close_button.setStyleSheet(theme.secondary_button_qss() +
            f'QPushButton{{padding:0;min-height:{button_height}px;max-height:{button_height}px;}}')
        self.close_button.setFixedSize(theme.HIT_TARGET_MIN, theme.HIT_TARGET_MIN)
        self.close_button.clicked.connect(lambda: self.dismiss(True))
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.hide()
        self._filter_installed = False
        window._list.verticalScrollBar().valueChanged.connect(lambda _: self.dismiss())

    def open(self, command, origin, anchor):
        self.origin = origin
        self.phrase = command['phrase']
        aliases = [str(a) for a in command.get('aliases', ()) if a != self.phrase]
        arguments = command.get('args', ())
        fields = [self.phrase, 'Also: ' + ', '.join(aliases) if aliases else '',
                  command.get('description', '').strip() or NO_DESCRIPTION,
                  'Arguments: ' + ', '.join(
                      f"{arg['name']} ({arg['type']}, {'required' if arg.get('required') else 'optional'})"
                      for arg in arguments) if arguments else 'No argument required.']
        for label, text in zip(self.labels, fields):
            label.setText(text)
            label.setVisible(bool(text))
        self.labels[0].setStyleSheet(f'font-size:{theme.TYPE_EMPHASIS}px;font-weight:600;')
        self.setAccessibleName('Command details: ' + self.phrase)
        description = '\n'.join(text for text in fields if text)
        self.setAccessibleDescription(description)
        self.run_button.setAccessibleName('Run it: ' + self.phrase)
        self.run_button.setAccessibleDescription(description)
        self.place(anchor)
        self.show()
        self.raise_()
        if not self._filter_installed:
            QApplication.instance().installEventFilter(self)
            self._filter_installed = True
        self.scroll.verticalScrollBar().setValue(0)
        origin.setFocus(Qt.FocusReason.OtherFocusReason)
        QWidget.setTabOrder(origin, self.run_button)
        QWidget.setTabOrder(self.run_button, self.close_button)
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self, description))

    def place(self, anchor):
        area = self.parentWidget().rect().adjusted(theme.SPACE_2, theme.SPACE_1, -theme.SPACE_2, -theme.SPACE_1)
        width = area.width()
        text_width = max(1, width - 2 * theme.SPACE_2 - 2)
        body_height = sum(label.heightForWidth(text_width) for label in self.labels if not label.isHidden())
        body_height += theme.SPACE_1 * (sum(not label.isHidden() for label in self.labels) - 1)
        height = min(area.height(), body_height + 2 * theme.SPACE_2 + theme.SPACE_1 + theme.HIT_TARGET_MIN + 4)
        y = anchor.bottom() + theme.SPACE_1
        if y + height > area.bottom() + 1:
            y = anchor.top() - theme.SPACE_1 - height
        y = max(area.top(), min(y, area.bottom() + 1 - height))
        self.setGeometry(area.left(), y, width, height)

    def dismiss(self, restore_focus=False):
        if self._filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._filter_installed = False
        self.hide()
        self.phrase = None
        if restore_focus and self.origin is not None and isValid(self.origin):
            self.origin.setFocus(Qt.FocusReason.OtherFocusReason)
        self.origin = None

    def run(self):
        phrase = self.phrase
        self.dismiss(True)
        if phrase:
            self.window._execute(phrase)

    def eventFilter(self, watched, event):
        if not self.isVisible():
            return False
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.dismiss(True)
                return True
            if watched is self.origin and event.key() == Qt.Key.Key_Tab:
                self.run_button.setFocus(Qt.FocusReason.TabFocusReason)
                return True
            if watched is self.origin and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                self.dismiss()
        elif kind == QEvent.Type.MouseButtonPress:
            if isinstance(watched, QWidget) and watched is not self and not self.isAncestorOf(watched):
                self.dismiss()
        elif watched is self.window and kind in (QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.WindowDeactivate):
            self.dismiss()
        return False
