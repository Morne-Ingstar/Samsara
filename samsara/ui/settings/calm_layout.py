"""Settings-only plain row treatment, applied after the shared guide scale."""
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QPainter, QLinearGradient
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QComboBox, QHBoxLayout, QVBoxLayout, QFrame, QWidget, QSlider, QLineEdit, QSizePolicy, QScrollArea
from shiboken6 import isValid
from samsara.ui import theme
from .calm_controls import switch, SegmentedChoice
from .presentation import SettingRow


def stylesheet():
    field_height = theme.TYPE_BODY + theme.SPACE_2
    field_margin = max(0, (theme.HIT_TARGET_MIN - field_height) // 2)
    return f"""
    QFrame[role='row'], QFrame[role='row']:hover {{
        background: transparent; border: none; border-radius: 0; margin: 0; padding: 0;
    }}
    QFrame[role='row'] QLabel[role='label'] {{
        font-size: {theme.TYPE_EMPHASIS}px; font-weight: {theme.WEIGHT_SCALE['regular']};
    }}
    QFrame[role='row'] QPushButton[settingsRowButton="true"] {{
        margin: {field_margin}px 0; padding: 0 {theme.SPACE_1}px;
        min-height: {field_height - 2}px; max-height: {field_height - 2}px;
        border-radius: 8px;
    }}
    QLabel[role='settingsGroup'] {{ font-size: {theme.TYPE_HEADING}px;
        font-weight: {theme.WEIGHT_SCALE['semibold']}; color: {theme.TEXT_PRIMARY}; }}
    QFrame#settingsPageHairline {{ background: {theme.BORDER_FAINT}; border: none; }}
    QComboBox, QAbstractSpinBox, QLineEdit, QPlainTextEdit {{
        font-weight: {theme.WEIGHT_SCALE['regular']}; }}
    QFrame#settingsSegments {{ background: {theme.BG1}; border: 1px solid {theme.BORDER};
        border-radius: {theme.CONTROL_RADIUS}px; }}
    QFrame#settingsSegments QRadioButton {{ background: transparent; margin: 0; padding: 0; }}
    QFrame[role='row'] QPushButton#helpButton {{ background: transparent;
        border: 1px solid transparent; padding: 0; margin: 0; }}
    QFrame[role='row'] QPushButton#helpButton:hover {{ background: {theme.HOVER_WASH}; }}
    QFrame[role='row'] QPushButton#helpButton:focus {{ border: 2px solid {theme.ACCENT}; }}
    QSlider {{ background: transparent; border: none; margin: 0; padding: 0; }}
    QSlider::groove:horizontal {{ height: {theme.SPACE_1}px; background: {theme.BG2};
        border: 1px solid {theme.BORDER}; border-radius: {theme.SPACE_1 // 2}px; }}
    QSlider::handle:horizontal {{ background: {theme.TEXT_PRIMARY};
        border: 2px solid {theme.BG0}; width: {theme.SPACE_2}px;
        margin: -{theme.SPACE_1}px 0; border-radius: {theme.CONTROL_RADIUS}px; }}
    QSlider::handle:horizontal:hover {{ border-color: {theme.TEXT_SECONDARY}; }}
    QSlider::handle:horizontal:focus {{ border-color: {theme.ACCENT}; }}
    QSlider::handle:horizontal:disabled {{ background: {theme.TEXT_DISABLED}; }}
    """


def _style_row_button(button):
    field_height = theme.TYPE_BODY + theme.SPACE_2
    margin = max(0, (theme.HIT_TARGET_MIN - field_height) // 2)
    qss = (
        "\n/* Settings row button face */\nQPushButton { "
        f"margin: {margin}px 0; padding: 0 {theme.SPACE_1}px; "
        f"min-height: {field_height - 2}px; max-height: {field_height - 2}px; "
        f"border-width: 1px; border-radius: 8px; background: {theme.BG2}; "
        f"border-color: {theme.BORDER}; }}"
    )
    original = button.styleSheet().split("/* Settings row button face */")[0].rstrip()
    if button.styleSheet() != original + qss:
        button.setStyleSheet(original + qss)


class RowLayout(QObject):
    """Measure the actual control cluster before choosing a second line."""
    def __init__(self, row):
        super().__init__(row)
        self.row = row
        self.busy = False
        self.placed = None
        indent = theme.SPACE_3 if row.title in (
            'Hands-free capture volume', 'Hands-free idle volume') else 0
        row.grid.setContentsMargins(indent, theme.SPACE_2, 0, theme.SPACE_2)
        row.grid.setHorizontalSpacing(theme.SPACE_3)
        row.grid.setVerticalSpacing(theme.SPACE_2)
        row.text.layout().setSpacing(theme.SPACE_1)
        row.grid.setAlignment(row.text, Qt.AlignmentFlag.AlignVCenter)
        # Remove the old trailing stretch so the cluster ends at the page inset.
        for index in reversed(range(row.values.count())):
            item = row.values.itemAt(index)
            if item.spacerItem() is not None:
                row.values.takeAt(index)
        row.values.setSpacing(theme.SPACE_1)
        for index in range(row.values.count()):
            item = row.values.itemAt(index)
            if item.widget():
                row.values.setAlignment(item.widget(), Qt.AlignmentFlag.AlignVCenter)
        # Preserve the 44 DIP target while matching the original 32 DIP field
        # face used by Settings dropdowns at the base commit.
        for control in (*row.value.findChildren(QPushButton), *row.value.findChildren(QComboBox)):
            control.setFixedHeight(theme.HIT_TARGET_MIN)
        for button in row.value.findChildren(QPushButton):
            button.setProperty("settingsRowButton", True)
            # theme.style_field() attaches input geometry directly to the
            # widget, which outranks the page stylesheet. Reassert the same
            # compact painted face used by dropdowns at widget specificity;
            # the widget itself remains a 44 DIP hit target.
            _style_row_button(button)
        if row.help:
            row.grid.removeWidget(row.help)
            row.text.layout().removeWidget(row.heading)
            title_line = QWidget(row.text)
            title_layout = QHBoxLayout(title_line)
            title_layout.setContentsMargins(0, 0, 0, 0)
            title_layout.setSpacing(theme.SPACE_1)
            title_layout.addWidget(row.heading)
            row.help.setParent(row)
            title_layout.addSpacing(theme.HIT_TARGET_MIN + theme.SPACE_1)
            title_layout.addStretch()
            row.text.layout().insertWidget(0, title_line)
        row.grid.setColumnStretch(1, 0)
        row.grid.setColumnStretch(2, 0)
        # Keep factory warnings/status copy below the reserved control line.
        extras = []
        for index in range(row.grid.count()):
            item = row.grid.itemAt(index)
            position = row.grid.getItemPosition(index)
            if position[0] > 0 and item.widget() is not None:
                extras.append((item.widget(), position, item.alignment()))
        for widget, (line, column, line_span, column_span), alignment in reversed(extras):
            row.grid.removeWidget(widget)
            row.grid.addWidget(widget, line + 1, column, line_span, column_span, alignment)
        if hasattr(row, '_wake_profile_controls'):
            for label in row.value.findChildren(QLabel):
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            for field in row.value.findChildren(QLineEdit):
                field.setMinimumWidth(theme.HIT_TARGET_MIN)
                field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        row.installEventFilter(self)
        self.reflow()

    def eventFilter(self, widget, event):
        if event.type() == QEvent.Type.Resize:
            self.reflow()
            # The legacy row uses a fixed 730px cutoff; measurements replace it.
            return True
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Show, QEvent.Type.StyleChange):
            self.reflow()
        return False

    def reflow(self):
        if self.busy:
            return
        self.busy = True
        try:
            row = self.row
            required = row.value.sizeHint().width()
            margins = row.grid.contentsMargins()
            row.value.setMaximumWidth(max(theme.HIT_TARGET_MIN, row.width() - margins.left() - margins.right()))
            narrow = row.width() - margins.left() - required - theme.SPACE_3 < 5 * theme.SPACE_7
            if narrow != self.placed:
                row.grid.removeWidget(row.value)
                if narrow:
                    row.grid.addWidget(row.value, 1, 0, 1, 3, Qt.AlignmentFlag.AlignRight)
                else:
                    row.grid.addWidget(row.value, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.placed = narrow
            row._narrow = narrow
            # Qt's nested wrapped labels must retain the height their ink needs.
            for label in (row.heading, row.detail):
                if label.width() > 0:
                    label.setMinimumHeight(max(label.fontMetrics().height(), label.heightForWidth(label.width())))
            if not narrow:
                row.grid.activate()
                row.text.layout().activate()
                row.heading.parentWidget().layout().activate()
                anchor = row.heading.mapTo(row, QPoint(0, 0))
                control_height = row.value.sizeHint().height()
                control_y = anchor.y() + (row.heading.height() - control_height) // 2
                row.value.setGeometry(row.value.x(), control_y, row.value.width(), control_height)
            if row.help:
                row.grid.activate()
                row.text.layout().activate()
                row.heading.parentWidget().layout().activate()
                anchor = row.heading.mapTo(row, row.heading.rect().topRight())
                top = anchor.y() + (row.heading.height() - row.help.height()) // 2
                row.help.move(anchor.x() + 1 + theme.SPACE_1,
                              max(0, min(row.height() - row.help.height(), top)))
        finally:
            self.busy = False


class GroupHeading(QLabel):
    def __init__(self, text, rows, disclosure=None):
        super().__init__(text)
        self.setProperty('role', 'settingsGroup')
        self.rows = rows
        self.disclosure = disclosure
        self.setContentsMargins(0, theme.SPACE_4, 0, theme.SPACE_2)
        for row in rows:
            row.installEventFilter(self)

    def eventFilter(self, widget, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            QTimer.singleShot(0, self, self.refresh)
        return False

    def refresh(self):
        self.rows = [row for row in self.rows if isValid(row)]
        self.setVisible(any(not row.isHidden() for row in self.rows))
        if self.disclosure is not None and isValid(self.disclosure):
            self.disclosure.refresh_filter_state()


def group_page(page, starts):
    """Headings are metadata; never change four-field specs, order or budgets."""
    groups = []
    for layout in (page.base, page.advanced.rows):
        rows = [layout.itemAt(index).widget() for index in range(layout.count())
                if hasattr(layout.itemAt(index).widget(), 'widget_keys')]
        current = None
        for row in rows:
            heading = starts.get(row.widget_keys[0])
            if heading:
                # Advanced may continue the last basic group. Keep its heading
                # at the first member, while retaining later, distinct sections.
                if not groups or groups[-1][0] != heading:
                    groups.append((heading, [], layout))
                current = groups[-1][1]
            if current is not None:
                current.append(row)
    for title, members, layout in groups:
        heading = GroupHeading(title, members, page.advanced if layout is page.advanced.rows else None)
        index = layout.indexOf(members[0])
        layout.insertWidget(index, heading)
        heading.refresh()


def apply_settings(window):
    window.setStyleSheet(window.styleSheet() + '\n' + stylesheet())
    for page in window._pages.values():
        page.column.setContentsMargins(theme.SPACE_4, theme.SPACE_3, theme.SPACE_4, theme.SPACE_3)
    for row in window._destination_rows:
        if not hasattr(row, '_calm_layout'):
            row._calm_layout = RowLayout(row)
        row._calm_layout.reflow()
        for button in row.value.findChildren(QPushButton):
            if button.property("settingsRowButton"):
                _style_row_button(button)
        for checkbox in row.findChildren(QCheckBox):
            switch(checkbox)
        for choice in row.findChildren(SegmentedChoice):
            choice.refresh()
        for slider in row.findChildren(QSlider):
            slider.setMinimumHeight(theme.HIT_TARGET_MIN)
            # Source sliders carry an inline sheet, which outranks the window.
            # Own the entire slider sheet: no opaque native background survives.
            slider.setStyleSheet(stylesheet())
        row.update()
    for row in window.findChildren(SettingRow):
        if not hasattr(row, '_calm_layout'):
            row._calm_layout = RowLayout(row)
        row._calm_layout.reflow()
        for button in row.value.findChildren(QPushButton):
            if button.property("settingsRowButton"):
                _style_row_button(button)
    if not hasattr(window, '_component_rows'):
        window._component_rows = ComponentRows(window._components_page)
    window._component_rows.refresh()
    for page in dict.fromkeys((*window._pages.values(), *window._tool_pages.values())):
        for scroll in (page, *page.findChildren(QScrollArea)):
            scroll.verticalScrollBar().setStyleSheet(f"""
                QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
                QScrollBar::handle:vertical {{ background: {theme.BORDER}; border-radius: 4px; min-height: 24px; }}
                QScrollBar::handle:vertical:hover {{ background: {theme.ICON_IDLE}; }}
                QScrollBar::handle:vertical:pressed {{ background: {theme.ACCENT}; }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
                QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            """)
            if not hasattr(scroll, '_settings_edges'):
                scroll._settings_edges = ScrollEdges(scroll)
            scroll._settings_edges.refresh()
        for layout in page.widget().findChildren(QVBoxLayout):
            if not hasattr(layout, '_settings_dividers'):
                layout._settings_dividers = RowDividers(layout)
            layout._settings_dividers.refresh()


def row_separator(layout, index):
    """373's 1-DIP BORDER_FAINT line with equal SPACE_1/2 air on both sides.

    Lifted from first_run_pages_qt.py:291-303; setup remains independent.
    """
    space = QWidget()
    space.setProperty('settingsRowDivider', True)
    space.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    space.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    column = QVBoxLayout(space)
    column.setContentsMargins(0, theme.SPACE_1 // 2, 0, theme.SPACE_1 // 2)
    column.setSpacing(0)
    line = QFrame(space)
    line.setObjectName('settingsRowHairline')
    line.setFixedHeight(1)
    column.addWidget(line)
    layout.insertWidget(index, space)
    return space, line


class RowDividers(QObject):
    """One separator per visible adjacency, within the existing row groups."""
    def __init__(self, layout):
        super().__init__(layout)
        self.layout = layout
        self.groups = []
        self.dividers = []
        self.busy = False
        layout.parentWidget().installEventFilter(self)
        self.refresh()

    def collect(self):
        groups = []
        current = []
        if not isValid(self.layout):
            return groups
        for index in range(self.layout.count()):
            item = self.layout.itemAt(index)
            widget = item.widget() if item is not None else None
            # Layouts can outlive widgets during the lazy Settings page
            # rebuild.  QObject wrappers remain in the layout briefly after
            # deleteLater(), but asking one for a property raises
            # RuntimeError (wrapped by Qt as an uncaught SystemError).
            if widget is not None and not isValid(widget):
                continue
            if widget and widget.property('settingsRowDivider'):
                continue
            if widget and widget.isHidden() and widget.property('role') not in ('row', 'settingsGroup') and not widget.property('settingsToolRow'):
                continue
            if widget and (widget.property('role') == 'row' or widget.property('settingsToolRow')):
                current.append(widget)
            elif current:
                groups.append(current)
                current = []
        if current:
            groups.append(current)
        return groups

    def eventFilter(self, widget, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.LayoutRequest):
            QTimer.singleShot(0, self, self.refresh)
        return False

    def refresh(self):
        if self.busy or not isValid(self.layout):
            return
        self.busy = True
        try:
            # Do not retain deleted wrappers between a page rebuild and the
            # next queued LayoutRequest.
            self.groups = [
                [row for row in group if isValid(row)]
                for group in self.groups
            ]
            self.groups = [group for group in self.groups if group]
            groups = self.collect()
            if groups != self.groups:
                for _, _, divider, _ in self.dividers:
                    if isValid(divider):
                        self.layout.removeWidget(divider)
                        divider.hide()
                        divider.deleteLater()
                self.groups, self.dividers = groups, []
                for group in groups:
                    for row in group:
                        row.installEventFilter(self)
                    for row in group[:-1]:
                        divider, line = row_separator(self.layout, self.layout.indexOf(row) + 1)
                        self.dividers.append((group, row, divider, line))
                if groups:
                    self.layout.setSpacing(0)
            for group, row, divider, line in self.dividers:
                if not all(isValid(obj) for obj in (row, divider, line)):
                    continue
                visible = [member for member in group if isValid(member) and not member.isHidden()]
                divider.setVisible(row in visible and row is not visible[-1])
                qss = f'background:{theme.BORDER_FAINT};border:none;'
                if line.styleSheet() != qss:
                    line.setStyleSheet(qss)
        finally:
            self.busy = False


class ComponentRows(QObject):
    """Treat asynchronously rebuilt component cards only inside Settings."""
    def __init__(self, components):
        super().__init__(components.widget)
        self.components = components
        components._rows_host.installEventFilter(self)
        components._rows_host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        components.widget.layout().setContentsMargins(0, 0, 0, 0)
        components.widget.layout().setAlignment(components._check_btn, Qt.AlignmentFlag.AlignRight)

    def eventFilter(self, widget, event):
        if event.type() == QEvent.Type.LayoutRequest:
            self.refresh()
        return False

    def refresh(self):
        components = self.components
        for controls in components._rows.values():
            if controls.get('_settings_card') is not None:
                continue
            card = controls['get'].parentWidget()
            controls['_settings_card'] = card
            card.setProperty('settingsToolRow', True)
            card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            card.setStyleSheet('background:transparent;border:none;')
            column = card.layout()
            column.setContentsMargins(0, theme.SPACE_2, 0, theme.SPACE_2)
            title = column.takeAt(0).widget()
            detail = column.takeAt(0).widget()
            actions = column.takeAt(0).layout()
            note = controls['note']
            actions.removeWidget(note)
            buttons = [controls['get'], controls['cancel']]
            cancel_visible = not controls['cancel'].isHidden()
            for button in buttons:
                actions.removeWidget(button)
                button.setMinimumWidth(button.fontMetrics().horizontalAdvance(button.text()) + theme.SPACE_5)
            row = SettingRow(title.text(), detail.text(), buttons)
            row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            row.grid.addWidget(note, 2, 0, 1, 3)
            row._calm_layout = RowLayout(row)
            # SettingRow exposes controls; restore the source's fetch state.
            controls['cancel'].setVisible(cancel_visible)
            column.insertWidget(0, row)
            title.hide()
            detail.hide()
            title.deleteLater()
            detail.deleteLater()
            actions.deleteLater()
        layout = components._rows_layout
        if not isValid(layout):
            return
        if not hasattr(layout, '_settings_dividers'):
            layout._settings_dividers = RowDividers(layout)
        layout._settings_dividers.refresh()


class ScrollFade(QWidget):
    """A mouse-transparent viewport overlay; rows keep their native rendering."""
    def __init__(self, viewport, top):
        super().__init__(viewport)
        self.top = top
        self.setObjectName('settingsTopFade' if top else 'settingsBottomFade')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet('background:transparent;border:none;')

    def paintEvent(self, event):
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        opaque = theme.qcolor(theme.BG0)
        clear = theme.qcolor(theme.BG0)
        clear.setAlpha(0)
        gradient.setColorAt(0, opaque if self.top else clear)
        gradient.setColorAt(1, clear if self.top else opaque)
        painter.fillRect(self.rect(), gradient)


class ScrollEdges(QObject):
    def __init__(self, scroll):
        super().__init__(scroll)
        self.scroll = scroll
        self.top = ScrollFade(scroll.viewport(), True)
        self.bottom = ScrollFade(scroll.viewport(), False)
        scroll.viewport().installEventFilter(self)
        bar = scroll.verticalScrollBar()
        bar.valueChanged.connect(self.refresh)
        bar.rangeChanged.connect(self.refresh)

    def eventFilter(self, widget, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.refresh()
        return False

    def refresh(self, *_):
        viewport = self.scroll.viewport()
        height = min(theme.SPACE_3, viewport.height() // 2)
        bar = self.scroll.verticalScrollBar()
        for overlay, top in ((self.top, True), (self.bottom, False)):
            overlay.setGeometry(0, 0 if top else viewport.height() - height, viewport.width(), height)
            overlay.setVisible(bar.maximum() > bar.minimum() and (
                bar.value() > bar.minimum() if top else bar.value() < bar.maximum()))
            overlay.raise_()
            overlay.update()
