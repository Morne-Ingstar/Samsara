"""Queue 78: the two reference windows -- the Command reference (the cheat
sheet) and the Quick Reference -- take their colour and type from
samsara/ui/theme.py, meet WCAG AA on every text pair, and clip nothing at
their default size or at the narrowest the user can drag them.

The owner, looking at the screenshot on morneis.com/samsara: "the coloring on
the command reference window is all off, the fonts look like shit, and some of
them are too small." The cause was a private eight-colour palette in
command_cheatsheet_qt.py, copied from the Tkinter version and left behind as
the tokens moved, plus TYPE_MIN (the 14 px ABSOLUTE floor, meant for captions)
used for every string in the window including the rows he reads to learn
commands.

These are gates, not a record of one fix: a new hex literal, a size below the
floor for reading text, a failing contrast pair or a clipped label all fail
here.
"""
from __future__ import annotations

import io
import re
import sys
import tempfile
import tokenize
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton

from samsara import command_catalog
from samsara.ui import command_cheatsheet_qt as cs
from samsara.ui import quick_reference_qt as qr
from samsara.ui import theme

REPO = Path(__file__).resolve().parent.parent
WINDOWS = ("samsara/ui/command_cheatsheet_qt.py", "samsara/ui/quick_reference_qt.py")

#: WCAG 2.2 SC 1.4.3. Everything in these windows is normal-size text: the
#: large-text allowance starts at 24 px (or 18.66 px bold) and nothing here
#: is that big except the Quick Reference title, which is measured anyway.
AA_NORMAL = 4.5
AA_LARGE = 3.0
LARGE_PX = 24


# ---------------------------------------------------------------------------
# Colour maths (same method as the queue 78 report's table)
# ---------------------------------------------------------------------------

def _parts(value: str) -> tuple:
    text = str(value).strip()
    if text.startswith("#"):
        h = text.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    inner = text[text.index("(") + 1:text.rindex(")")]
    bits = [p.strip() for p in inner.split(",")]
    r, g, b = (int(round(float(p))) for p in bits[:3])
    return r, g, b, (float(bits[3]) if len(bits) > 3 else 1.0)


