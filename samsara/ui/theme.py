"""Shared design system for Samsara's wizard/tutorial windows.

Single source of truth for the tutorial window, the first-run wizard, and
the mic setup wizard. Mirrors the BG0/BG1/BG2 surface-tier + sharpened-
border approach used elsewhere (PRISM-dev/arc_qt.py) without importing that
code -- values here are chosen to match this app's existing accent
(the mic wizard's cyan "Next" button, the one component already proven to
render correctly) and to keep every step visually distinguishable.

Why a per-widget setStyleSheet() AND a class property
-------------------------------------------------------
Windows' native style engine can override QSS `background-color` for
QPushButton/QComboBox through stylesheet inheritance -- the mic wizard's own
"Next" button already works around this by calling setStyleSheet() directly
on the widget instead of relying purely on a dialog-wide `[class="..."]`
selector. make_primary()/make_secondary() below do both: they set the
`class` dynamic property (so `[class="primary"]` selectors elsewhere still
match, e.g. for QSS-only introspection/tooling) AND apply a complete
per-widget stylesheet directly, which always wins regardless of ancestor
stylesheet cascade timing. They also unpolish()+polish() in the correct
order -- a class property set without a repolish is a known prior failure
mode (the widget silently keeps whatever style it last resolved).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtWidgets import QPushButton, QWidget

# ---------------------------------------------------------------------------
# Color math (tiny, dependency-free -- just enough for hover/pressed/disabled
# variants derived from a single source color instead of hand-picked hex).
# ---------------------------------------------------------------------------

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    """Blend hex_a toward hex_b by fraction t (0.0 = hex_a, 1.0 = hex_b)."""
    a, b = _hex_to_rgb(hex_a), _hex_to_rgb(hex_b)
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


# ---------------------------------------------------------------------------
# Surface tiers -- each step must read as visibly distinct on a cheap panel.
# Values match the mic wizard's existing (proven-working) palette exactly,
# so converting it to this system doesn't change its appearance.
# ---------------------------------------------------------------------------

BG0 = "#0b0e14"   # window background
BG1 = "#131820"   # cards, footers/nav bars -- one step up from the window
BG2 = "#1a2030"   # inputs, hover states -- one step up from cards

# Sharpened border: a translucent white hairline, not a flat gray. Applied to
# every container, input, combo, and secondary button.
BORDER = "rgba(255,255,255,0.16)"
BORDER_FAINT = "rgba(255,255,255,0.08)"   # separators (hr-style, not outlines)

# ---------------------------------------------------------------------------
# Text tiers
# ---------------------------------------------------------------------------

TEXT_PRIMARY = "#e4e8ef"                  # near-white
# Secondary copy still needs to read as secondary, but 65% white was too
# subdued against all three dark surface tiers for explanatory text. Keep
# this centralized so every shared-theme consumer gets the accessibility
# improvement without one-off window overrides.
TEXT_SECONDARY = "rgba(255,255,255,0.75)"
TEXT_DISABLED = "rgba(255,255,255,0.40)"

# ---------------------------------------------------------------------------
# Accent -- read from the mic wizard's working "Next" button, the one
# component the task calls out as already correct. Hover/pressed/disabled
# are derived, not hand-picked, so the relationship stays principled.
# ---------------------------------------------------------------------------

ACCENT = "#5cc4d4"
ACCENT_HOVER = _mix(ACCENT, "#ffffff", 0.10)     # brightens ~10%
ACCENT_PRESSED = _mix(ACCENT, "#000000", 0.15)   # darkens ~15%
# Dim accent, not gray-on-gray -- same trick as ARC's #1e3a6e: blend the
# accent itself toward the window background rather than desaturating to gray.
ACCENT_DISABLED = _mix(ACCENT, BG0, 0.65)
TEXT_ON_ACCENT = BG0                              # dark text on accent fill

# Status colors (kept from the mic wizard's existing palette -- not part of
# the button/border system, but shared here so all three windows agree).
SUCCESS = "#6ee7a0"   # success / ready
ERROR = "#f87171"     # failure text and badges (red: never decorative)
WARNING = "#fbbf24"   # warning / caution

# ---------------------------------------------------------------------------
# Visual identity (owner decision 2026-09-13): ONE accent, ONE semantic.
#
#   ACCENT (cyan) is the only brand colour. Surfaces are the BG0/BG1/BG2
#   ladder above. Red is never decorative: it means live/recording
#   (RECORDING) or failure (ERROR). SUCCESS green and WARNING amber keep
#   their roles. No gold, no second accent.
#
# The icon system draws only from these tokens: assets/icon/samsara.svg is
# the single source and tools/gen_icons.py renders every size and state.
#   Wheel segments = capture state: ICON_IDLE grey (idle), ACCENT (listening
#     / wake armed), RECORDING (recording), AVA (Ava owns capture).
#     Motion and shape carry the state too, never colour alone: hollow
#     segments = not recording, filled = recording; pulse = listening;
#     spin = thinking/transcribing.
#   Hub = hands-free state, drawn as an eye on an ICON_HUB disc: closed line
#     = asleep/off, open eye with a pupil dot = wake listener armed, large
#     filled pupil = wake phrase heard, listening for the command.
# ---------------------------------------------------------------------------

RECORDING = "#c0392b"   # live capture: recording wheel segments, "live" state
# Retired as a brand colour. The token name is kept for existing importers;
# its only role is now RECORDING (live/recording).
BRAND_RED = RECORDING
AVA = "#a78bfa"         # Ava, the on-device assistant, owns the capture
ICON_IDLE = "#8b929c"   # idle wheel segments and the closed eye; mid grey that reads on light and dark taskbars
ICON_HUB = BG1          # hub disc behind the eye, so the eye reads on any taskbar colour

# ---------------------------------------------------------------------------
# Type scale (4 sizes, mirroring the precedent's scale)
# ---------------------------------------------------------------------------

FONT_FAMILY = "'Segoe UI', system-ui, sans-serif"
FONT_SIZE_TITLE = 20
FONT_SIZE_HEADING = 15
FONT_SIZE_BODY = 13
# 12 px is the smallest supported shared-theme text. Legacy windows with
# private stylesheets are intentionally outside this token's scope.
FONT_SIZE_CAPTION = 12

# Inscription-style display face, used once: the creed on the Home page's
# identity strip ("Free - Open source - Accessibility first"). Letterspaced
# capitals in a titling face; colour stays ICON_IDLE/TEXT_SECONDARY, never a
# texture. Every family in the stack ships with Windows or Office, and the
# generic serif closes the fallback chain.
FONT_FAMILY_DISPLAY = "'Perpetua Titling MT', 'Palatino Linotype', 'Book Antiqua', Georgia, serif"

# ---------------------------------------------------------------------------
# Combo-box dropdown arrow. QComboBox::down-arrow's CSS border-triangle trick
# (transparent left/right borders + a solid top border) does NOT render as a
# triangle in this Qt build -- it paints as a small filled block instead. A
# `data:` URI in QSS url() doesn't render either (Qt's QSS engine resolves
# url() against real paths, not embedded data). So, per the task's own
# fallback instruction, a tiny SVG chevron is written to a real temp file
# once at import time and referenced by path -- generated at runtime, not a
# repo asset, so there's nothing to add to scripts/samsara.spec datas.
# ---------------------------------------------------------------------------

def _write_arrow_svg() -> str:
    fill = _mix(BG2, "#ffffff", 0.75)   # approximates TEXT_SECONDARY over BG2
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6">'
        f'<path d="M0 0 L5 6 L10 0 Z" fill="{fill}"/></svg>'
    )
    path = Path(tempfile.gettempdir()) / "samsara_theme_combo_arrow.svg"
    path.write_text(svg, encoding="utf-8")
    return path.as_posix()   # QSS url() requires forward slashes, even on Windows


ARROW_PATH = _write_arrow_svg()

# ---------------------------------------------------------------------------
# Per-widget stylesheets (used directly by make_primary/make_secondary, not
# just via the dialog-wide QSS class selectors -- see module docstring).
# ---------------------------------------------------------------------------

_PRIMARY_BUTTON_QSS = (
    f"QPushButton{{background:{ACCENT};color:{TEXT_ON_ACCENT};"
    f"border:none;border-radius:6px;font-weight:600;padding:10px 24px;}}"
    f"QPushButton:hover{{background:{ACCENT_HOVER};color:{TEXT_ON_ACCENT};}}"
    f"QPushButton:pressed{{background:{ACCENT_PRESSED};color:{TEXT_ON_ACCENT};}}"
    f"QPushButton:disabled{{background:{ACCENT_DISABLED};color:{TEXT_DISABLED};}}"
)

_SECONDARY_BUTTON_QSS = (
    f"QPushButton{{background:transparent;color:{TEXT_PRIMARY};"
    f"border:1px solid {BORDER};border-radius:6px;padding:10px 24px;}}"
    f"QPushButton:hover{{background:rgba(255,255,255,0.06);color:{TEXT_PRIMARY};"
    f"border-color:{BORDER};}}"
    f"QPushButton:pressed{{background:rgba(255,255,255,0.10);color:{TEXT_PRIMARY};}}"
    f"QPushButton:disabled{{background:transparent;color:{TEXT_DISABLED};"
    f"border-color:{BORDER_FAINT};}}"
)

# Lower-emphasis than secondary: no border, muted text -- for de-emphasized
# actions like "Skip" that shouldn't compete with the primary/secondary pair.
_GHOST_BUTTON_QSS = (
    f"QPushButton{{background:transparent;color:{TEXT_SECONDARY};"
    f"border:none;padding:10px 12px;}}"
    f"QPushButton:hover{{color:{TEXT_PRIMARY};background:transparent;}}"
    f"QPushButton:disabled{{color:{TEXT_DISABLED};}}"
)


def make_primary(btn: QPushButton) -> None:
    """Style btn as the primary (accent-filled) action. Sets the `class`
    property for any QSS selectors that key off it AND applies a complete
    per-widget stylesheet directly (belt-and-suspenders against native
    style override -- see module docstring), then repolishes in the
    property-set -> unpolish -> polish order Qt requires to pick up a
    property change that affects style selectors."""
    btn.setProperty("class", "primary")
    btn.setStyleSheet(_PRIMARY_BUTTON_QSS)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def make_secondary(btn: QPushButton) -> None:
    """Style btn as the secondary (outlined) action. See make_primary()."""
    btn.setProperty("class", "secondary")
    btn.setStyleSheet(_SECONDARY_BUTTON_QSS)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def make_ghost(btn: QPushButton) -> None:
    """Style btn as a low-emphasis, borderless action (e.g. "Skip"). See
    make_primary()."""
    btn.setProperty("class", "ghost")
    btn.setStyleSheet(_GHOST_BUTTON_QSS)
    btn.style().unpolish(btn)
    btn.style().polish(btn)


_FOOTER_QSS = f"background:{BG1};border-top:1px solid {BORDER};"


def style_footer(widget: QWidget) -> None:
    """Apply the footer/nav-bar treatment: BG1 fill + a 1px top border so it
    visually separates from the body above it."""
    widget.setStyleSheet(_FOOTER_QSS)


# ---------------------------------------------------------------------------
# Bordered-card helpers -- class property + global QSS[class="..."] selector,
# same idiom as make_primary/make_secondary/make_ghost above, and NOT a
# per-widget setStyleSheet("QFrame{border:...}") call.
#
# Why: QLabel IS-A QFrame. A per-widget local stylesheet that sets `border`
# on a QFrame -- even with a bare-declaration selector-less string -- leaks
# that border onto any descendant QLabel that has its own local styleSheet()
# too (Qt's stylesheet engine resolves an unset `border` on such a label by
# walking up to the nearest ANCESTOR's local stylesheet, not standard CSS
# non-inheritance). This was the root cause of a real bug: plain text labels
# nested in a card rendered with their own faint rounded-rect outline, as if
# they were input fields. Routing the border through the shared top-level
# stylesheet via a class-attribute selector (verified empirically) avoids
# the leak, because there's no local per-widget declaration block for a
# child's unset property to inherit from.
# ---------------------------------------------------------------------------

def style_card(widget: QWidget) -> None:
    """Apply the content-card treatment: BG1 fill + full BORDER outline."""
    widget.setProperty("class", "card")
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def style_instruction_box(widget: QWidget) -> None:
    """Tinted instruction callout (interactive tutorial steps)."""
    widget.setProperty("class", "instructionBox")
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def style_success_banner(widget: QWidget) -> None:
    """Accent-tinted success confirmation banner."""
    widget.setProperty("class", "successBanner")
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def style_guide_card(widget: QWidget) -> None:
    """Hoverable "go deeper" link card (tutorial done page)."""
    widget.setProperty("class", "guideCard")
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def style_tip_frame(widget: QWidget) -> None:
    """Use-case tip callout (first-run wizard's complete page)."""
    widget.setProperty("class", "tipFrame")
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ---------------------------------------------------------------------------
# Scrollbars -- ONE treatment app-wide (queue 29)
# ---------------------------------------------------------------------------
# No arrow buttons, a transparent track, a fully rounded ICON_IDLE handle that
# brightens on hover and turns ACCENT while dragged. Never hidden and never
# overlay-only: the owner scrolls with painful joints and by voice, so the
# bar stays visible at a fixed width and the handle keeps a 44 px minimum
# grab length however long the page is. SCROLLBAR_QSS is part of
# build_stylesheet() and of every top-level window's own sheet (QSS set on a
# window beats the application sheet, so the rule has to live there too);
# install_app_scrollbars() adds it to the application stylesheet for windows
# that carry no sheet of their own.

SCROLLBAR_WIDTH = 10      # handle thickness in px; the bar adds a 2 px margin each side
_SCROLLBAR_MARGIN = 2
_SCROLLBAR_MIN_GRAB = 44  # px, the minimum handle length (accessibility)


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    return f"rgba({r},{g},{b},{alpha:.2f})"


def _scrollbar_qss() -> str:
    bar = SCROLLBAR_WIDTH + 2 * _SCROLLBAR_MARGIN
    radius = SCROLLBAR_WIDTH // 2
    rest, hover, pressed = _rgba(ICON_IDLE, 0.35), _rgba(ICON_IDLE, 0.55), _rgba(ACCENT, 0.55)
    return f"""
QScrollBar:vertical {{
    background: transparent; border: none; margin: 0px; width: {bar}px;
}}
QScrollBar:horizontal {{
    background: transparent; border: none; margin: 0px; height: {bar}px;
}}
QScrollBar::handle:vertical {{
    background: {rest}; border: none; border-radius: {radius}px;
    margin: {_SCROLLBAR_MARGIN}px; min-height: {_SCROLLBAR_MIN_GRAB}px;
}}
QScrollBar::handle:horizontal {{
    background: {rest}; border: none; border-radius: {radius}px;
    margin: {_SCROLLBAR_MARGIN}px; min-width: {_SCROLLBAR_MIN_GRAB}px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {hover}; }}
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed {{ background: {pressed}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px; width: 0px; border: none; background: transparent;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px; height: 0px; border: none; background: transparent;
}}
QScrollBar::up-arrow, QScrollBar::down-arrow, QScrollBar::left-arrow, QScrollBar::right-arrow {{
    width: 0px; height: 0px; background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}
"""


SCROLLBAR_QSS = _scrollbar_qss()
_SCROLLBAR_MARKER = "/* samsara-scrollbars */"


def install_app_scrollbars(app=None) -> None:
    """Add SCROLLBAR_QSS to the QApplication stylesheet once (idempotent), so
    windows without a sheet of their own inherit the same scrollbars."""
    if app is None:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
        app = QApplication.instance()
    if app is None:
        return
    current = app.styleSheet() or ""
    if _SCROLLBAR_MARKER not in current:
        app.setStyleSheet(current + _SCROLLBAR_MARKER + SCROLLBAR_QSS)


# ---------------------------------------------------------------------------
# Dialog-wide stylesheet
# ---------------------------------------------------------------------------

def build_stylesheet() -> str:
    """One QSS string covering the shared baseline for all three windows:
    window/dialog background and text, class-selector button rules (a
    fallback layer -- make_primary()/make_secondary() apply the same look
    directly per-widget, which is what actually guarantees correct
    rendering), combo box with a visible drop-down arrow, line/text edit
    with an accent focus ring, and radio-button indicators."""
    return f"""
QMainWindow, QDialog, QWidget {{
    background-color: {BG0};
    color: {TEXT_PRIMARY};
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}px;
}}
QLabel {{ background: transparent; color: {TEXT_PRIMARY}; }}

QPushButton[class="primary"] {{
    background-color: {ACCENT};
    color: {TEXT_ON_ACCENT};
    border: none;
    border-radius: 6px;
    font-weight: 600;
    padding: 10px 24px;
}}
QPushButton[class="primary"]:hover {{ background-color: {ACCENT_HOVER}; color: {TEXT_ON_ACCENT}; }}
QPushButton[class="primary"]:pressed {{ background-color: {ACCENT_PRESSED}; color: {TEXT_ON_ACCENT}; }}
QPushButton[class="primary"]:disabled {{ background-color: {ACCENT_DISABLED}; color: {TEXT_DISABLED}; }}

QPushButton[class="secondary"] {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 10px 24px;
}}
QPushButton[class="secondary"]:hover {{ background-color: rgba(255,255,255,0.06); border-color: {BORDER}; }}
QPushButton[class="secondary"]:pressed {{ background-color: rgba(255,255,255,0.10); }}
QPushButton[class="secondary"]:disabled {{ color: {TEXT_DISABLED}; border-color: {BORDER_FAINT}; }}

QPushButton[class="ghost"] {{
    background: transparent;
    color: {TEXT_SECONDARY};
    border: none;
}}
QPushButton[class="ghost"]:hover {{ color: {TEXT_PRIMARY}; }}

QPushButton[class="danger"] {{
    background: transparent;
    color: {TEXT_SECONDARY};
    border: none;
    font-size: {FONT_SIZE_CAPTION}px;
    padding: 4px 10px;
}}
QPushButton[class="danger"]:hover {{ color: {ERROR}; }}

QFrame[class="card"] {{
    background-color: {BG1};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame[class="instructionBox"] {{
    background-color: {BG1};
    border: 1px solid rgba(92,196,212,0.25);
    border-radius: 8px;
}}
QFrame[class="successBanner"] {{
    background-color: rgba(92,196,212,0.08);
    border: 1px solid rgba(92,196,212,0.3);
    border-radius: 8px;
}}
QFrame[class="guideCard"] {{
    background-color: {BG1};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame[class="guideCard"]:hover {{ border-color: rgba(92,196,212,0.4); }}
QFrame[class="tipFrame"] {{
    background-color: rgba(92,196,212,0.08);
    border: 1px solid rgba(92,196,212,0.25);
    border-radius: 8px;
}}

QComboBox {{
    background-color: {BG2};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
}}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
}}
QComboBox::down-arrow {{
    image: url({ARROW_PATH});
    width: 10px;
    height: 6px;
    margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG2};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
}}

QLineEdit, QTextEdit {{
    background-color: {BG2};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px;
    selection-background-color: {ACCENT};
    selection-color: {TEXT_ON_ACCENT};
}}
QLineEdit:focus, QTextEdit:focus {{ border: 1px solid {ACCENT}; }}

QRadioButton {{ color: {TEXT_PRIMARY}; spacing: 8px; }}
QRadioButton::indicator {{
    width: 16px; height: 16px;
    border-radius: 8px;
    border: 2px solid {BORDER};
    background: {BG2};
}}
QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

QScrollArea {{ border: none; background: transparent; }}
""" + SCROLLBAR_QSS
