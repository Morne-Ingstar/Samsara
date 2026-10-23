"""Shared Settings presentation. Copy is provisional per owner-approved 285."""
from PySide6.QtCore import Qt, QPoint, QRectF
from PySide6.QtGui import QPainter, QPainterPath, QPen, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QComboBox, QListView,
    QHBoxLayout, QVBoxLayout, QGridLayout,
    QScrollArea, QStylePainter, QStyleOptionComboBox, QStyle,
)
from samsara.ui import theme

def style():
    return f"""
    QWidget {{ color: {theme.TEXT_PRIMARY}; font-family: 'Segoe UI';
               font-size: {theme.TYPE_BODY}px; background: {theme.BG0}; }}
    QFrame#sidebar, QFrame#footer {{ background: {theme.BG1}; }}
    QFrame#footer {{ border-top: 1px solid {theme.BORDER_FAINT}; }}
    QFrame#footer QWidget {{ background: transparent; }}
    QLabel {{ background: transparent; }}
    QSlider {{ background: transparent; }}
    QLabel[role='description'] {{ color: {theme.TEXT_SECONDARY}; }}
    QLabel[role='title'] {{ font-size: {theme.TYPE_TITLE}px; font-weight: 600; }}
    QLabel[role='label'] {{ font-weight: 600; }}
    QPushButton, QComboBox, QLineEdit, QSpinBox {{ background: {theme.BG2};
        border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 0 12px; }}
    QComboBox {{ border-color: {theme.BORDER_FAINT}; border-radius: 8px;
        padding: 0 8px 0 12px; }}
    QComboBox:hover {{ border-color: {theme.BORDER}; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right;
        width: 28px; background: transparent; border: none; }}
    QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
    QComboBox QAbstractItemView {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 4px;
        selection-background-color: {theme.BG2}; selection-color: {theme.TEXT_PRIMARY}; outline: none; }}
    QComboBox QAbstractItemView::item {{ min-height: 44px; padding: 0 12px; border-radius: 5px; }}
    QPushButton:hover {{ border-color: {theme.ACCENT}; }}
    QPushButton:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {theme.ACCENT}; }}
    QPushButton#apply {{ background: {theme.ACCENT};
        color: {theme.TEXT_ON_ACCENT}; border: none; }}
    QPushButton[role='nav'] {{ background: transparent; border: 1px solid transparent;
        text-align: left; padding-left: 14px; }}
    QPushButton[role='nav']:hover {{ background: {theme.HOVER_WASH}; }}
    QPushButton[role='nav'][selected='true'] {{ background: {theme.BG2}; font-weight: 600; }}
    QPushButton[role='nav']:focus {{ border: 1px solid {theme.ACCENT}; }}
    QPushButton#helpButton {{ background: transparent; border: 1px solid transparent;
        border-radius: 22px; padding: 0; color: {theme.TEXT_SECONDARY}; }}
    QPushButton#helpButton:hover {{ background: {theme.HOVER_WASH}; color: {theme.TEXT_PRIMARY}; }}
    QPushButton#helpButton:focus {{ border: 1px solid {theme.ACCENT}; }}
    QCheckBox {{ spacing: 12px; background: transparent; }}
    QCheckBox::indicator {{ width: 24px; height: 24px; border: 1px solid {theme.BORDER};
        border-radius: 4px; background: {theme.BG2}; }}
    QCheckBox::indicator:checked {{ background: {theme.ACCENT}; border-color: {theme.ACCENT}; }}
    QScrollArea {{ border: none; }}
    QMenu {{ background: {theme.BG1}; border: 1px solid {theme.BORDER}; padding: 8px; }}
    QMenu::item {{ padding: 13px 24px; }}
    QMenu::item:selected {{ background: {theme.BG2}; }}
    QFrame#helpPanel {{ background: {theme.BG2}; border: 1px solid {theme.ACCENT}; border-radius: 8px; }}
    """



