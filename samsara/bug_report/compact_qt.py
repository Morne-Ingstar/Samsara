"""Clickable report control inside Compact's existing message hit window."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton
from samsara.ui import theme
from samsara.ui.indicator_message_layout import MessageHoverWindow


class ReportMessageWindow(MessageHoverWindow):
    def __init__(self, owner):
        super().__init__(owner)
        self.report = QPushButton("Report", self)
        self.report.setAccessibleName("Report this problem")
        self.report.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.report.setFont(theme.qfont(theme.TYPE_MIN))
        self.report.setStyleSheet(f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 5px; padding: 0; }}")
        self.report.clicked.connect(self._report)
        from samsara.ui.history_result_link import HistoryResultLabel
        self.history_label = HistoryResultLabel(self)
        self.history_label.setObjectName('dictationHistoryLink')
        self.history_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.history_label.setStyleSheet(f'color: {theme.TEXT_PRIMARY}; background: transparent; border: 0;')
        self.history_label.hide()

    def sync_history(self):
        from samsara.ui.history_result_link import set_history_link
        from samsara.ui.indicator_message_layout import message_lines
        chip = self.owner._active_chip()
        callback = getattr(self.owner._chip_feedback, 'history', None) if chip else None
        label = self.history_label
        if callback:
            font = self.owner._chip_font()
            label.setFont(font)
            style = (f'QLabel#dictationHistoryLink {{ color: {theme.TEXT_PRIMARY}; background: transparent; border: 0; '
                     f'font-family: "Segoe UI"; font-size: {theme.TYPE_MIN}px; font-weight: {700 if font.bold() else 400}; }}')
            if label.styleSheet() != style:
                label.setStyleSheet(style)
            label.ensurePolished()
            width = max(1, self.width()-24-self.owner._report_width())
            lines, _ = message_lines(chip[0], label.font(), width, max_lines=None)
            label.setGeometry(12, 0, width, self.height())
            set_history_link(label, '\n'.join(lines), callback)
            label.setAccessibleName(chip[0])
            label.show()
        else:
            set_history_link(label, '', None)
            label.hide()
        # The link can receive Tab/Enter without activating on appearance.
        passive = not bool(callback)
        if bool(self.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus) != passive:
            self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, passive)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def _report(self):
        if getattr(self.owner._chip_feedback, 'owner', '') == 'dictation_retry':
            callback = getattr(self.owner, '_dictation_retry_callback', None)
            if callback:
                callback()
            return
        callback = getattr(self.owner, "_report_problem", None)
        if callback:
            callback(self.owner._active_chip()[0], self.owner._active_chip()[1])

    def resizeEvent(self, event):
        width = self.owner._report_width()
        self.report.setGeometry(self.width()-width, (self.height()-28)//2, max(68, width-8), 28)
        self.sync_history()
        super().resizeEvent(event)

    def paintEvent(self, event):
        chip = self.owner._active_chip()
        retry = getattr(self.owner._chip_feedback, 'owner', '') == 'dictation_retry'
        self.report.setText('Try again' if retry else 'Report')
        self.report.setAccessibleName('Try again' if retry else 'Report this problem')
        self.report.setVisible(bool(chip and (retry or chip[1] in ("error", "warning"))))
        self.sync_history()
        super().paintEvent(event)
