"""Settings sidebar: heading, search and page list on one surface (queue 360).

Matches the owner's reference sidebar (ui_proof/360/reference) - one flat
surface, a pill search field inside it, outline icons, an inset rounded pill
for the selected row - while keeping the accessibility floor: 44 px rows and
search, body-size text, a keyboard focus ring, and a heavier selected label.

Colours are read from theme when painting or rebound by theme's runtime
restyle, never baked in at import, so a dark <-> light flip reaches the panel.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontDatabase, QPainter, QPen, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QLabel, QLineEdit, QListWidget, QStyle,
    QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget,
)

from samsara.ui import theme

#: The panel's fixed width; the reference is 268 at 14 px text, ours carries 18 px rows.
SIDEBAR_WIDTH = 240
#: Gap above and below the pill inside a row: 44 px row -> 40 px pill (reference: 30 of 31).
PILL_GAP = 2
#: Corner radius of the selected-row pill (reference: about a third of the pill height).
PILL_RADIUS = 10
#: Icon (and search magnifier) inset from the pill's left edge (reference: 8 at 14 px text).
ICON_INSET = 12
#: Square box the icon glyph is drawn in (reference: 16 px icon beside 14 px text, ~1.15x).
ICON_BOX = 20
#: Where every label starts, measured from the pill's left edge: inset + icon + gap.
LABEL_X = ICON_INSET + ICON_BOX + ICON_INSET
#: Width of the keyboard focus ring on the search field and the current row.
FOCUS_RING = 2
#: QLineEdit's own fixed horizontal text margin (QLineEditPrivate::horizontalMargin).
_LINE_EDIT_MARGIN = 2

#: Windows' system icon font; MDL2 is the Windows 10 face with the same codepoints.
ICON_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
#: Page title -> glyph. Checked by eye in ui_proof/360/glyphs.png.
PAGE_GLYPHS = {
    "General": "",    # Settings (gear)
    "Dictation": "",  # Microphone
    "Audio": "",      # Volume (speaker)
    "Ava": "",        # Robot
    "Shortcuts": "",  # Keyboard
    "Help": "",       # Help (question mark)
}
SEARCH_GLYPH = ""     # Search (magnifier)

_icon_family_cache: list[str | None] = []


def icon_family() -> str | None:
    """The first installed icon font, or None (then no icon is drawn)."""
    if not _icon_family_cache:
        installed = set(QFontDatabase.families())
        _icon_family_cache.append(next((f for f in ICON_FAMILIES if f in installed), None))
    return _icon_family_cache[0]


def _icon_font() -> QFont | None:
    family = icon_family()
    if family is None:
        return None
    # The glyph is drawn at the heading token, which fills the ICON_BOX square.
    return theme.qfont(theme.TYPE_HEADING, family=family)


def label_colour():
    return theme.qcolor(theme.TEXT_PRIMARY)


def placeholder_colour_token() -> str:
    """TEXT_SECONDARY when it clears 4.5:1 on both the panel and the field, else TEXT_PRIMARY."""
    ok = all(theme.contrast_ratio(theme.TEXT_SECONDARY, bg) >= 4.5 for bg in (theme.BG1, theme.BG2))
    return theme.TEXT_SECONDARY if ok else theme.TEXT_PRIMARY


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

class SidebarDelegate(QStyledItemDelegate):
    """Paints one row: an inset pill, an icon column and a left-aligned label."""

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), theme.HIT_TARGET_MIN)

    @staticmethod
    def pill_rect(row_rect: QRect) -> QRect:
        return row_rect.adjusted(0, PILL_GAP, 0, -PILL_GAP)

    @staticmethod
    def icon_rect(row_rect: QRect) -> QRect:
        pill = SidebarDelegate.pill_rect(row_rect)
        top = pill.top() + (pill.height() - ICON_BOX) // 2
        return QRect(pill.left() + ICON_INSET, top, ICON_BOX, ICON_BOX)

    @staticmethod
    def label_rect(row_rect: QRect) -> QRect:
        pill = SidebarDelegate.pill_rect(row_rect)
        return QRect(pill.left() + LABEL_X, pill.top(), max(0, pill.width() - LABEL_X - ICON_INSET), pill.height())

    @staticmethod
    def label_font(selected: bool) -> QFont:
        weight = QFont.Weight.DemiBold if selected else QFont.Weight.Normal
        return theme.qfont(theme.TYPE_NAV, weight=weight)

    def paint(self, painter: QPainter, option, index):
        if index.data(Qt.ItemDataRole.UserRole) == 'heading':
            painter.save()
            painter.setPen(label_colour())
            painter.setFont(theme.qfont(theme.TYPE_BODY, weight=QFont.Weight.DemiBold))
            painter.drawText(option.rect.adjusted(ICON_INSET, 0, 0, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             index.data(Qt.ItemDataRole.DisplayRole))
            painter.restore()
            return
        view = option.widget
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        pill = self.pill_rect(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected or hovered:
            fill = theme.qcolor(theme.BG2)
            if not selected:
                fill.setAlphaF(fill.alphaF() / 2)
            painter.setBrush(fill)
            painter.drawRoundedRect(pill, PILL_RADIUS, PILL_RADIUS)
        if view is not None and keyboard_focus_visible(view) and index == view.currentIndex():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.ACCENT), FOCUS_RING))
            half = FOCUS_RING // 2
            painter.drawRoundedRect(pill.adjusted(half, half, -half, -half), PILL_RADIUS - half, PILL_RADIUS - half)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.setPen(label_colour())
        glyph = PAGE_GLYPHS.get(text)
        icon_font = _icon_font()
        if glyph and icon_font is not None:
            painter.setFont(icon_font)
            painter.drawText(self.icon_rect(option.rect), Qt.AlignmentFlag.AlignCenter, glyph)
        font = self.label_font(selected)
        painter.setFont(font)
        rect = self.label_rect(option.rect)
        elided = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        painter.restore()


def keyboard_focus_visible(view) -> bool:
    """True while the list has focus that arrived by keyboard.

    Qt marks the window WA_KeyboardFocusChange when focus arrives by Tab,
    Shift+Tab or a shortcut and clears it on a mouse click: the ring follows the
    keyboard, never a click. No event filter is needed (none outlives the window
    at interpreter exit, which crashed the test process).
    """
    window = view.window()
    return view.hasFocus() and window is not None and window.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)


# ---------------------------------------------------------------------------
# Search field
# ---------------------------------------------------------------------------

class SearchGlyph(QWidget):
    """The magnifier at the search field's left, aligned with the row icons. Decorative."""

    def __init__(self, edit: QLineEdit):
        super().__init__(edit)
        self.setObjectName("settingsSearchGlyph")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("")
        # The field's height is fixed at the hit target, so one placement holds.
        self.setGeometry(ICON_INSET, 0, ICON_BOX, theme.HIT_TARGET_MIN)
        self._placing_clear = False
        edit.installEventFilter(self)
        self._clear_button = next(iter(edit.findChildren(QToolButton)), None)
        if self._clear_button is not None:
            self._clear_button.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            keyboard = event.type() == QEvent.Type.FocusIn and event.reason() in (
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
            watched.setProperty('settingsKeyboardFocus', keyboard)
            watched.setStyleSheet(search_qss())
        # Settings raises every button to a 44-DIP hit target. Qt's internal
        # clear-button placement assumes its smaller native size instead.
        if watched is self._clear_button and not self._placing_clear and event.type() in (
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
        ):
            self._placing_clear = True
            edit = self.parentWidget()
            size = theme.HIT_TARGET_MIN
            watched.setGeometry(edit.width() - size - FOCUS_RING, 0, size, edit.height())
            self._placing_clear = False
        return super().eventFilter(watched, event)

    def paintEvent(self, _event):
        font = _icon_font()
        if font is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(font)
        painter.setPen(theme.qcolor(placeholder_colour_token()))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, SEARCH_GLYPH)
        painter.end()


