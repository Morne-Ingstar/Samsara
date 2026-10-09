"""Settings-only plain row treatment, applied after the shared guide scale."""
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QLabel, QHBoxLayout, QWidget, QSlider, QLineEdit, QSizePolicy
from shiboken6 import isValid
from samsara.ui import theme
from .calm_controls import switch, SegmentedChoice


def stylesheet():
    return f"""
    QFrame[role='row'], QFrame[role='row']:hover {{
        background: transparent; border: none; border-radius: 0; margin: 0; padding: 0;
    }}
    QFrame[role='row'] QLabel[role='label'] {{
        font-size: {theme.TYPE_EMPHASIS}px; font-weight: {theme.WEIGHT_SCALE['regular']};
    }}
    QLabel[role='settingsGroup'] {{ font-size: {theme.TYPE_HEADING}px;
        font-weight: {theme.WEIGHT_SCALE['semibold']}; color: {theme.TEXT_PRIMARY}; }}
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


class RowLayout(QObject):
    """Measure the actual control cluster before choosing a second line."""
    def __init__(self, row):
        super().__init__(row)
        self.row = row
        self.busy = False
        self.placed = None
        indent = theme.SPACE_3 if row.title in (
            'Hands-free capture volume', 'Hands-free idle volume', 'Start hidden', 'Indicator position') else 0
        row.grid.setContentsMargins(indent, theme.SPACE_2, 0, theme.SPACE_2)
        row.grid.setHorizontalSpacing(theme.SPACE_3)
        row.grid.setVerticalSpacing(theme.SPACE_2)
        row.text.layout().setSpacing(theme.SPACE_1)
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
        if row.help:
            row.grid.removeWidget(row.help)
            row.text.layout().removeWidget(row.heading)
            title_line = QWidget(row.text)
            title_layout = QHBoxLayout(title_line)
            title_layout.setContentsMargins(0, 0, 0, 0)
            title_layout.setSpacing(theme.SPACE_1)
            title_layout.addWidget(row.heading, 1)
            title_layout.addWidget(row.help, 0, Qt.AlignmentFlag.AlignTop)
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
