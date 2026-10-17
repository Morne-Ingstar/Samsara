"""Read-only command/config field with an accessible, focusable copy action."""
from html import escape
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton
from samsara.ui import theme


class CodeSnippet(QFrame):
    def __init__(self, text="", *, name="Code snippet", copy_action=None, parent=None):
        super().__init__(parent)
        self.setObjectName("codeSnippet")
        self.setAccessibleName(name)
        self.copy_action = copy_action
        self._text = text
        self._hovered = False
        self._copied = False
        self.setStyleSheet(f"QFrame#codeSnippet {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; border-radius: 8px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        self.preview = QLabel(text)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.preview.setWordWrap(True)
        self.preview.setMinimumWidth(0)
        # Do not ask Windows for the legacy system FixedFont (often 8514oem).
        # The stylesheet below already selects Consolas for this preview.
        self.preview.setFont(QFont("Consolas"))
        self.preview.setStyleSheet(f"background: transparent; color: {theme.TEXT_PRIMARY}; font-family: Consolas; font-size: {theme.TYPE_BODY}px;")
        # Private previews are deliberately masked. Only Copy supplies the usable
        # command; selecting the mask would put a broken command on the clipboard.
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction if copy_action else
                                            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.preview.setAccessibleName(name)
        row.addWidget(self.preview, 1)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setAccessibleName("Copy " + name)
        self.copy_button.setToolTip("Copy")  # STAND-IN COPY (595)
        self.copy_button.setMinimumSize(theme.HIT_TARGET_MIN, theme.HIT_TARGET_MIN)
        self.copy_button.setAutoDefault(True)
        self.copy_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        icon = QPixmap(24, 24)
        icon.fill(Qt.GlobalColor.transparent)
        painter = QPainter(icon)
        painter.setPen(QPen(theme.qcolor(theme.TEXT_PRIMARY), 1.5))
        painter.drawRoundedRect(4, 3, 12, 14, 2, 2)
        painter.drawRoundedRect(9, 8, 12, 14, 2, 2)
        painter.end()
        self._copy_icon = QIcon(icon)
        self.copy_button.setIcon(self._copy_icon)
        # The review asks for a discoverable action even before hover/focus.
        self.copy_button.installEventFilter(self)
        self.preview.installEventFilter(self)
        row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        self._confirmation = QTimer(self)
        self._confirmation.setSingleShot(True)
        self._confirmation.setInterval(1500)
        self._confirmation.timeout.connect(self._reset_confirmation)
        self.copy_button.clicked.connect(self._copy)
        self._update_copy_visibility()

    def text(self):
        return self._text

    def setText(self, text, *, highlight_lines=()):
        self._text = text
        self.preview.setTextFormat(Qt.TextFormat.RichText if highlight_lines else Qt.TextFormat.PlainText)
        if highlight_lines:
            # Highlight the JSON insertion and its required comma. Copy still
            # supplies the exact plain JSON, without presentation markup.
            lines = []
            for i, line in enumerate(text.splitlines()):
                shown = escape(line).replace(' ', '&nbsp;')
                if i in highlight_lines:
                    shown = f'<span style="background-color: {theme.ACCENT_DIM}; color: {theme.ACCENT};">{shown}</span>'
                lines.append(shown)
            self.preview.setText('<br/>'.join(lines))
        else:
            self.preview.setText(text)

    def _copy(self):
        if self.copy_action is not None:
            if self.copy_action() is False:
                return
        else:
            QApplication.clipboard().setText(self.text())
        self._copied = True
        self.copy_button.setIcon(QIcon())
        self.copy_button.setText("Copied")  # STAND-IN COPY (595)
        self._update_copy_visibility()
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self.copy_button, "Copied"))
        self._confirmation.start()

    def _reset_confirmation(self):
        self._copied = False
        self.copy_button.setText("Copy")
        self.copy_button.setIcon(self._copy_icon)
        self._update_copy_visibility()

    def _update_copy_visibility(self):
        self.copy_button.setVisible(True)

    def enterEvent(self, event):
        self._hovered = True
        self._update_copy_visibility()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._update_copy_visibility()
        super().leaveEvent(event)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self._update_copy_visibility()
        return super().eventFilter(watched, event)
