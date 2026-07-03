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
TEXT_SECONDARY = "rgba(255,255,255,0.65)"
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
SUCCESS = "#6ee7a0"
ERROR = "#f87171"
WARNING = "#fbbf24"

# ---------------------------------------------------------------------------
# Type scale (4 sizes, mirroring the precedent's scale)
# ---------------------------------------------------------------------------

FONT_FAMILY = "'Segoe UI', system-ui, sans-serif"
FONT_SIZE_TITLE = 20
FONT_SIZE_HEADING = 15
FONT_SIZE_BODY = 13
FONT_SIZE_CAPTION = 11

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
    fill = _mix(BG2, "#ffffff", 0.65)   # approximates TEXT_SECONDARY over BG2
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
_CARD_QSS = (
    f"QFrame{{background:{BG1};border:1px solid {BORDER};border-radius:8px;}}"
)


def style_footer(widget: QWidget) -> None:
    """Apply the footer/nav-bar treatment: BG1 fill + a 1px top border so it
    visually separates from the body above it."""
    widget.setStyleSheet(_FOOTER_QSS)


def style_card(widget: QWidget) -> None:
    """Apply the content-card treatment: BG1 fill + full BORDER outline."""
    widget.setStyleSheet(_CARD_QSS)


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
"""