def over(fg: str, bg: str) -> tuple:
    """fg composited onto bg. A translucent token has no ratio on its own."""
    fr, fg_, fb, fa = _parts(fg)
    br, bg_, bb, _ = _parts(bg)
    return (fr * fa + br * (1 - fa), fg_ * fa + bg_ * (1 - fa), fb * fa + bb * (1 - fa))


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def contrast(fg: str, bg: str) -> float:
    def lum(rgb):
        r, g, b = (_lin(c) for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    a, b = lum(over(fg, bg)), lum(over(bg, bg))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


#: The selected/pinned fill is the accent tinted onto the card surface.
#: Computed from the TOKEN, not from the window under test, so this file still
#: collects (and its gates still fail honestly) against a window that has not
#: been converted yet.
SELECTED_ALPHA = 0.14
SELECTED_OVER_BG1 = "#" + "".join(
    f"{int(round(c)):02x}" for c in over(theme._rgba(theme.ACCENT, SELECTED_ALPHA), theme.BG1))

#: Every text-on-background pair the two windows draw. Adding a pair to a
#: window means adding it here; that is the point of the gate.
PAIRS = [
    # --- Command reference -------------------------------------------------
    ("cheatsheet: window title", theme.TEXT_PRIMARY, theme.BG1, theme.TYPE_EMPHASIS),
    ("cheatsheet: opacity", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_BODY),
    ("cheatsheet: close button (hover)", theme.ERROR, theme.BG2, theme.TYPE_TITLE),
    ("cheatsheet: filter text", theme.TEXT_PRIMARY, theme.BG2, theme.TYPE_BODY),
    ("cheatsheet: filter placeholder", theme.TEXT_SECONDARY, theme.BG2, theme.TYPE_BODY),
    ("cheatsheet: list row", theme.TEXT_PRIMARY, theme.BG1, theme.TYPE_BODY),
    ("cheatsheet: list row, not live here", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_BODY),
    ("cheatsheet: list row hovered", theme.TEXT_PRIMARY, theme.BG2, theme.TYPE_BODY),
    ("cheatsheet: list row selected", theme.ACCENT, SELECTED_OVER_BG1, theme.TYPE_BODY),
    ("cheatsheet: section header", theme.TEXT_SECONDARY, theme.BG0, theme.TYPE_MIN),
    ("cheatsheet: static row phrase", theme.TEXT_PRIMARY, theme.BG1, theme.TYPE_BODY),
    ("cheatsheet: static row count", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_MIN),
    ("cheatsheet: pin star, pinned", theme.ACCENT, theme.BG1, theme.TYPE_BODY),
    ("cheatsheet: pin star, unpinned", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_BODY),
    ("cheatsheet: live-here-only box", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_BODY),
    ("cheatsheet: hidden count", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_MIN),
    ("cheatsheet: combo text", theme.TEXT_PRIMARY, theme.BG2, theme.TYPE_BODY),
    ("cheatsheet: catalog unavailable", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_BODY),
    # --- Quick Reference ---------------------------------------------------
    ("quickref: window title", theme.TEXT_PRIMARY, theme.BG0, theme.TYPE_TITLE),
    ("quickref: card heading", theme.TEXT_PRIMARY, theme.BG1, theme.TYPE_HEADING),
    ("quickref: row label", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_BODY),
    ("quickref: row value", theme.TEXT_PRIMARY, theme.BG1, theme.TYPE_HEADING),
    ("quickref: disabled row value", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_HEADING),
    ("quickref: phrase name", theme.TEXT_PRIMARY, theme.BG1, theme.TYPE_BODY),
    ("quickref: phrase description", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_MIN),
    ("quickref: footer note", theme.TEXT_SECONDARY, theme.BG1, theme.TYPE_MIN),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _rows():
    """Registry-shaped rows built from the real catalog, exactly what the
    live window is handed."""
    out = []
    for record in command_catalog.load_catalog_json() or []:
        out.append({
            "phrase": command_catalog.canonical_phrase(record),
            "aliases": record.get("aliases", []),
            "description": record.get("description", ""),
            "source": "builtin" if record.get("kind") == "builtin" else record.get("plugin"),
            "plugin": record.get("plugin"), "pack": record.get("pack"),
            "risk": record.get("risk"),
        })
    return out


@pytest.fixture
def sheet(qapp, tmp_path):
    def make(width=None, height=None):
        win = cs._CheatSheetWindow(lambda phrase: None, _rows, tmp_path / "palette.json")
        if width:
            win.resize(width, height or cs._DEFAULT_H)
        win.show()
        for _ in range(40):
            qapp.processEvents()
        return win
    return make


@pytest.fixture
def quickref(qapp):
    import types

    def make(width=None, height=None):
        win = qr._QuickReferenceWindow(types.SimpleNamespace(config={
            "hotkey": "ctrl+shift",
            "command_mode": {"enabled": True, "mode": "toggle"},
            "wake_word_enabled": True,
            "wake_word_config": {"phrase": "jarvis"},
        }))
        if width:
            win.resize(width, height or 760)
        win.show()
        for _ in range(40):
            qapp.processEvents()
        return win
    return make


def clipped(root) -> list:
    """Visible widgets given less room than they need. A QLabel that word-wraps
    is judged on height, everything else on its own size hint -- a QCheckBox
    and a QComboBox do not wrap, they just lose their last letters ("Live here
    onl", "Command Referen")."""
    bad = []
    widgets = []
    for kind in (QLabel, QCheckBox, QComboBox, QPushButton):
        widgets.extend(root.findChildren(kind))
    for w in widgets:
        if not w.isVisibleTo(root) or w.width() <= 0 or w.height() <= 0:
            continue
        name = w.accessibleName() or (w.text() if hasattr(w, "text") else "") or type(w).__name__
        if isinstance(w, QLabel) and w.wordWrap():
            need = w.heightForWidth(w.width())
            if need > w.height() + 1:
                bad.append(f"{name}: needs h={need}, has h={w.height()}")
            continue
        if isinstance(w, QComboBox):
            continue        # a combo elides on purpose -- it is the row's give
        hint = w.sizeHint()
        if hint.width() > w.width() + 1:
            bad.append(f"{name}: needs w={hint.width()}, has w={w.width()}")
    return bad


# ---------------------------------------------------------------------------
# 1. Colour and size come from the token module
# ---------------------------------------------------------------------------

def _code_only(path: Path) -> str:
    """Source with comments and docstrings removed: the palette history is
    written in the comments and must not trip the literal gate."""
    src = path.read_text(encoding="utf-8")
    out, prev_end, prev_type = [], (1, 0), None
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev_type in (None, tokenize.INDENT,
                                                         tokenize.NEWLINE, tokenize.NL):
            continue            # a bare string statement is a docstring
        out.append(tok.string)
        prev_type = tok.type if tok.type not in (tokenize.NL,) else prev_type
    return "\n".join(out)


_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_CSS_RGB = re.compile(r"\brgba?\s*\(")


class TestColourComesFromTheme:
    @pytest.mark.parametrize("rel", WINDOWS)
    def test_no_hardcoded_colour_literal_remains(self, rel):
        """Queue 78: command_cheatsheet_qt.py carried eight of these
        (_BG/_SURFACE/_ELEVATED/_ACCENT/_ACCENT_DIM/_TEXT_PRI/_TEXT_SEC/
        _BORDER) plus a one-off "#e06060" for the close button."""
        code = _code_only(REPO / rel)
        hexes = sorted(set(_HEX.findall(code)))
        assert hexes == [], f"{rel} still hardcodes {hexes}"
        assert not _CSS_RGB.search(code), (
            f"{rel} builds an rgba() by hand; use a token or theme._rgba()")

    @pytest.mark.parametrize("rel", WINDOWS)
    def test_every_colour_used_is_a_token_or_derived_from_one(self, rel):
        """Whatever names the file uses, each must resolve into theme."""
        code = _code_only(REPO / rel)
        for name in re.findall(r"\btheme\.([A-Z_][A-Z0-9_]*)\b", code):
            assert hasattr(theme, name), f"{rel} uses theme.{name}, which does not exist"

    def test_the_selected_fill_is_tinted_from_the_accent(self):
        """Not a flat dim-cyan hex: if ACCENT moves, the selection moves with
        it. Same alpha history_view.py uses, so both windows agree."""
        # This must be evaluated when the window is styled, not frozen at
        # import, because the app can switch between dark and light palettes.
        assert cs._selected_bg() == theme._rgba(theme.ACCENT, SELECTED_ALPHA)
        assert getattr(cs, "_SELECTED_ALPHA", None) == SELECTED_ALPHA


class TestTypeScale:
    @pytest.mark.parametrize("rel", WINDOWS)
    def test_no_font_size_literal(self, rel):
        code = _code_only(REPO / rel)
        assert not re.search(r"font-size\s*:\s*\d", code), f"{rel} hardcodes a font size"
        assert not re.search(r"setPixelSize\(\s*\d", code), f"{rel} hardcodes a pixel size"
        assert not re.search(r"setPointSize[F]?\(", code), f"{rel} sets a POINT size"

    @pytest.mark.parametrize("rel", WINDOWS)
    def test_nothing_is_below_the_absolute_floor(self, rel):
        code = _code_only(REPO / rel)
        used = {name for name in re.findall(r"theme\.(TYPE_[A-Z_]+|FONT_SIZE_[A-Z_]+)", code)}
        for name in used:
            value = getattr(theme, name)
            if isinstance(value, int):
                assert value >= theme.TYPE_FLOOR_ABSOLUTE, f"{rel} uses {name} = {value}px"

    def test_the_rows_the_owner_reads_are_at_the_body_floor(self):
        """"Some of them are too small": every string in this window was
        TYPE_MIN (14 px, the caption floor). The list rows, the filter, the
        static rows and controls are reading text and are TYPE_BODY now;
        only counts and other metadata stay at TYPE_MIN."""
        code = (REPO / WINDOWS[0]).read_text(encoding="utf-8")
        body_sites = [
            "QListWidget {{",                    # the command list itself
            "QLineEdit {{",                      # the filter the user types in
            "phrase_lbl.setStyleSheet",          # Most Used / Pinned rows
            "self._live_only.setStyleSheet",     # "Live here only"
            "self._opacity_label.setStyleSheet", # "Opacity"
        ]
        for marker in body_sites:
            index = code.index(marker)
            window = code[index:index + 400]
            assert "TYPE_BODY" in window, f"{marker} is not at the body floor"
        assert "TYPE_EMPHASIS" in code, "the window's own name is still caption-sized"

    def test_row_heights_leave_room_for_the_bigger_text(self, sheet):
        win = sheet()
        assert win._title_bar.height() >= theme.TYPE_EMPHASIS + 16
        assert win._category_bar.height() >= theme.TYPE_BODY + 16


# ---------------------------------------------------------------------------
# 2. Contrast
# ---------------------------------------------------------------------------

class TestContrast:
    @pytest.mark.parametrize("label,fg,bg,px", PAIRS, ids=[p[0] for p in PAIRS])
    def test_pair_meets_wcag_aa(self, label, fg, bg, px):
        need = AA_LARGE if px >= LARGE_PX else AA_NORMAL
        ratio = contrast(fg, bg)
        assert ratio >= need, f"{label}: {ratio:.2f}:1 on {fg} over {bg}, needs {need}:1"

    def test_the_old_private_palette_would_not_pass_this_bar(self):
        """The retired secondary slate cleared AA by 0.29; the token clears it
        by 5.8. This is why "all off" was a real report even though the old
        palette technically passed."""
        assert contrast("#7a8599", "#131820") < contrast(theme.TEXT_SECONDARY, theme.BG1)
        assert contrast(theme.TEXT_SECONDARY, theme.BG1) > 10.0

    def test_disabled_rows_are_readable_not_ghosts(self):
        """Quick Reference dimmed its switched-off rows with TEXT_DISABLED
        (3.82:1). WCAG exempts inactive CONTROLS; these are rows the user is
        meant to read, so they are marked by the word "(disabled)" instead."""
        assert contrast(theme.TEXT_DISABLED, theme.BG1) < AA_NORMAL      # why it moved
        source = (REPO / WINDOWS[1]).read_text(encoding="utf-8")
        assert "TEXT_DISABLED" not in _code_only(REPO / WINDOWS[1])
        assert "(disabled)" in source


# ---------------------------------------------------------------------------
# 3. Nothing is clipped
# ---------------------------------------------------------------------------

class TestNothingIsClipped:
    def test_command_reference_at_its_default_size(self, sheet):
        win = sheet()
        assert clipped(win) == []

    def test_command_reference_at_its_narrowest(self, sheet):
        """The owner saw "show empty wake..." cut off elsewhere; this window
        had "Command Referen" and "Live here onl". The window now refuses to
        be dragged narrower than the rows it has to draw."""
        win = sheet()
        win.resize(240, 400)             # far below anything sane
        for _ in range(40):
            win.parent() or None
        win.show()
        assert win.width() >= win.minimumWidth()
        assert clipped(win) == []

    def test_the_minimum_width_is_measured_from_the_rows_not_guessed(self, sheet):
        win = sheet()
        need = max(win._title_bar.sizeHint().width(),
                   win._category_bar.sizeHint().width())
        assert win.minimumWidth() >= need
        assert cs._DEFAULT_W >= win.minimumWidth()

    def test_quick_reference_at_its_default_and_narrow(self, quickref):
        win = quickref()
        assert clipped(win) == []
        win.resize(460, 760)
        win.show()
        assert clipped(win) == []


# ---------------------------------------------------------------------------
# 4. The content still generates from the catalog (queue 45's rule)
# ---------------------------------------------------------------------------

class TestStillGeneratedFromTheCatalog:
    def test_every_row_is_a_catalog_command(self, sheet):
        win = sheet()
        phrases = {command_catalog.canonical_phrase(r)
                   for r in command_catalog.load_catalog_json() or []}
        assert win._list.count() > 50
        for i in range(win._list.count()):
            item = win._list.item(i)
            assert item.data(0x0100) in phrases      # Qt.UserRole

    def test_no_command_phrase_is_written_into_the_window_source(self):
        """The overhaul was styling only: a hand-written phrase here would
        mean the window had stopped reading the catalog."""
        code = _code_only(REPO / WINDOWS[0])
        aliases = {a.lower() for r in (command_catalog.load_catalog_json() or [])
                   for a in r.get("aliases", []) if len(a.split()) > 1}
        hits = [a for a in aliases if f'"{a}"' in code.lower()]
        assert hits == [], f"hardcoded command phrases: {hits}"
