"""Small, direct community actions shared by the Settings Help page."""

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget


DISCORD_URL = "https://discord.gg/tPYvf369hQ"
EMAIL_URL = "mailto:me@morneis.com"


def community_actions(parent=None):
    """Return keyboard accessible buttons that open the OS URL handlers."""
    actions = QWidget(parent)
    row = QHBoxLayout(actions)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    for label, name, accessible_name, url in (
        ("Discord", "communityDiscordButton", "Open the Samsara Discord community", DISCORD_URL),
        ("Email", "communityEmailButton", "Email me@morneis.com", EMAIL_URL),
    ):
        button = QPushButton(label, actions)
        button.setObjectName(name)
        button.setAccessibleName(accessible_name)
        button.clicked.connect(lambda _checked=False, target=url: QDesktopServices.openUrl(QUrl(target)))
        row.addWidget(button)
    row.addStretch()
    return actions
