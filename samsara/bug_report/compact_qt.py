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
        super().resizeEvent(event)

    def paintEvent(self, event):
        chip = self.owner._active_chip()
        retry = getattr(self.owner._chip_feedback, 'owner', '') == 'dictation_retry'
        self.report.setText('Try again' if retry else 'Report')
        self.report.setAccessibleName('Try again' if retry else 'Report this problem')
        self.report.setVisible(bool(chip and (retry or chip[1] in ("error", "warning"))))
        super().paintEvent(event)
