"""Isolated Step 0 layout proof. Not imported by production Settings.

All copy and displayed values are illustrative. No preferences, devices,
credentials, commands, or application services are read or written.
Construct only on qt_runtime's thread; the external proof runner owns capture.
"""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QComboBox, QCheckBox,
    QSpinBox, QAbstractSpinBox, QHBoxLayout, QVBoxLayout,
    QScrollArea, QMenu, QListView,
)
from samsara.ui import theme


# title, one-line description, control kind, example value, inline action
PAGES = {
    "General": [
        ("Interface size", "Make text and controls comfortable to use.", "combo", "100%", ""),
        ("Theme", "Choose the appearance of Samsara.", "combo", "System", ""),
        ("Microphone", "Choose where Samsara listens.", "combo", "Default microphone", "Refresh"),
        ("Language", "Choose the language you dictate in.", "combo", "English", ""),
        ("Contextual hints", "Show guidance as you learn.", "check", True, "Reset tips"),
    ],
    "Dictation": [
        ("Session button", "Enable hands-free sessions from your shortcut.", "check", True, ""),
        ("Wake activation", "Start a session by saying your wake phrase.", "check", False, ""),
        ("Speech pace", "Allow time for the pauses in your speech.", "combo", "Relaxed", ""),
        ("Cleanup mode", "Choose how dictated text is cleaned up.", "combo", "Standard", ""),
        ("Insertion / output", "Choose where completed dictation goes.", "combo", "Paste into the active app", ""),
        ("Commands while dictating", "Recognise commands during a dictation session.", "check", True, ""),
    ],
    "Audio": [
        ("Output device", "Choose where Samsara plays sound.", "combo", "Default speakers", "Refresh"),
        ("Feedback sounds", "Hear signals for recording and actions.", "check", True, ""),
        ("Sound volume", "Set the volume of feedback sounds.", "number", "70 %", "Test"),
        ("Sound theme", "Choose a set of feedback sounds.", "combo", "Default", "Preview"),
        ("Text-to-speech", "Let Samsara speak responses aloud.", "check", False, ""),
        ("Ava captions", "Show Ava's responses as text.", "check", True, ""),
        ("Mute Ava's voice", "Silence Ava while keeping other speech available.", "check", False, ""),
    ],
    "Ava": [
        ("Ava", "Enable your voice assistant.", "check", False, ""),
        ("Provider route", "Choose where Ava processes requests.", "combo", "Local", "Consent"),
        ("Memory retention", "Choose how long Ava remembers conversations.", "combo", "This session", ""),
        ("Editing", "Allow Ava to edit what you just dictated.", "check", False, ""),
    ],
    "Shortcuts": [
        ("Primary dictation", "Start dictation using your preferred key.", "binding", "Choose key", "Hold"),
        ("Session button", "Start or stop a hands-free session.", "binding", "Choose key", "Toggle"),
        ("Ava command session", "Tap a key or chord to talk to Ava.", "binding", "Choose key", ""),
        ("Ava held key", "Hold a key while speaking to Ava.", "binding", "Choose key", ""),
        ("Cancel", "Cancel the active recording.", "binding", "Choose key", ""),
        ("Undo", "Undo the most recent supported action.", "binding", "Choose key", ""),
        ("Paste staged thought", "Insert the thought waiting in your session.", "binding", "Choose key", ""),
        ("Quick memo", "Capture a short voice memo.", "binding", "Choose key", ""),
        ("Corrections", "Capture a correction; report from that window.", "binding", "Choose key", "Open"),
        ("Complete alarm", "Mark the active alarm as completed.", "binding", "Choose key", ""),
        ("Dismiss alarm", "Dismiss the active alarm without completing it.", "binding", "Choose key", ""),
    ],
    "Help": [
        ("Documentation", "Find commands and usage guidance.", "button", "Open", ""),
        ("Setup / practice", "Find tutorials, microphone setup and Ava guides.", "button", "Choose guide", ""),
        ("Support / problem", "Review diagnostics and get help with a problem.", "button", "Get help", ""),
        ("Private backup / restore", "Keep a private copy of your configuration.", "button", "Manage", ""),
        ("Updates", "Check for a newer version of Samsara.", "button", "Check now", "Daily check: off"),
    ],
}

