"""A content-height help notice shared by one Settings visit's lazy pages."""
from PySide6.QtCore import QObject, Signal, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton
from samsara import __version__
from samsara.ui import theme

DISCORD_INVITE_URL = "https://discord.gg/tPYvf369hQ"
DISCORD_USERNAME = "Morne"
NOTICE_TEXT = ("Beta testers: if you're having trouble with setup or configuration, or just "
               "have questions, feel free to contact me via Discord or email.")


class _Session(QObject):
    dismissed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.hidden = False

    def dismiss(self):
        self.hidden = True
        self.dismissed.emit()


class BetaNotice(QFrame):
    def __init__(self, parent=None, *, session=None, version=None):
        super().__init__(parent)
        self.setObjectName("betaNotice")
        self.session = session if session is not None else _Session(self)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setStyleSheet(f"QFrame#betaNotice {{ background: {theme.ACCENT_DIM}; border: 1px solid {theme.ACCENT}; border-radius: 6px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 4, 3)
        row.setSpacing(4)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setMinimumWidth(0)
        self.message.setTextFormat(Qt.TextFormat.RichText)
        text = NOTICE_TEXT.replace("Discord", self._anchor("Discord", DISCORD_INVITE_URL, DISCORD_USERNAME))
        text = text.replace("email.", self._anchor("email", "mailto:me@morneis.com") + ".")
        self.message.setText(text)
        self.message.setAccessibleName(NOTICE_TEXT)
        self.message.setStyleSheet(
            f"QLabel {{ background: transparent; color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_MIN}px; border: 1px solid transparent; text-decoration: none; }}"
            f"QLabel:focus {{ border-color: {theme.ACCENT}; text-decoration: none; }}")
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self.message.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.message.linkActivated.connect(self._open_link)
        self.message.linkHovered.connect(lambda url: self.message.setCursor(
            Qt.CursorShape.PointingHandCursor if url else Qt.CursorShape.ArrowCursor))
        row.addWidget(self.message, 1)
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setAccessibleName("Dismiss beta help for this session")
        self.close_button.setToolTip(self.close_button.accessibleName())
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.close_button.setStyleSheet(
            f"QToolButton {{ background: transparent; color: {theme.TEXT_SECONDARY}; border: 1px solid transparent; padding: 0 3px; }}"
            f"QToolButton:hover, QToolButton:focus {{ border-color: {theme.ACCENT}; }}")
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.close_button.clicked.connect(self.session.dismiss)
        self.session.dismissed.connect(self.hide)
        self.fit_to_text()
        self.setVisible("beta" in (version if version is not None else __version__).lower() and not self.session.hidden)

    def fit_to_text(self):
        # The owner's thin notice takes precedence over the shared 44px
        # Settings button minimum. The frame itself has no fixed height.
        self.message.ensurePolished()
        self.close_button.setFixedSize(22, self.message.fontMetrics().height() + 2)

    @staticmethod
    def _anchor(label, url, title=""):
        return f'<a href="{url}" title="{title}" style="color: {theme.ACCENT}; text-decoration: none;">{label}</a>'

    @staticmethod
    def _open_link(url):
        if url in (DISCORD_INVITE_URL, "mailto:me@morneis.com"):
            QDesktopServices.openUrl(QUrl(url))
