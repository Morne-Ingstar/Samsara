"""Ava's informed-consent dialog and its small, UI-independent state helpers."""

from __future__ import annotations

from samsara.ai_preferences import accept_consent, consent_covers, provider_for

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from samsara.ui import theme


CONSENT_VERSION = 2

# OWNER COPY — REVIEW
CONSENT_TEXT = """Turning Ava on

Ava is an AI. When you ask her to do something, she can run commands on this computer — open apps, type,
press keys, change settings — the same way you can by voice.

She can get things wrong. Samsara can undo the last thing she typed, but not everything she does can be undone.

If you use a cloud AI, what you say to Ava is sent to that provider on your own account and your own key.
Local AI never leaves this computer.

You're in charge. Turn Ava off any time."""


def consent_required(config: dict | None, *, cloud_enabled: bool = False,
                     features=("ava",), provider=None) -> bool:
    """Whether the current facts require the user to acknowledge Ava again."""
    ava = (config or {}).get("ava", {})
    consent = ava.get("consent", {}) if isinstance(ava, dict) else {}
    try:
        destination = provider or provider_for(config or {}, "cloud" if cloud_enabled else "local")
    except (ValueError, TypeError):
        return True
    return not consent_covers(consent, features=features, provider=destination,
                              cloud=cloud_enabled, version=CONSENT_VERSION)


def accepted_consent(
    config: dict | None,
    *,
    cloud_enabled: bool = False,
    accepted_at: str | None = None,
    features=("ava",),
    provider=None,
) -> dict:
    """Return the persisted consent record after an explicit acknowledgement."""
    destination = provider or provider_for(config or {}, "cloud" if cloud_enabled else "local")
    return accept_consent(config or {}, features=features, provider=destination,
                          cloud=cloud_enabled, accepted_at=accepted_at, version=CONSENT_VERSION)


class AvaConsentDialog(QDialog):
    """A modal acknowledgement, or a read-only replay of the same copy."""

    def __init__(self, parent=None, *, read_only: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Turning Ava on")
        self.setAccessibleName("Ava consent")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(
            f"QDialog {{ background: {theme.BG0}; }}"
            f"QLabel {{ color: {theme.TEXT_PRIMARY}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        text = QLabel(CONSENT_TEXT)
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setAccessibleName("What turning Ava on means")
        text.setStyleSheet(f"font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(text)

        buttons = QHBoxLayout()
        buttons.addStretch()
        if read_only:
            close = QPushButton("Close")
            close.setAccessibleName("Close Ava consent")
            theme.make_secondary(close)
            close.clicked.connect(self.reject)
            buttons.addWidget(close)
            close.setFocus(Qt.FocusReason.OtherFocusReason)
            self._initial_focus = close
        else:
            not_now = QPushButton("Not now")
            not_now.setAccessibleName("Not now")
            theme.make_secondary(not_now)
            not_now.clicked.connect(self.reject)
            buttons.addWidget(not_now)

            accept = QPushButton("I understand, turn Ava on")
            accept.setAccessibleName("I understand, turn Ava on")
            theme.make_primary(accept)
            accept.clicked.connect(self.accept)
            buttons.addWidget(accept)
            not_now.setFocus(Qt.FocusReason.OtherFocusReason)
            self._initial_focus = not_now
        layout.addLayout(buttons)

    def showEvent(self, event):
        super().showEvent(event)
        self._initial_focus.setFocus(Qt.FocusReason.OtherFocusReason)


def request_consent(parent, config: dict | None, *, cloud_enabled: bool = False) -> dict | None:
    """Show the acknowledgement when needed; return its record only on accept."""
    if not consent_required(config, cloud_enabled=cloud_enabled):
        ava = (config or {}).get("ava", {})
        return dict(ava["consent"])
    dialog = AvaConsentDialog(parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return accepted_consent(config, cloud_enabled=cloud_enabled)


def show_consent_copy(parent) -> None:
    """Replay the explanation without changing consent or an Ava toggle."""
    AvaConsentDialog(parent, read_only=True).exec()