ADVANCED = {
    "General": [
        ("Start at sign-in", "Launch Samsara when you sign in to Windows.", "check", False, ""),
        ("Start hidden", "Start in the tray with the main window hidden.", "check", False, ""),
        ("Listening indicator", "Show when Samsara is listening.", "check", True, ""),
        ("Indicator position", "Choose the indicator's starting position.", "combo", "Bottom centre", ""),
        ("Floating windows", "Recover an indicator or preview you cannot find.", "button", "Reset windows", ""),
        ("Live surface", "Try the experimental listening display.", "check", False, ""),
        ("Preview fades after", "Delay before the dictation preview fades.", "number", "30 s", ""),
    ],
    "Dictation": [
        ("Recognition", "Choose model and performance details.", "combo", "Automatic", ""),
        ("Command pause", "Wait this long before completing a phrase.", "number", "2 s", ""),
        ("Text formatting", "Adjust spacing, capitals and number formatting.", "button", "Details", ""),
        ("Continuous preset", "Use the existing continuous dictation mode.", "button", "Details", ""),
    ],
    "Audio": [
        ("Spoken notices", "Choose which session notices are spoken.", "combo", "Questions only", ""),
        ("Speech setup", "Choose voice, speed and speech volume.", "button", "Details", ""),
        ("Other apps' audio", "Adjust interruption and volume reduction.", "button", "Details", ""),
        ("Speech during feedback", "Set remaining speech volume during sounds.", "number", "50 %", ""),
    ],
    "Ava": [
        ("Ava personality", "Choose the style of Ava's answers.", "combo", "Relaxed", ""),
        ("Editing timeout", "Limit how long an editing request can take.", "number", "30 s", ""),
        ("Automatic corrections", "Choose where automatic editing is allowed.", "button", "Details", ""),
        ("Performance", "Adjust model, memory and request limits.", "button", "Details", ""),
    ],
    "Shortcuts": [
        ("Additional bindings", "Command-only, wake and continuous shortcuts.", "button", "Details", ""),
    ],
    "Help": [
        ("Support details", "Review a summary, bundle or log before sharing.", "button", "Details", ""),
    ],
}

TOOLS = ("Command library", "Alarms", "Health", "Music", "Components", "Integrations")


def style():
    return f"""
    QWidget {{ color: {theme.TEXT_PRIMARY}; font-family: 'Segoe UI';
               font-size: {theme.TYPE_BODY}px; background: {theme.BG0}; }}
    QFrame#sidebar, QFrame#footer {{ background: {theme.BG1}; }}
    QLabel {{ background: transparent; }}
    QLabel[role='description'] {{ color: {theme.TEXT_SECONDARY}; }}
    QLabel[role='title'] {{ font-size: {theme.TYPE_BODY + 12}px; font-weight: 600; }}
    QLabel[role='label'] {{ font-weight: 600; }}
    QFrame[role='row'] {{ border-bottom: 1px solid {theme.BORDER_FAINT}; }}
    QPushButton, QComboBox, QLineEdit, QSpinBox {{ background: {theme.BG2};
        border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 0 12px; }}
    QComboBox {{ border-color: {theme.BORDER_FAINT}; border-radius: 8px;
        padding: 0 8px 0 12px; }}
    QComboBox:hover {{ border-color: {theme.BORDER}; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right;
        width: 28px; background: transparent; border: none; }}
    QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}
    QComboBox QAbstractItemView {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 4px;
        selection-background-color: {theme.BG2}; selection-color: {theme.TEXT_PRIMARY}; outline: none; }}
    QComboBox QAbstractItemView::item {{ min-height: 44px; padding: 0 12px; border-radius: 5px; }}
    QPushButton:hover {{ border-color: {theme.ACCENT}; }}
    QPushButton:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {theme.ACCENT}; }}
    QPushButton#apply {{ background: {theme.ACCENT};
        color: {theme.TEXT_ON_ACCENT}; border: none; }}
    QPushButton[role='nav'] {{ background: transparent; border: 1px solid transparent;
        text-align: left; padding-left: 14px; }}
    QPushButton[role='nav']:hover {{ background: {theme.HOVER_WASH}; }}
    QPushButton[role='nav'][selected='true'] {{ background: {theme.BG2}; font-weight: 600; }}
    QPushButton[role='nav']:focus {{ border: 1px solid {theme.ACCENT}; }}
    QPushButton#helpButton {{ background: transparent; border: 1px solid transparent;
        border-radius: 22px; padding: 0; color: {theme.TEXT_SECONDARY}; }}
    QPushButton#helpButton:hover {{ background: {theme.HOVER_WASH}; color: {theme.TEXT_PRIMARY}; }}
    QPushButton#helpButton:focus {{ border: 1px solid {theme.ACCENT}; }}
    QCheckBox {{ spacing: 12px; background: transparent; }}
    QCheckBox::indicator {{ width: 24px; height: 24px; border: 1px solid {theme.BORDER};
        border-radius: 4px; background: {theme.BG2}; }}
    QCheckBox::indicator:checked {{ background: {theme.ACCENT}; border-color: {theme.ACCENT}; }}
    QScrollArea {{ border: none; }}
    QMenu {{ background: {theme.BG1}; border: 1px solid {theme.BORDER}; padding: 8px; }}
    QMenu::item {{ padding: 13px 24px; }}
    QMenu::item:selected {{ background: {theme.BG2}; }}
    QFrame#helpPanel {{ background: {theme.BG2}; border: 1px solid {theme.ACCENT}; border-radius: 8px; }}
    """


