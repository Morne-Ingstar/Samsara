"""A keyboard-navigable scroll area for rows with real checkboxes."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget, QVBoxLayout

from samsara.ui import theme


class CheckBoxList(QScrollArea):
    def __init__(self, name, height, width):
        super().__init__()
        self.setAccessibleName(name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setFixedHeight(height)
        self.setMaximumWidth(width)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalScrollBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.content = QWidget()
        self.rows = QVBoxLayout(self.content)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        self.rows.addStretch()
        self.setWidget(self.content)
        self._entries = []
        self._current = -1

    def add_row(self, row, control, name_label=None):
        marker = QFrame()
        marker.setFixedWidth(2)
        marker.setStyleSheet("background: transparent; border: none;")
        marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.layout().insertWidget(0, marker)
        self.rows.insertWidget(self.rows.count() - 1, row)
        index = len(self._entries)
        self._entries.append((row, control, name_label, marker))
        if control is not None:
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            control.clicked.connect(lambda: self._focus_row(index))

    def _focus_row(self, index):
        self._set_current(index)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def show_control(self, control):
        for index, (_, checkbox, _, _) in enumerate(self._entries):
            if checkbox is control:
                self._set_current(index)
                break

    def _set_current(self, index):
        if self._current >= 0:
            self._entries[self._current][3].setStyleSheet("background: transparent; border: none;")
        self._current = index
        row, _, _, marker = self._entries[index]
        marker.setStyleSheet(f"background: {theme.ACCENT}; border: none;")
        self.rows.activate()
        self.ensureWidgetVisible(row, 0, 0)
        if self.hasFocus():
            self._announce_current()

    def _announce_current(self):
        _, control, name_label, _ = self._entries[self._current]
        # Expose the active row to assistive technology while the list keeps
        # actual keyboard focus and Tab continues to leave the list.
        QAccessible.updateAccessibility(QAccessibleEvent(
            control if control is not None else name_label, QAccessible.Event.Focus))

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._current < 0 and self._entries:
            self._set_current(0)
        elif self._current >= 0:
            self._announce_current()

    def keyPressEvent(self, event):
        if self._current < 0:
            return super().keyPressEvent(event)
        key = event.key()
        last = len(self._entries) - 1
        if key == Qt.Key.Key_Space:
            control = self._entries[self._current][1]
            if control is not None and control.isEnabled() and not event.isAutoRepeat():
                control.click()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = -1 if key == Qt.Key.Key_Up else 1
            self._set_current(max(0, min(last, self._current + step)))
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self._set_current(0 if key == Qt.Key.Key_Home else last)
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            bar = self.verticalScrollBar()
            direction = -1 if key == Qt.Key.Key_PageUp else 1
            y = self._entries[self._current][0].y() + direction * bar.pageStep()
            index = min(range(len(self._entries)), key=lambda i: abs(self._entries[i][0].y() - y))
            self._set_current(index)
        else:
            return super().keyPressEvent(event)
        event.accept()
