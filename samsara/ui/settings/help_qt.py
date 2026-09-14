"""Help & Support page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
HelpPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from samsara.support_feedback import (
    BETA_SUPPORT_EMAIL,
    BUG_REPORT_URL,
    DOCUMENTATION_URL,
    build_safe_diagnostic_summary,
)
from samsara.ui import theme

from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, logger


class HelpPage:
    """Methods of the Help & Support settings page (moved from _SettingsWindow)."""

    def _open_support_url(self, url: str, status_label: QLabel) -> None:
        """Open a user-requested support destination and show any failure."""
        try:
            opened = bool(QDesktopServices.openUrl(QUrl(url)))
        except Exception as exc:
            logger.exception("[SUPPORT] Could not open feedback URL: %s", exc)
            opened = False
        status_label.setText(
            "Opened in your browser."
            if opened else
            "Could not open the link. Visit morneis.com/samsara/support."
        )

    def _open_live_log_for_support(self, status_label: QLabel) -> None:
        opener = getattr(self.app, "open_log_viewer", None)
        if not callable(opener):
            status_label.setText("Live Log is unavailable in this session.")
            return
        try:
            opener()
        except Exception as exc:
            logger.exception("[SUPPORT] Could not open Live Log: %s", exc)
            status_label.setText("Could not open Live Log. See the Samsara log folder.")
            return
        status_label.setText("Live Log opened. Review and redact before sharing.")

    def _copy_safe_diagnostics(self, status_label: QLabel) -> None:
        try:
            summary = build_safe_diagnostic_summary(self.app.config)
            QApplication.clipboard().setText(summary)
        except Exception as exc:
            logger.exception("[SUPPORT] Could not copy diagnostics: %s", exc)
            status_label.setText("Could not copy the diagnostic summary.")
            return
        status_label.setText("Safe diagnostic summary copied — no logs or secrets included.")

    def _open_update_dialog(self) -> None:
        """Open the packaged-app updater after an explicit user action."""
        from samsara.ui.update_qt import show_update_dialog

        show_update_dialog(self.app, check_immediately=True)

    def _build_support_tab(self):
        """Build a discoverable, honest support surface for beta users.

        This deliberately collects learning, direct beta contact, diagnostics,
        feedback, and update controls in one sidebar destination rather than
        interrupting configuration work on the General page.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )

        container = QWidget()
        container.setMaximumWidth(_CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        layout.addWidget(self._section_title("Help & learning"))
        intro = QLabel(
            "Start with the documentation or the tutorial. Samsara is one developer "
            "— beta email works, replies aren't instant."
        )
        intro.setObjectName("supportIntroLabel")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #AEB4C0; font-size: 13px;")
        layout.addWidget(intro)

        docs_btn = QPushButton("Open documentation")
        docs_btn.setObjectName("openDocumentationButton")
        docs_btn.setAccessibleName("Open Samsara documentation")
        docs_btn.clicked.connect(
            lambda: self._open_support_url(DOCUMENTATION_URL, support_status)
        )
        layout.addLayout(self._setting_row(
            "Documentation",
            "Quick start, voice-command reference, wake-word guide, and custom commands.",
            docs_btn,
        ))

        tutorial_btn = QPushButton("Replay tutorial")
        tutorial_btn.setObjectName("replayTutorialButton")
        tutorial_btn.setAccessibleName("Replay interactive Samsara tutorial")
        tutorial_btn.clicked.connect(
            lambda: getattr(self.app, 'show_tutorial', lambda: None)()
        )
        layout.addLayout(self._setting_row(
            "Interactive tutorial",
            "Practice dictation, commands, Show Numbers, and Ava inside Samsara.",
            tutorial_btn,
        ))

        mic_guide_btn = QPushButton("Open mic setup")
        mic_guide_btn.setObjectName("openMicSetupGuideButton")
        mic_guide_btn.clicked.connect(
            lambda: getattr(self.app, 'open_mic_setup_guide', lambda: None)()
        )
        layout.addLayout(self._setting_row(
            "Microphone setup guide",
            "Choose and test a microphone, then tune wake-word detection for it.",
            mic_guide_btn,
        ))

        ava_guide_btn = QPushButton("Open Ava setup")
        ava_guide_btn.setObjectName("openAvaSetupGuideButton")
        ava_guide_btn.clicked.connect(
            lambda: getattr(self.app, 'open_ava_guide', lambda: None)()
        )
        layout.addLayout(self._setting_row(
            "Ava setup guide",
            "Set up Samsara's optional local assistant.",
            ava_guide_btn,
        ))
        layout.addSpacing(20)

        layout.addWidget(self._section_title("Beta support & feedback"))
        contact = QLabel(
            "Testers: email is fine. GitHub is for people who already have an account."
        )
        contact.setObjectName("supportContactIntroLabel")
        contact.setWordWrap(True)
        contact.setStyleSheet("color: #AEB4C0; font-size: 13px;")
        layout.addWidget(contact)

        support_status = QLabel("")
        support_status.setObjectName("feedbackStatusLabel")
        support_status.setWordWrap(True)
        support_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        support_status.setStyleSheet("color: #AEB4C0; font-size: 12px;")

        # 1. Email -- first, because most testers have no GitHub account.
        beta_btn = QPushButton("Email the developer")
        beta_btn.setObjectName("betaFeedbackButton")
        beta_btn.setAccessibleName("Email the Samsara developer for beta support")
        beta_btn.clicked.connect(
            lambda: self._email_developer(support_status)
        )
        address = QLabel(BETA_SUPPORT_EMAIL)
        address.setObjectName("betaSupportAddressLabel")
        address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        address.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: 12px;")
        email_control = QWidget()
        email_row = QHBoxLayout(email_control)
        email_row.setContentsMargins(0, 0, 0, 0)
        email_row.setSpacing(10)
        email_row.addWidget(beta_btn)
        email_row.addWidget(address)
        layout.addLayout(self._setting_row(
            "Contact the developer",
            "Copies a ready-to-send email with safe diagnostics, then tries your "
            "mail app. Replies are personal and may take time.",
            email_control,
        ))

        # 2. Safe diagnostics
        diagnostics_btn = QPushButton("Copy safe diagnostics")
        diagnostics_btn.setObjectName("copyDiagnosticButton")
        diagnostics_btn.setAccessibleName("Copy safe Samsara diagnostic summary")
        diagnostics_btn.clicked.connect(
            lambda: self._copy_safe_diagnostics(support_status)
        )
        layout.addLayout(self._setting_row(
            "Safe diagnostic summary",
            "Copy useful runtime facts without logs, dictated text, file paths, or keys.",
            diagnostics_btn,
        ))

        # 3. GitHub
        report_btn = QPushButton("Report a problem")
        report_btn.setObjectName("reportBugButton")
        report_btn.setAccessibleName("Report a Samsara problem on GitHub")
        report_btn.clicked.connect(
            lambda: self._open_support_url(BUG_REPORT_URL, support_status)
        )
        layout.addLayout(self._setting_row(
            "Report a reproducible problem (GitHub)",
            "Open a public GitHub report. Include expected behavior, what happened, and steps.",
            report_btn,
        ))

        # 4. Live log, with its redaction warning
        live_log_btn = QPushButton("Open live log")
        live_log_btn.setObjectName("openLiveLogButton")
        live_log_btn.setAccessibleName("Open Samsara live log")
        live_log_btn.clicked.connect(
            lambda: self._open_live_log_for_support(support_status)
        )
        layout.addLayout(self._setting_row(
            "Live log",
            "The log can contain dictated text and file paths. Review it and share only "
            "the relevant, redacted lines.",
            live_log_btn,
        ))
        layout.addWidget(support_status)
        layout.addSpacing(20)

        layout.addWidget(self._section_title("Updates"))
        check_updates_btn = QPushButton("Check for updates")
        check_updates_btn.setObjectName("checkForUpdatesButton")
        check_updates_btn.setAccessibleName("Check for Samsara updates")
        check_updates_btn.clicked.connect(self._open_update_dialog)
        layout.addLayout(self._setting_row(
            "Check for updates",
            "No update server. Checking contacts GitHub Releases only when you press this; "
            "GitHub sees your IP.",
            check_updates_btn,
        ))

        automatic_updates = QCheckBox()
        automatic_updates.setObjectName("automaticUpdateChecksCheckbox")
        current_update_settings = self.app.config.get("updates", {})
        if not isinstance(current_update_settings, dict):
            current_update_settings = {}
        automatic_updates.setChecked(bool(
            current_update_settings.get("automatic_checks", False)
        ))
        self._widgets["automatic_update_checks"] = automatic_updates
        layout.addLayout(self._setting_row(
            "Automatically check GitHub once a day",
            "Off by default. Checks GitHub at most once a day. GitHub sees your IP — Samsara "
            "sends no audio, text, settings, or identifiers.",
            automatic_updates,
        ))

        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _email_developer(self, status_label: QLabel) -> None:
        """Copy the whole support email, then TRY the mail app.

        A mailto: with no registered mail client is handed to the browser
        and goes nowhere while openUrl still reports success, so the
        clipboard copy comes first and the status never claims anything
        opened.
        """
        from samsara.support_feedback import (
            build_support_email_text,
            build_support_mailto,
        )

        copied = False
        try:
            QApplication.clipboard().setText(build_support_email_text(self.app.config))
            copied = True
        except Exception as exc:
            logger.exception("[SUPPORT] Could not copy the support email: %s", exc)
        try:
            QDesktopServices.openUrl(QUrl(build_support_mailto(self.app.config)))
        except Exception as exc:
            logger.exception("[SUPPORT] Could not hand off mailto: %s", exc)
        if copied:
            status_label.setText(
                "Message copied to your clipboard. If your mail app didn't open, "
                f"paste it into any email to {BETA_SUPPORT_EMAIL}."
            )
        else:
            status_label.setText(
                "Could not copy the message. Write to "
                f"{BETA_SUPPORT_EMAIL} and use Copy safe diagnostics below."
            )
