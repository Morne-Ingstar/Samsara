"""Review a finished ZIP before a single explicit send or browser handoff."""
import threading
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout)
from samsara.ui import theme


PRIVACY_TEXT = ("Review before sharing. Send to GitHub opens a public issue draft in your browser. "
                "You still choose whether to submit it there.")
# PLACEHOLDER COPY (980b): consent sentence requires owner review.
UPLOAD_CONSENT = ("Send the reviewed diagnostics and optional contact to the developer at morneis.com. "
                  "No audio is included.")


def report_style():
    return (f"QDialog {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; }} "
            f"QLabel {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; }} "
            f"QLineEdit, QPlainTextEdit {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; padding: 8px; font-size: {theme.TYPE_BODY}px; }} "
            f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 8px 14px; font-size: {theme.TYPE_BODY}px; }} "
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}")


class ReportPreview(QDialog):
    upload_finished = Signal(object)

    def __init__(self, report, parent=None, *, config=None, directory=None,
                 settings=None, on_contact=None, on_spoken=None):
        super().__init__(parent)
        from samsara.paths import samsara_home_dir
        from .upload import load_settings
        self.report = report
        self.config = config or {}
        self.directory = Path(directory) if directory is not None else samsara_home_dir() / "support_bundles"
        self.upload_settings = settings if settings is not None else load_settings()
        self.on_contact, self.on_spoken = on_contact, on_spoken
        self.bundle = None
        self.busy = False
        self.saved_path = None
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
        privacy = QLabel("Review the exact report below. Nothing is sent until you click Send report.")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        layout.addWidget(QLabel("What were you doing? (optional)"))
        self.activity = QLineEdit()
        self.activity.setAccessibleName("What were you doing?")
        layout.addWidget(self.activity)
        # PLACEHOLDER COPY (980b): ordinary labels/status listed in REPORT.md.
        self.contact_label = QLabel("Contact for a reply (optional)")
        layout.addWidget(self.contact_label)
        self.contact = QLineEdit(str(self.config.get("report_upload_contact", ""))[:200])
        self.contact.setMaxLength(200)
        self.contact.setAccessibleName("Contact for a reply (optional)")
        layout.addWidget(self.contact)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setAccessibleName("Exact report text")
        layout.addWidget(self.body, 1)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.consent = QLabel(UPLOAD_CONSENT)
        self.consent.setWordWrap(True)
        layout.addWidget(self.consent)
        primary = QHBoxLayout()
        self.upload_button = QPushButton("Send report")
        self.upload_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.upload_button.setAutoDefault(False)
        self.upload_button.setStyleSheet(f"QPushButton {{ border: 2px solid {theme.ACCENT}; font-weight: 600; }}")
        self.open_folder = QPushButton("Open folder")
        self.open_folder.hide()
        primary.addWidget(self.upload_button)
        primary.addWidget(self.open_folder)
        primary.addStretch()
        layout.addLayout(primary)
        actions = QHBoxLayout()
        self.send = QPushButton("Send to GitHub")
        self.email = QPushButton("Email")
        self.copy = QPushButton("Copy")
        self.save = QPushButton("Save")
        self.cancel = QPushButton("Cancel")
        for button in (self.send, self.email, self.copy, self.save, self.cancel):
            button.setAutoDefault(False)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.activity.textChanged.connect(self._refresh)
        self.contact.textChanged.connect(self._refresh)
        self.contact.editingFinished.connect(self._remember_contact)
        self.cancel.clicked.connect(self.close)
        self.copy.clicked.connect(self._copy)
        self.save.clicked.connect(self._save)
        self.send.clicked.connect(self._send)
        self.email.clicked.connect(self._email)
        self.upload_button.clicked.connect(self.send_report)
        self.upload_finished.connect(self._upload_done)
        self.open_folder.clicked.connect(self._open_folder)
        for widget in (self.upload_button, self.contact, self.contact_label, self.consent):
            widget.setVisible(self.upload_settings is not None)
        self._refresh()
        self.ensurePolished()
        # Keep room for contact/consent and a multi-line failure receipt on small screens.
        # The exact ZIP remains scrollable instead of pushing the action buttons offscreen.
        self.body.setMinimumHeight(96)
        width = min(740, self.screen().availableGeometry().width()-48)
        height = min(self.screen().availableGeometry().height()-48,
                     max(500, self.sizeHint().height(), layout.totalHeightForWidth(width)))
        self.resize(width, height)

    def _refresh(self):
        from .upload import build_bundle
        previous = self.bundle
        try:
            self.bundle = build_bundle(self.report, config=self.config, directory=self.directory / ".review",
                                       activity=self.activity.text(), contact=self.contact.text())
        except (OSError, ValueError):
            self.bundle = None
            self.body.clear()
            self.status.setText("Could not prepare the report. Check the support folder has space.")
        else:
            self.body.setPlainText(self.bundle.preview())
            self.status.clear()
        if previous is not None and previous.path != self.saved_path:
            previous.discard()
        self.upload_button.setEnabled(self.bundle is not None and not self.busy)
        self.upload_button.setText("Send report")
        self.upload_button.setVisible(self.upload_settings is not None)
        self.cancel.setText("Cancel")

    def _remember_contact(self):
        if self.on_contact is not None:
            try:
                self.on_contact(self.contact.text())
            except Exception:
                self.status.setText("Contact could not be remembered; it is still included in this report.")

    def _save(self):
        if self.bundle is None:
            return
        try:
            destination = self.directory / ("report-" + self.bundle.digest + ".zip")
            destination.write_bytes(self.bundle.data)
        except OSError:
            self.status.setText("Could not save the report. Check the support folder has space.")
            return
        self.saved_path = destination.resolve()
        self.open_folder.show()
        self.status.setText("Saved report: " + str(self.saved_path))

    def send_report(self):
        if self.busy or self.bundle is None or self.upload_settings is None or not self.upload_button.isEnabled():
            return False
        from .upload import send_bundle, UploadResult
        self._remember_contact()
        bundle, settings = self.bundle, self.upload_settings
        self.busy = True
        self.upload_button.setEnabled(False)
        self.activity.setEnabled(False)
        self.contact.setEnabled(False)
        self.status.setText("Sending report…")

        def worker():
            try:
                result = send_bundle(bundle, settings, self.directory)
            except Exception:
                result = UploadResult(saved_path=bundle.path, reason="Report could not be sent")
            try:
                self.upload_finished.emit(result)
            except RuntimeError:
                pass  # Closed preview; upload/fallback still completes without another attempt.

        threading.Thread(target=worker, name="problem-report-upload", daemon=True).start()
        return True

    def _upload_done(self, result):
        self.busy = False
        self.activity.setEnabled(True)
        self.contact.setEnabled(True)
        self.saved_path = result.saved_path
        self.open_folder.setVisible(result.saved_path is not None)
        if result.report_id:
            text = "Sent - report " + result.report_id
            self.upload_button.setEnabled(False)
            self.upload_button.hide()
            self.cancel.setText("Close")  # PLACEHOLDER COPY (980b): confirmed delivery is complete.
        else:
            text = result.reason + ". "
            text += ("Saved report: " + str(result.saved_path)) if result.saved_path else "Could not save the report."
            text += " No automatic retry. A timeout may mean it arrived; Retry sends the same report."
            self.upload_button.setText("Retry send")
            self.upload_button.setEnabled(True)
        self.status.setText(text)
        if self.on_spoken is not None:
            try:
                self.on_spoken(text)
            except Exception:
                pass  # Visual receipt remains available if speech is unavailable.

    def _open_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if self.saved_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.saved_path.parent)))

    def _email(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from samsara.support_feedback import BETA_SUPPORT_MAILTO
        self._copy()
        QDesktopServices.openUrl(QUrl(BETA_SUPPORT_MAILTO))
        self.status.setText("Report copied. Paste it into your email.")

    def closeEvent(self, event):
        # A closed window must not delete the worker's last recoverable copy.
        if self.bundle is not None and not self.busy and self.bundle.path != self.saved_path:
            self.bundle.discard()
        super().closeEvent(event)

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
