"""Shared Settings presentation. Copy is provisional per owner-approved 285."""
from PySide6.QtCore import Qt, QPoint, QRectF, QEvent
from PySide6.QtGui import QPainter, QPainterPath, QPen, QKeySequence, QShortcut, QColor
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QComboBox, QListView,
    QHBoxLayout, QVBoxLayout, QGridLayout,
    QScrollArea, QStylePainter, QStyleOptionComboBox, QStyle,
    QTableWidget, QStyledItemDelegate, QHeaderView, QSizePolicy,
)
from samsara.ui import theme

TOOLS_TABLE_MAX_WIDTH = 640


class DataCellDelegate(QStyledItemDelegate):
    """Opaque whole-row states, elided text and a persistent selection marker."""
    def paint(self, painter, option, index):
        table = self.parent()
        selected = table.selectionModel().isRowSelected(index.row(), index.parent())
        fill = (theme.DATA_ROW_SELECTED if selected else
                theme.DATA_ROW_HOVER if index.row() == table._hover_row else
                theme.DATA_ROW_ALT if index.row() % 2 else theme.DATA_SURFACE)
        painter.save()
        painter.setClipRect(option.rect)
        painter.fillRect(option.rect, QColor(fill))
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        secondary = index.column() in table._secondary_columns and not selected
        painter.setPen(QColor(theme.DATA_TEXT_SECONDARY if secondary else theme.TEXT_PRIMARY))
        painter.setFont(option.font)
        rect = option.rect.adjusted(12, 0, -12, 0)
        alignment = index.data(Qt.ItemDataRole.TextAlignmentRole)
        alignment = Qt.AlignmentFlag(alignment) if alignment else Qt.AlignmentFlag.AlignLeft
        text = option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width()))
        painter.drawText(rect, alignment | Qt.AlignmentFlag.AlignVCenter, text)
        if selected and index.column() == 0:
            painter.fillRect(option.rect.x(), option.rect.y() + 8, 3,
                             option.rect.height() - 16, QColor(theme.ACCENT))
        if table.hasFocus() and table.currentRow() == index.row():
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(theme.ACCENT), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            row = QRectF(1, option.rect.y() + 1, table.viewport().width() - 2,
                         option.rect.height() - 2)
            painter.drawRoundedRect(row, 5, 5)
        painter.restore()


class DataTable(QTableWidget):
    """Settings' filled record panel; ordinary tables elsewhere stay unchanged."""
    def __init__(self, *args):
        super().__init__(*args)
        self._hover_row = -1
        self._data_widths = ()
        self._flex_column = 0
        self._secondary_columns = ()
        self.setProperty('settingsDataPanel', True)
        self.setMaximumWidth(TOOLS_TABLE_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setMouseTracking(True)
        self.viewport().setObjectName('settingsDataViewport')
        self.viewport().installEventFilter(self)
        self.setItemDelegate(DataCellDelegate(self))
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.horizontalHeader().setStretchLastSection(False)
        self.horizontalHeader().setMinimumSectionSize(24)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.horizontalScrollBar().rangeChanged.connect(self._scroll_range_changed)
        self.retheme()

    def columns(self, widths, flexible=0, secondary=()):
        self._data_widths, self._flex_column = widths, flexible
        self._secondary_columns = secondary
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._allocate_columns()

    def _allocate_columns(self):
        if not self._data_widths:
            return
        widths = list(self._data_widths)
        # Narrow windows scroll horizontally; never crush a data/state column.
        available = self.viewport().width()
        if available >= sum(widths) - self.verticalScrollBar().sizeHint().width():
            widths[self._flex_column] += available - sum(widths)
        for column, width in enumerate(widths):
            if self.columnWidth(column) != width:
                self.setColumnWidth(column, width)

    def retheme(self):
        self.setStyleSheet(f"""
            QTableWidget {{ background: {theme.DATA_SURFACE}; color: {theme.TEXT_PRIMARY};
                alternate-background-color: {theme.DATA_ROW_ALT}; border: none;
                border-radius: 10px; padding: 8px 16px; gridline-color: transparent; }}
            QTableWidget QWidget {{ background: transparent; }}
            QTableWidget QHeaderView {{ background: {theme.DATA_SURFACE}; }}
            QHeaderView::section {{ background: {theme.DATA_SURFACE}; color: {theme.DATA_TEXT_SECONDARY};
                border: none; padding: 0 12px; font-size: {theme.TYPE_BODY}px; font-weight: 600; }}
            QTableCornerButton::section {{ background: {theme.DATA_SURFACE}; border: none; }}
            QLabel#toolsTableEmptyState {{ color: {theme.DATA_TEXT_SECONDARY}; background: transparent; }}
            QScrollBar {{ background: {theme.DATA_SURFACE}; border: none; margin: 0; }}
            QScrollBar:vertical {{ width: 10px; }}
            QScrollBar:horizontal {{ height: 10px; }}
            QScrollBar::handle {{ background: {theme.DATA_TEXT_SECONDARY}; border-radius: 4px; }}
            QScrollBar::handle:vertical {{ min-height: 24px; }}
            QScrollBar::handle:horizontal {{ min-width: 24px; }}
            QScrollBar::handle:hover {{ background: {theme.ACCENT}; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        """)
        self.viewport().setStyleSheet(f'QWidget#settingsDataViewport {{ background: {theme.DATA_SURFACE}; border: none; }}')
        self.horizontalHeader().setFixedHeight(max(40, self.fontMetrics().height() + 16))
        self.verticalHeader().setDefaultSectionSize(max(48, self.fontMetrics().height() + 24))
        self._allocate_columns()
        if hasattr(self, '_empty_state_label'):
            table_empty_state(self, self._empty_state_label.text())
        self.viewport().update()

    def _scroll_range_changed(self, *_):
        if hasattr(self, '_empty_state_label'):
            table_empty_state(self, self._empty_state_label.text())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_data_widths'):
            self._allocate_columns()
        if hasattr(self, '_empty_state_label'):
            self._empty_state_label.setGeometry(self.viewport().rect())

    def mouseMoveEvent(self, event):
        self._hover_row = self.rowAt(int(event.position().y()))
        self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def setItem(self, row, column, item):
        # The model retains full values for accessibility; hover exposes elision.
        item.setToolTip(item.text())
        super().setItem(row, column, item)

    def setCellWidget(self, row, column, widget):
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)
        if isinstance(widget, QLabel):
            widget.setContentsMargins(12, 0, 12, 0)
            widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            widget.setProperty('dataSecondary', True)
        super().setCellWidget(row, column, widget)

    def eventFilter(self, widget, event):
        if widget is self.viewport():
            if event.type() == QEvent.Type.Leave:
                self._hover_row = -1
                self.viewport().update()
        elif event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove):
            self._hover_row = self.rowAt(widget.mapTo(self.viewport(), QPoint()).y())
            self.viewport().update()
        return super().eventFilter(widget, event)


