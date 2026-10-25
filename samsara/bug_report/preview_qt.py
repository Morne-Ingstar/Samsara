"""Review the exact report before opening the user's browser."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout)
from samsara.ui import theme


PRIVACY_TEXT = ("Review before sharing. Send to GitHub opens a public issue draft in your browser. "
                "You still choose whether to submit it there.")


def report_style():
    return (f"QDialog {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; }} "
            f"QLabel {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; }} "
            f"QLineEdit, QPlainTextEdit {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; padding: 8px; font-size: {theme.TYPE_BODY}px; }} "
            f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 8px 14px; font-size: {theme.TYPE_BODY}px; }} "
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}")


class ReportPreview(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("Report a problem")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self.setStyleSheet(report_style())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel("Report a problem")
        title.setFont(theme.qfont(theme.TYPE_HEADING))
        title.setStyleSheet(f"font-size: {theme.TYPE_HEADING}px;")
        layout.addWidget(title)
        privacy = QLabel(PRIVACY_TEXT)
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        layout.addWidget(QLabel("What were you doing? (optional)"))
        self.activity = QLineEdit()
        self.activity.setAccessibleName("What were you doing?")
        layout.addWidget(self.activity)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setAccessibleName("Exact report text")
        layout.addWidget(self.body, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch()
        self.send = QPushButton("Send to GitHub")
        self.copy = QPushButton("Copy")
        self.cancel = QPushButton("Cancel")
        for button in (self.send, self.copy, self.cancel):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.activity.textChanged.connect(self._refresh)
        self.cancel.clicked.connect(self.close)
        self.copy.clicked.connect(self._copy)
        self.send.clicked.connect(self._send)
        self._refresh()
        self.ensurePolished()
        height_budget = max(160, self.screen().availableGeometry().height()-460)
        self.body.setMinimumHeight(min(320, height_budget, max(180, (report.count('\n')+5)*self.body.fontMetrics().lineSpacing()+24)))
        width = min(660, self.screen().availableGeometry().width()-48)
        self.resize(width, max(self.sizeHint().height(), layout.totalHeightForWidth(width)))

    def _refresh(self):
        from .redact import activity_text
        text = ("What were you doing?\n" + activity_text(self.activity.text()) + "\n\n" + self.report)
        self.body.setPlainText(text)

    def _copy(self):
        QApplication.clipboard().setText(self.body.toPlainText())
        self.status.setText("Report copied.")

    def _send(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from .github_issue import issue_draft, PASTE_NOTICE
        draft = issue_draft(self.body.toPlainText())
        if draft.clipboard is not None:
            QApplication.clipboard().setText(draft.clipboard)
        try:
            opened = QDesktopServices.openUrl(QUrl(draft.url))
        except Exception:
            opened = False
        self.status.setText((PASTE_NOTICE if draft.clipboard is not None else "Issue draft opened in your browser.")
                            if opened else "Could not open your browser. Use Copy and open GitHub manually.")


class CrashOffer(QDialog):
    def __init__(self, on_report):
        super().__init__()
        self.setWindowTitle("Previous session")
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self.setStyleSheet(report_style())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(QLabel("Samsara closed unexpectedly last time. Report it?"))
        actions = QHBoxLayout()
        actions.addStretch()
        report, cancel = QPushButton("Report"), QPushButton("Not now")
        report.clicked.connect(lambda: (on_report(), self.close()))
        cancel.clicked.connect(self.close)
        actions.addWidget(report)
        actions.addWidget(cancel)
        layout.addLayout(actions)