def label(text, role="description"):
    widget = QLabel(text)
    widget.setProperty("role", role)
    return widget


def button(text):
    widget = QPushButton(text)
    widget.setMinimumSize(44, 44)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


class CleanComboBox(QComboBox):
    """Keep Qt selection/keyboard behavior; replace only the native arrow chrome."""
    def __init__(self):
        super().__init__()
        self.setView(QListView())
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(theme.qcolor(theme.TEXT_SECONDARY), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        x, y = self.width() - 18, self.height() / 2
        chevron = QPainterPath()
        chevron.moveTo(x - 4, y - 2)
        chevron.lineTo(x, y + 2)
        chevron.lineTo(x + 4, y - 2)
        painter.drawPath(chevron)


class HelpButton(QPushButton):
    """In-window help: same content for hover, focus and tap; Escape closes."""
    def __init__(self, title, description, window):
        super().__init__("?")
        self.setObjectName("helpButton")
        self.setFixedSize(44, 44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Help: {title}")
        self.setAccessibleDescription(description)
        self.title, self.description, self.host = title, description, window
        self.clicked.connect(self.reveal)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.TEXT_SECONDARY), 1.2))
        painter.drawEllipse(QRectF(11, 11, 22, 22))

    def reveal(self):
        self.host.show_help(self.title, self.description)

    def enterEvent(self, event):
        self.reveal()
        super().enterEvent(event)

    def focusInEvent(self, event):
        self.reveal()
        super().focusInEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.host.help_panel.hide()
        else:
            super().keyPressEvent(event)


class NumberTarget(QWidget):
    """44px outer target with a centred, single-line numeric editor."""
    def __init__(self, value):
        super().__init__()
        self.setFixedSize(108, 44)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        self.editor = QSpinBox()
        number, unit = value.split(" ")
        self.editor.setRange(0, 999)
        self.editor.setValue(int(number))
        self.editor.setSuffix(" " + unit)
        self.editor.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.editor.setFixedHeight(32)
        self.editor.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.setFocusProxy(self.editor)
        layout.addWidget(self.editor)

    def mousePressEvent(self, event):
        self.editor.setFocus()
        self.editor.selectAll()
        super().mousePressEvent(event)


