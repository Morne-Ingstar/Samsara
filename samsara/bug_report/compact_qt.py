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
        callback = getattr(self.owner, "_report_problem", None)
        if callback:
            callback(self.owner._active_chip()[0], self.owner._active_chip()[1])

    def resizeEvent(self, event):
        self.report.setGeometry(self.width()-76, (self.height()-28)//2, 68, 28)
        super().resizeEvent(event)

    def paintEvent(self, event):
        chip = self.owner._active_chip()
        self.report.setVisible(bool(chip and chip[1] in ("error", "warning")))
        super().paintEvent(event)