class CleanComboBox(QComboBox):
    """Keep Qt selection/keyboard behavior; replace only the native arrow chrome."""
    def __init__(self):
        super().__init__()
        self.setView(QListView())
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        if not self.isEditable():
            field = self.style().subControlRect(
                QStyle.ComplexControl.CC_ComboBox, option,
                QStyle.SubControl.SC_ComboBoxEditField, self)
            option.currentText = self.fontMetrics().elidedText(
                option.currentText, Qt.TextElideMode.ElideRight, field.width())
            painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(theme.qcolor(theme.TEXT_SECONDARY), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        x, y = self.width() - 18, self.height() / 2
        chevron = QPainterPath()
        chevron.moveTo(x - 4, y - 2)
        chevron.lineTo(x, y + 2)
        chevron.lineTo(x + 4, y - 2)
        painter.drawPath(chevron)


class HelpButton(QPushButton):
    """Explicit, keyboard-accessible help disclosure; Escape closes it."""
    def __init__(self, title, help_data, window):
        super().__init__("?")
        self.setObjectName("helpButton")
        self.setFixedSize(44, 44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Help: {title}")
        self.setAccessibleDescription(help_data["why"])
        self.title, self.help_data, self.host = title, help_data, window
        self.clicked.connect(self.reveal)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.TEXT_SECONDARY), 1.2))
        painter.drawEllipse(QRectF(11, 11, 22, 22))

    def reveal(self):
        self.host.show_help()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.host.hide_help()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.reveal()
        else:
            super().keyPressEvent(event)