def search_qss() -> str:
    height = theme.HIT_TARGET_MIN
    return (
        f"QLineEdit#settingsSearchField {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};"
        f" border: 1px solid {theme.BORDER}; border-radius: {theme.CONTROL_RADIUS}px;"
        f" padding: 0 {ICON_INSET + FOCUS_RING - 1}px; margin: 0;"
        f" min-height: {height - 2}px; max-height: {height - 2}px;"
        f" font-size: {theme.TYPE_BODY}px; font-weight: {theme.FONT_WEIGHT_MIN};"
        f" placeholder-text-color: {placeholder_colour_token()}; }}"
        f" QLineEdit#settingsSearchField[settingsKeyboardFocus='true']:focus {{ border: {FOCUS_RING}px solid {theme.ACCENT};"
        f" padding: 0 {ICON_INSET}px; min-height: {height - 2 * FOCUS_RING}px;"
        f" max-height: {height - 2 * FOCUS_RING}px; }}"
    )


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

def panel_qss() -> str:
    return (
        f"QWidget#settingsSidebar {{ background: {theme.BG1}; }}"
        f" QWidget#settingsSidebar QWidget {{ background: transparent; }}"
        # The list is opaque in the panel's own colour, so it reads as the same surface
        # and still renders correctly when grabbed on its own.
        f" QWidget#settingsSidebar QListWidget {{ background: {theme.BG1}; border: none; outline: none; }}"
    )


def heading_qss() -> str:
    return (
        f"QLabel {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_HEADING}px;"
        f" font-weight: {theme.WEIGHT_SCALE['semibold']}; padding-left: {ICON_INSET}px; background: transparent; }}"
    )


def build_panel(heading: str, search: QLineEdit | None, pages: QListWidget) -> QWidget:
    """Put heading, search (omitted for a tool window) and the page list on one surface."""
    panel = QWidget()
    panel.setObjectName("settingsSidebar")
    panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    panel.setFixedWidth(SIDEBAR_WIDTH)
    panel.setStyleSheet(panel_qss())
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(theme.SPACE_1, theme.SPACE_2, theme.SPACE_1, theme.SPACE_1)
    layout.setSpacing(0)

    title = QLabel(heading)
    title.setObjectName("settingsSidebarHeading")
    title.setStyleSheet(heading_qss())
    layout.addWidget(title)
    layout.addSpacing(theme.SPACE_1)

    if search is not None:
        search.setObjectName('settingsSearchField')
        search.setStyleSheet(search_qss())
        search.setFixedHeight(theme.HIT_TARGET_MIN)
        # Reserve the decorative icon's column independently of QSS padding.
        search.setTextMargins(ICON_BOX + theme.SPACE_2 - FOCUS_RING - _LINE_EDIT_MARGIN, 0, 0, 0)
        search.setProperty("settingsInputTextInset", ICON_INSET + ICON_BOX + theme.SPACE_2)
        search.setClearButtonEnabled(True)
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), search)
        escape.setContext(Qt.ShortcutContext.WidgetShortcut)
        escape.activated.connect(search.clear)
        SearchGlyph(search)
        layout.addWidget(search)
        layout.addSpacing(theme.SPACE_2)

    pages.setItemDelegate(SidebarDelegate(pages))
    pages.setFrameShape(QFrame.Shape.NoFrame)
    pages.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    pages.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    pages.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    pages.setUniformItemSizes(True)
    pages.setMouseTracking(True)
    pages.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
    layout.addWidget(pages, 1)
    return panel
