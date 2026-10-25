"""Persistent, catalogue-backed command details inside the floating reference."""
from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QFont, QFontMetrics, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from samsara.ui import theme
from samsara.ui.settings import sidebar
from samsara.ui.command_reference_surfaces import row_fill

# Owner-editable stand-in; descriptions themselves always come from the catalogue.
NO_DESCRIPTION = "No description is available for this command."


def row_help_lines(row):
    """Use only catalog copy: a description and the first reviewed example."""
    lines = []
    description = row.get('description', '').strip()
    if description:
        lines.append(description)
    examples = row.get('examples', ())
    if examples:
        lines.append('Say: ' + examples[0])  # PLACEHOLDER COPY (855)
    return lines


class _RunButton(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)


def style_search(edit, height=None):
    """Use Settings' search field, including its keyboard-only ring and glyph."""
    edit.setObjectName('settingsSearchField')
    sheet = sidebar.search_qss()
    height = height or theme.HIT_TARGET_MIN
    # Keep Settings' search glyph, focus ring and colours at a compact height.
    for border in (2, 2 * sidebar.FOCUS_RING):
        sheet = sheet.replace(f'height: {theme.HIT_TARGET_MIN - border}px', f'height: {height - border}px')
    edit.setStyleSheet(sheet)
    edit.setFixedHeight(height)
    edit.setTextMargins(sidebar.ICON_BOX + theme.SPACE_2 - sidebar.FOCUS_RING - sidebar._LINE_EDIT_MARGIN, 0, 0, 0)
    edit.setProperty('settingsInputTextInset', sidebar.ICON_INSET + sidebar.ICON_BOX + theme.SPACE_2)
    sidebar.SearchGlyph(edit)