class Disclosure(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.toggle = QPushButton(title + "  ▸")
        self.toggle.setMinimumHeight(44)
        self.toggle.setCheckable(True)
        self.toggle.setAccessibleName(title)
        self.column.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.content = QWidget()
        self.rows = QVBoxLayout(self.content)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        self.no_match = QLabel("No advanced settings match")
        self.no_match.setProperty("role", "description")
        self.no_match.setMinimumHeight(44)
        self.no_match.hide()
        self.filter_query = ""
        self.rows.addWidget(self.no_match)
        self.column.addWidget(self.content)
        self.content.hide()
        self.toggle.toggled.connect(self.content.setVisible)
        self.toggle.toggled.connect(lambda on: self.toggle.setText(title + ("  ▾" if on else "  ▸")))
        self.toggle.toggled.connect(lambda _on: self.refresh_filter_state())

    def reveal(self):
        self.toggle.setChecked(True)

    def refresh_filter_state(self, query=None):
        if query is not None:
            self.filter_query = query
        visible_rows = any(
            item.widget() is not None and item.widget() is not self.no_match
            and not item.widget().property('settingsRowDivider')
            and item.widget().isVisible()
            for index in range(self.rows.count())
            if (item := self.rows.itemAt(index)) is not None
        )
        self.no_match.setVisible(bool(self.filter_query) and self.toggle.isChecked() and not visible_rows)


class HelpDocsLink(QLabel):
    def __init__(self, url, close_help, parent=None):
        super().__init__(parent)
        self.close_help = close_help
        self.setText(f'<a href="{url}" style="color: {theme.ACCENT}; text-decoration: none;">Read more in the docs</a>')
        self.setStyleSheet(
            "QLabel { background: transparent; border: 1px solid transparent;"
            " border-radius: 3px; text-decoration: none; }"
            "QLabel:hover { text-decoration: none; }"
            f"QLabel:focus {{ border: 1px solid {theme.ACCENT}; text-decoration: none; }}"
        )
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(44)
        self.setAccessibleName("Read more in the docs")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_help()
        else:
            super().keyPressEvent(event)


class SettingRow(QFrame):
    """One semantic row, including grouped controls and accessible help."""
    def __init__(self, title, description, controls, help_data=None, parent=None):
        super().__init__(parent)
        self.setProperty("role", "row")
        self.title = title
        self.description = description
        self.help_data = help_data
        self.full_help = help_data["why"] if help_data else ""
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 10, 0, 10)
        self.grid.setHorizontalSpacing(12)
        self.text = QWidget()
        text_layout = QVBoxLayout(self.text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        self.heading = QLabel(title or description)
        self.heading.setProperty("role", "label")
        self.heading.setWordWrap(True)
        self.detail = QLabel(description if title else "")
        self.detail.setProperty("role", "description")
        self.detail.setWordWrap(True)
        text_layout.addWidget(self.heading)
        text_layout.addWidget(self.detail)
        self.help = HelpButton(title, help_data, self) if help_data else None
        self.value = QWidget()
        self.values = QHBoxLayout(self.value)
        self.values.setContentsMargins(0, 0, 0, 0)
        self.values.setSpacing(8)
        for control in controls:
            self.values.addWidget(control, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            if not control.accessibleName():
                control.setAccessibleName(title)
            control.setAccessibleDescription(self.full_help or description)
            control.show()
        self.values.addStretch()
        self.grid.addWidget(self.text, 0, 0)
        if self.help is not None:
            self.grid.addWidget(self.help, 0, 1)
        self.grid.addWidget(self.value, 0, 2)
        self.grid.setColumnStretch(0, 1)
        self.help_panel = None
        self._narrow = False

    def show_help(self):
        """Overlay a compact panel without changing the page's row layout."""
        window = self.window()
        previous = getattr(window, '_active_setting_help', None)
        if previous is not None and previous is not self:
            previous.hide_help()
        if self.help_panel is None:
            self.help_panel = QFrame(window)
            self.help_panel.setObjectName("helpPanel")
            escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.help_panel)
            escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            escape.activated.connect(self.hide_help)
            help_layout = QVBoxLayout(self.help_panel)
            help_layout.setContentsMargins(14, 12, 14, 12)
            help_layout.setSpacing(8)
            self.help_copy = QLabel(self.help_data["why"])
            self.help_copy.setWordWrap(True)
            help_layout.addWidget(self.help_copy)
            self.help_link = HelpDocsLink(self.help_data["docs"], self.hide_help)
            help_layout.addWidget(self.help_link)
            close = QPushButton("Close help")
            close.setMinimumHeight(44)
            close.setAccessibleName(f"Close help: {self.title}")
            close.clicked.connect(self.hide_help)
            help_layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        panel_width = min(480, max(260, self.text.width()))
        self.help_copy.setFixedWidth(panel_width - 28)
        self.help_copy.setFixedHeight(self.help_copy.sizeHint().height())
        self.help_panel.setFixedWidth(panel_width)
        self.help_panel.adjustSize()
        bounds = window.rect().adjusted(12, 12, -12, -64)
        anchor = self.text.mapTo(window, QPoint(0, self.text.height() + 4))
        x = max(bounds.left(), min(anchor.x(), bounds.right() - self.help_panel.width()))
        y = anchor.y()
        if y + self.help_panel.height() > bounds.bottom():
            y = max(bounds.top(), self.help.mapTo(window, QPoint(0, -self.help_panel.height() - 4)).y())
        self.help_panel.move(x, y)
        window._active_setting_help = self
        self.help_panel.show()
        self.help_panel.raise_()

    def hide_help(self):
        if self.help_panel is not None:
            self.help_panel.hide()
            if self.help is not None:
                self.help.setFocus()
        window = self.window()
        if getattr(window, '_active_setting_help', None) is self:
            window._active_setting_help = None

    def resizeEvent(self, event):
        narrow = event.size().width() < 730
        if narrow != self._narrow:
            self._narrow = narrow
            self.grid.removeWidget(self.value)
            if narrow:
                self.grid.addWidget(self.value, 1, 0, 1, 3)
            else:
                self.grid.addWidget(self.value, 0, 2)
        super().resizeEvent(event)


class Page(QScrollArea):
    def __init__(self, title, subtitle):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.title = title
        self.container = QWidget()
        self.container.setMaximumWidth(1160)
        self.column = QVBoxLayout(self.container)
        self.column.setContentsMargins(28, 24, 28, 24)
        self.column.setSpacing(8)
        heading = QLabel(title)
        heading.setProperty("role", "title")
        self.column.addWidget(heading)
        note = QLabel(subtitle)
        note.setWordWrap(True)
        note.setProperty("role", "description")
        self.column.addWidget(note)
        divider = QWidget(self.container)
        divider.setObjectName("settingsPageDivider")
        divider_layout = QVBoxLayout(divider)
        divider_layout.setContentsMargins(0, theme.SPACE_1 // 2, 0, theme.SPACE_1 // 2)
        divider_layout.setSpacing(0)
        line = QFrame(divider)
        line.setObjectName("settingsPageHairline")
        line.setFixedHeight(1)
        divider_layout.addWidget(line)
        self.column.addWidget(divider)
        self.base = QVBoxLayout()
        self.base.setSpacing(0)
        self.column.addLayout(self.base)
        self.advanced = Disclosure("Advanced")
        self.column.addWidget(self.advanced)
        self.column.addStretch()
        self.setWidget(self.container)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.base_rows = []

    def add_row(self, row, advanced=False):
        if advanced:
            self.advanced.rows.addWidget(row)
        else:
            self.base.addWidget(row)
            self.base_rows.append(row)
