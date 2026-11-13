"""1014: release notes are opened deliberately, never a Home content card."""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout

from samsara.ui import theme


# Owner-supplied corrections for the in-app summaries, including the next release.
RELEASE_SUMMARIES = {
    "0.40.3": "0.40.3 - a more forgiving microphone setup, and a safer uninstall.",
    "0.40.4": "0.40.4 - faster start, simpler setup, a better microphone picker, and a cleaner install and uninstall.",
    "0.41.0": "0.41.0 - steadier dictation, a Stats page on Home, saved window layouts, clearer messages, and a tidier Settings.",
    "0.41.2": "0.41.2 - dictation that is never thrown away, cleaner punctuation, a live microphone check, and fewer false warnings.",
    "0.42.0": "0.42.0 - steadier dictation, easier hands-free, clearer messages, and more voice commands.",
    "0.42.1": "0.42.1 - steadier dictation that keeps every word, easier hands-free, clearer messages, and more voice commands.",
    "0.42.2": "0.42.2 - cleaner dictation that keeps your words and joins your sentences, easier hands-free, and more voice commands.",
}


class WhatsNewButton(QPushButton):
    def __init__(self, home):
        super().__init__("What's new", home)
        self.setObjectName("homeWhatsNewButton")
        self.setAccessibleName("What's new")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.ACCENT};"
            f" border: 1px solid transparent; border-radius: 6px; padding: 0 8px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ background: {theme.BG2}; }}"
            f"QPushButton:focus {{ border-color: {theme.ACCENT}; }}")
        self.home = home
        self.dialog = None
        self.clicked.connect(self.open_notes)

    def refresh(self):
        unseen = self.home._due_whats_new() is not None
        if self.property("unread") != unseen:
            self.setProperty("unread", unseen)
            dot = QPixmap(8, 8)
            dot.fill(Qt.GlobalColor.transparent)
            painter = QPainter(dot)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            if unseen:
                painter.drawEllipse(1, 1, 6, 6)
            painter.end()
            self.setIcon(QIcon(dot))
            self.setIconSize(QSize(8, 8))
        self.setAccessibleDescription("Unread release notes" if unseen else "Release notes")

    def open_notes(self):
        from samsara.ui import home_qt
        if self.dialog is not None:
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        note = home_qt.whats_new_note(None, current=home_qt.__version__)
        version, sentence = note or (home_qt.__version__, "")
        dialog = QDialog(self.home.window())
        dialog.setObjectName("homeReleaseNotesDialog")
        dialog.setWindowTitle("What's new")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(16)
        heading = QLabel(f"Samsara {version}")
        heading.setStyleSheet(f"font-size: {theme.TYPE_BODY}px; font-weight: 600; color: {theme.TEXT_PRIMARY};")
        layout.addWidget(heading)
        body = QLabel(sentence)
        body.setObjectName("releaseSummary")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setVisible(bool(sentence))
        layout.addWidget(body)
        actions = QHBoxLayout()
        full = QPushButton(home_qt.CHANGELOG_LINK)
        theme.make_secondary(full)
        full.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        full.clicked.connect(lambda: home_qt.open_url(home_qt.CHANGELOG_URL))
        actions.addWidget(full)
        actions.addStretch()
        close = QPushButton("Close")
        theme.make_secondary(close)
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        # Fit the notes to the host even on a small desktop at 125% scaling.
        dialog.resize(min(520, self.home.window().width() - 48), dialog.sizeHint().height())
        self.dialog = dialog
        dialog.destroyed.connect(self._closed)
        dialog.show()
        # Opening, rather than dismissing a Home card, is the seen boundary.
        if note is not None:
            self.home._settings.setValue(home_qt.WHATS_NEW_SEEN_KEY, version)
        self.refresh()

    def _closed(self, *args):
        self.dialog = None