class CommandRowDelegate(QStyledItemDelegate):
    """One-line scan rows; full catalog text stays in accessible item data."""

    expanded_phrase = None
    detail_height = 0

    @staticmethod
    def tiers(index):
        row = index.data(Qt.ItemDataRole.UserRole + 3) or {}
        primary = row.get('display_phrase', row.get('phrase', index.data() or '')).title()
        aliases = row.get('display_aliases', row.get('shown_aliases', ()))
        secondary = 'also: ' + ', '.join(aliases) if aliases else ''
        return primary, secondary

    @staticmethod
    def tier_styles():
        primary = QFont(theme.FONT_FAMILY)
        primary.setPixelSize(theme.TYPE_BODY)
        primary.setWeight(QFont.Weight.DemiBold)
        secondary = QFont(theme.FONT_FAMILY)
        secondary.setPixelSize(theme.TYPE_MIN)
        return ((primary, theme.TEXT_PRIMARY), (secondary, theme.DATA_TEXT_SECONDARY))

    def header_height(self):
        return max(theme.HIT_TARGET_MIN, max(QFontMetrics(font).height()
                   for font, _ in self.tier_styles()) + 20)

    def text_layout(self, index, width):
        styles = self.tier_styles()
        row = index.data(Qt.ItemDataRole.UserRole + 3) or {}
        primary = self.tiers(index)[0]
        summary = ' '.join(row.get('description', '').split())
        available = max(1, width - 32)
        # One shared name column for the filtered list, capped for narrow hosts.
        # Cache measurements across paints; filtering gives rows a new generation.
        model = index.model()
        key = (model.rowCount(), index.data(Qt.ItemDataRole.UserRole + 2), styles[0][0].toString())
        if getattr(self, '_column_key', None) != key:
            metrics = QFontMetrics(styles[0][0])
            self._name_width = max((metrics.horizontalAdvance(self.tiers(model.index(i, 0))[0])
                                    for i in range(model.rowCount())), default=100) + 8
            self._column_key = key
        name_width = min(available, max(100, min(self._name_width, available * 2 // 5)))
        height = self.header_height()
        parts = [(primary, *styles[0], QRect(16, 0, name_width, height))]
        remaining = available - name_width - 16
        if summary and remaining > 40:
            parts.append((summary, *styles[1], QRect(32 + name_width, 0, remaining, height)))
        expanded = row.get('phrase') == self.expanded_phrase
        return parts, height + (self.detail_height if expanded else 0)

    def sizeHint(self, option, index):
        width = self.parent().viewport().width()
        return QSize(0, self.text_layout(index, width)[1])

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        hovered = bool(styled.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setClipRect(styled.rect)
        painter.fillRect(styled.rect, theme.qcolor(row_fill(index.row(), selected, hovered)))
        if selected:
            painter.fillRect(styled.rect.adjusted(0, 8, -styled.rect.width() + 3, -8), theme.qcolor(theme.ACCENT))
        for text, font, colour, rect in self.text_layout(index, styled.rect.width())[0]:
            painter.setFont(font)
            painter.setPen(theme.qcolor(theme.TEXT_PRIMARY if selected else colour))
            visible = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(rect.translated(styled.rect.topLeft()), Qt.AlignmentFlag.AlignVCenter, visible)
        if styled.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
            painter.drawRoundedRect(styled.rect.adjusted(2, 2, -2, -2), 6, 6)
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
            f'QFrame#commandDetailCard{{background:{theme.DATA_SURFACE};border:1px solid {theme.BORDER};'
            f'border-radius:{theme.CONTROL_RADIUS}px;}}'
            f'QLabel{{background:transparent;border:none;color:{theme.TEXT_PRIMARY};}}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_2, theme.SPACE_2, theme.SPACE_2, theme.SPACE_2)
        layout.setSpacing(theme.SPACE_1)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet(f'QScrollArea,QScrollArea>QWidget>QWidget{{background:{theme.DATA_SURFACE};border:none;}}' + theme.SCROLLBAR_QSS)
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
            label.setStyleSheet(f'font-size:{theme.TYPE_SECONDARY}px;color:{theme.DATA_TEXT_SECONDARY};')
            self.column.addWidget(label)
            self.labels.append(label)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.metadata = QLabel()
        self.metadata.setWordWrap(True)
        self.metadata.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.metadata)
        self.pack_status = QLabel()
        self.pack_status.setWordWrap(True)
        layout.addWidget(self.pack_status)
        self.enable_button = _RunButton()
        self.enable_button.setStyleSheet(theme.primary_button_qss())
        self.enable_button.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.enable_button.clicked.connect(self.enable_pack)
        layout.addWidget(self.enable_button, 0, Qt.AlignmentFlag.AlignLeft)
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
        window._list.verticalScrollBar().valueChanged.connect(self._scrolled)

    def _scrolled(self, _value):
        if not getattr(getattr(self.window, '_disclosure', None), 'placing', False):
            self.dismiss()

    def open(self, command, origin, anchor):
        self.dismiss()
        self.origin = origin
        inline = origin is self.window._list
        fill = theme.DATA_ROW_SELECTED if inline else theme.DATA_SURFACE
        border = 'none' if inline else f'1px solid {theme.BORDER}'
        self.setStyleSheet(
            f'QFrame#commandDetailCard{{background:{fill};border:{border};border-radius:0;}}'
            f'QLabel{{background:transparent;border:none;color:{theme.TEXT_PRIMARY};}}')
        self.scroll.setStyleSheet(f'QScrollArea,QScrollArea>QWidget>QWidget{{background:{fill};border:none;}}' + theme.SCROLLBAR_QSS)
        self.phrase = command['phrase']
        self._pack_id = command.get('pack', 'core')
        self._anchor = anchor
        disabled = bool(command.get('pack_disabled'))
        self.metadata.setText(', '.join(command.get('row_tags', ())))
        self.metadata.setVisible(bool(self.metadata.text()))
        arguments = command.get('args', ())
        examples = command.get('examples', ())
        self._run_phrase = examples[0] if arguments and examples else self.phrase
        from samsara.command_packs import PACKS
        pack_name = PACKS.get(self._pack_id, {}).get('label', self._pack_id)
        self.pack_status.setText("This command's pack is off" if disabled else '')
        self.pack_status.setVisible(disabled)
        self.enable_button.setText('Turn on ' + pack_name)
        self.enable_button.setVisible(disabled and callable(getattr(self.window, '_enable_pack_cb', None)))
        self.run_button.setVisible(not disabled and (not arguments or bool(examples)))
        aliases = [str(a) for a in command.get('aliases', ()) if a != self.phrase]
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
        description = '\n'.join(text for text in fields + [self.metadata.text()] if text)
        self.setAccessibleDescription(description)
        self.run_button.setAccessibleName('Run it: ' + self._run_phrase)
        self.run_button.setAccessibleDescription(description)
        self.place(anchor)
        self.show()
        self.raise_()
        if not self._filter_installed:
            QApplication.instance().installEventFilter(self)
            self._filter_installed = True
        self.scroll.verticalScrollBar().setValue(0)
        origin.setFocus(Qt.FocusReason.OtherFocusReason)
        first_action = self.enable_button if disabled else (
            self.run_button if self.run_button.isVisible() else self.close_button)
        QWidget.setTabOrder(origin, first_action)
        QWidget.setTabOrder(self.enable_button, self.close_button)
        QWidget.setTabOrder(self.run_button, self.close_button)
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self, description))

    def place(self, anchor):
        area = self.parentWidget().rect().adjusted(theme.SPACE_2, theme.SPACE_1, -theme.SPACE_2, -theme.SPACE_1)
        if not self.pack_status.isHidden():
            area.setTop(max(area.top(), self.window._list.mapTo(self.parentWidget(), self.window._list.rect().topLeft()).y()))
        width = area.width()
        disclosure = getattr(self.window, '_disclosure', None)
        inline = self.origin is self.window._list and disclosure is not None and self.window._list.viewport().height() >= 160
        if inline:
            width = self.window._list.viewport().width()
        text_width = max(1, width - 2 * theme.SPACE_2 - 2)
        body_height = sum(label.heightForWidth(text_width) for label in self.labels if not label.isHidden())
        body_height += theme.SPACE_1 * (sum(not label.isHidden() for label in self.labels) - 1)
        height = min(area.height(), body_height + 2 * theme.SPACE_2 + theme.SPACE_1 + theme.HIT_TARGET_MIN + 4)
        if not self.metadata.isHidden():
            height = min(area.height(), height + self.metadata.heightForWidth(text_width) + theme.SPACE_1)
        if not self.pack_status.isHidden():
            height = min(area.height(), height + self.pack_status.heightForWidth(text_width) + theme.SPACE_1)
        if not self.enable_button.isHidden():
            height = min(area.height(), height + self.enable_button.sizeHint().height() + theme.SPACE_1)
        if inline:
            if disclosure.place(self, height):
                return
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
        disclosure = getattr(self.window, '_disclosure', None)
        if disclosure is not None:
            disclosure.collapse()
        self.phrase = None
        if restore_focus and self.origin is not None and isValid(self.origin):
            self.origin.setFocus(Qt.FocusReason.OtherFocusReason)
        self.origin = None

    def run(self):
        if not self.isVisible() or not self.phrase:
            return
        phrase = getattr(self, '_run_phrase', self.phrase)
        self.dismiss(True)
        if phrase:
            self.window._execute(phrase)

    def enable_pack(self):
        phrase, anchor = self.phrase, self._anchor
        try:
            if self.window._enable_pack_cb(self._pack_id, self.window):
                self.window.refresh_commands()
            self.window._show_details(phrase, self.window._list, anchor)
        except Exception:
            self.window._show_details(phrase, self.window._list, anchor)
            page = {'hermes': 'Integrations', 'ai': 'Ava'}.get(self._pack_id, 'Commands')
            self.pack_status.setText(f'Could not turn on this pack. Try again in Settings > {page}.')
            self.pack_status.show()

    def eventFilter(self, watched, event):
        if not self.isVisible():
            return False
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.dismiss(True)
                return True
            if watched is self.origin and event.key() == Qt.Key.Key_Tab:
                action = self.enable_button if self.enable_button.isVisible() else (
                    self.run_button if self.run_button.isVisible() else self.close_button)
                action.setFocus(Qt.FocusReason.TabFocusReason)
                return True
            if watched is self.origin and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                self.dismiss()
        elif kind == QEvent.Type.MouseButtonPress:
            # Keep list geometry stable until native hit-testing has chosen the
            # clicked row. Collapsing here moves that row out from under it.
            if watched is self.window._list.viewport():
                return False
            if isinstance(watched, QWidget) and watched is not self and not self.isAncestorOf(watched):
                self.dismiss()
        elif watched is self.window and kind in (QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.WindowDeactivate):
            self.dismiss()
        return False