def table_empty_state(table, text):
    """Accessible viewport overlay: 96-DIP empty body, never a fake record."""
    label = getattr(table, '_empty_state_label', None)
    if label is None:
        label = QLabel(table.viewport())
        label.setObjectName('toolsTableEmptyState')
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label.setContentsMargins(12, 24, 12, 24)
        table._empty_state_label = label
    label.setText(text)
    label.setAccessibleName(text)
    label.setStyleSheet(f'color: {theme.DATA_TEXT_SECONDARY}; background: transparent;')
    empty = table.rowCount() == 0
    label.setVisible(empty)
    table.setMaximumWidth(TOOLS_TABLE_MAX_WIDTH)
    header = max(40, table.fontMetrics().height() + 16)
    table.horizontalHeader().setFixedHeight(header)
    row_height = max(48, table.fontMetrics().height() + 24)
    for row in range(table.rowCount()):
        table.setRowHeight(row, row_height)
        for column in range(table.columnCount()):
            widget = table.cellWidget(row, column)
            if widget and widget.property('dataSecondary'):
                widget.setStyleSheet(f'color: {theme.DATA_TEXT_SECONDARY}; background: transparent; border: none;')
    body = max(96, label.heightForWidth(max(1, table.width() - 32))) if empty else row_height * min(table.rowCount(), 5)
    # A horizontal scrollbar takes its own space, leaving all five rows readable.
    horizontal = table.horizontalScrollBar().sizeHint().height() if table.horizontalScrollBar().maximum() else 0
    table.setFixedHeight(16 + header + body + horizontal)
    label.setGeometry(table.viewport().rect())
    label.raise_()


def data_heading(label):
    label.setProperty('role', 'dataSectionTitle')
    label.setContentsMargins(0, 0, 0, 0)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return label


def style_data_list(panel):
    """A wrapping list shares the table surface without form-row separators."""
    panel.setProperty('settingsDataList', True)
    panel.setMaximumWidth(TOOLS_TABLE_MAX_WIDTH)
    panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    panel.setStyleSheet(f"""
        QWidget#commandPacksContent {{ background: {theme.DATA_SURFACE}; border: none; border-radius: 10px; }}
        QWidget[dataListRow='true'] {{ background: {theme.DATA_SURFACE}; border: none; border-radius: 0; }}
        QWidget[dataListRow='true'][alternate='true'] {{ background: {theme.DATA_ROW_ALT}; }}
        QWidget[dataListRow='true']:hover {{ background: {theme.DATA_ROW_HOVER}; }}
        QWidget[dataListRow='true'] QLabel, QWidget[dataListRow='true'] QCheckBox {{ background: transparent; }}
        QLabel[dataSecondary='true'] {{ color: {theme.DATA_TEXT_SECONDARY}; }}
    """)
    visible = [panel.layout().itemAt(i).widget() for i in range(panel.layout().count())]
    for label in panel.findChildren(QLabel):
        if label.property('dataSecondary'):
            label.setStyleSheet(f'color: {theme.DATA_TEXT_SECONDARY}; background: transparent;')
    for i, row in enumerate(row for row in visible if row and not row.isHidden()):
        row.setProperty('alternate', bool(i % 2))
        row.style().unpolish(row)
        row.style().polish(row)


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
    QLabel[role='dataSectionTitle'] {{ color: {theme.ACCENT}; font-size: {theme.TYPE_HEADING}px; font-weight: 600; }}
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

    def addWidget(self, widget):
        """Retain the control-layout API used when a lazy tool moves its editor."""
        self.values.addWidget(widget)

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