class SettingsPreview(QWidget):
    def __init__(self, page, expanded=False):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.setWindowTitle("Samsara Settings — layout preview")
        self.setStyleSheet(style())
        self.resize(1160, 1060 if page == "Shortcuts" else 920)
        self.help_buttons = []
        self.controls = []
        self.rows = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(202)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(20, 26, 20, 22)
        nav.addWidget(label("Settings", "label"))
        nav.addSpacing(22)
        for name in PAGES:
            item = button(name)
            item.setProperty("role", "nav")
            item.setProperty("selected", name == page)
            nav.addWidget(item)
        nav.addStretch()
        self.tools_button = button("Tools  ▾")
        nav.addWidget(self.tools_button)
        self.tools_menu = QMenu(self)
        self.tools_menu.setStyleSheet(style())
        for name in TOOLS:
            self.tools_menu.addAction(name)
        # Menu is independently grabbed by the runner; no app actions wired.
        body.addWidget(sidebar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(30, 26, 30, 24)
        column.setSpacing(0)
        column.addWidget(label(page, "title"))
        column.addSpacing(6)
        subtitles = {
            "General": "Make Samsara comfortable for everyday use.",
            "Dictation": "Choose how you start speaking and deliver text.",
            "Audio": "Manage sounds, speech and captions in one place.",
            "Ava": "Choose how Ava helps, with permissions you control.",
            "Shortcuts": "Keep your essential actions within reach.",
            "Help": "Set up, practise and recover when you need to.",
        }
        column.addWidget(label(subtitles[page]))
        column.addSpacing(22)
        for spec in PAGES[page]:
            column.addWidget(self.make_row(spec))
        column.addSpacing(14)
        self.disclosure = button("Advanced  ▾" if expanded else "Advanced  ▸")
        self.disclosure.setAccessibleName(f"{page} advanced settings")
        self.disclosure.setCheckable(True)
        self.disclosure.setChecked(expanded)
        column.addWidget(self.disclosure, alignment=Qt.AlignmentFlag.AlignLeft)
        self.advanced = QWidget()
        details = QVBoxLayout(self.advanced)
        details.setContentsMargins(0, 8, 0, 0)
        details.setSpacing(0)
        for spec in ADVANCED[page]:
            details.addWidget(self.make_row(spec))
        column.addWidget(self.advanced)
        self.advanced.setVisible(expanded)
        self.disclosure.toggled.connect(self.advanced.setVisible)
        self.disclosure.toggled.connect(lambda opened: self.disclosure.setText("Advanced  ▾" if opened else "Advanced  ▸"))
        column.addStretch()
        scroll.setWidget(content)
        body.addWidget(scroll)
        outer.addLayout(body, 1)
        footer = QFrame()
        footer.setObjectName("footer")
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(24, 14, 24, 14)
        bottom.addWidget(label("Layout preview · example values · placeholder copy"))
        bottom.addStretch()
        bottom.addWidget(button("Cancel"))
        apply = button("Apply && Close")
        apply.setObjectName("apply")
        bottom.addWidget(apply)
        outer.addWidget(footer)
        self.help_panel = QFrame(self)
        self.help_panel.setObjectName("helpPanel")
        self.help_panel.setFixedWidth(450)
        help_layout = QVBoxLayout(self.help_panel)
        help_layout.setContentsMargins(18, 14, 18, 14)
        self.help_title = label("", "label")
        self.help_text = label("")
        self.help_text.setWordWrap(True)
        help_layout.addWidget(self.help_title)
        help_layout.addWidget(self.help_text)
        close_help = button("Close help")
        close_help.clicked.connect(self.help_panel.hide)
        help_layout.addWidget(close_help, alignment=Qt.AlignmentFlag.AlignRight)
        self.help_panel.hide()
        if expanded:
            self.resize(1160, 1240 if page == "General" else 1060)

    def make_row(self, spec):
        title, description, kind, value, action = spec
        row = QFrame()
        row.setProperty("role", "row")
        row.setFixedHeight(70)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)
        text_column = QVBoxLayout()
        text_column.setSpacing(3)
        text_column.addWidget(label(title, "label"))
        text_column.addWidget(label(description))
        layout.addLayout(text_column, 1)
        full = description + " Changes take effect when you choose Apply & Close."
        if title == "Live surface":
            full = "An experimental display for listening and dictation. This is an explicit opt-in and remains off by default. The existing indicator and preview remain available."
        elif title == "Start hidden":
            full = "Samsara starts in the Windows tray. Open its tray menu to return to Settings or the main window. Start at sign-in is a separate choice."
        elif title == "Preview fades after":
            full = "The delay before an idle dictation preview fades. This adjusts the existing preview display; it does not change how long Samsara listens."
        helper = HelpButton(title, full, self)
        self.help_buttons.append(helper)
        layout.addWidget(helper)
        value_box = QWidget()
        value_box.setFixedWidth(312)
        value_layout = QHBoxLayout(value_box)
        value_layout.setContentsMargins(0, 0, 0, 0)
        value_layout.setSpacing(8)
        if kind == "combo":
            control = CleanComboBox()
            control.setFont(theme.qfont(theme.TYPE_BODY))
            control.addItem(value)
            if title == "Theme":
                control.addItems(["Light", "Dark"])
            control.setFixedHeight(44)
            control.setFixedWidth(min(312, max(116, control.fontMetrics().horizontalAdvance(value) + 54)))
        elif kind == "check":
            control = QCheckBox("On" if value else "Off")
            control.setChecked(value)
            control.setMinimumSize(80, 44)
        elif kind == "number":
            control = NumberTarget(value)
        else:
            control = button(value)
        control.setAccessibleName(title)
        control.setAccessibleDescription(full)
        if isinstance(control, NumberTarget):
            control.editor.setAccessibleName(title)
            control.editor.setAccessibleDescription(full)
        self.controls.append(control)
        self.rows.append(row)
        value_layout.addWidget(control)
        if action:
            extra = button(action)
            extra.setAccessibleName(f"{title}: {action}")
            value_layout.addWidget(extra)
        value_layout.addStretch()
        layout.addWidget(value_box)
        return row

    def show_help(self, title, description):
        self.help_title.setText(title)
        self.help_text.setText(description)
        self.help_panel.adjustSize()
        self.help_panel.move(self.width() - self.help_panel.width() - 30, 105)
        self.help_panel.show()
        self.help_panel.raise_()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
