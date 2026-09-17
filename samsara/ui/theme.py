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

import re
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QPushButton, QWidget

# ---------------------------------------------------------------------------
# Color math (tiny, dependency-free -- just enough for hover/pressed/disabled
# variants derived from a single source color instead of hand-picked hex).
# ---------------------------------------------------------------------------

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


_CSS_RGB = re.compile(
    r"^\s*rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+%?)\s*)?\)\s*$",
    re.IGNORECASE)


def qcolor(value) -> QColor:
    """A token colour as a QColor, for painting (icons, QPainter pens).

    Tokens are written for stylesheets, so several are CSS 'rgba(r,g,b,a)'
    with a 0-1 alpha (TEXT_SECONDARY, TEXT_DISABLED, BORDER). QColor(str)
    only understands '#rgb'/'#rrggbb'/'#aarrggbb' and SVG names: it returns an
    INVALID colour for rgb()/rgba(), which a pen paints as opaque black --
    the near-black sidebar icons (queue 74). Unparseable input falls back to
    TEXT_PRIMARY rather than black, and is never silently invisible."""
    if isinstance(value, QColor):
        return QColor(value)
    text = str(value or "").strip()
    m = _CSS_RGB.match(text)
    if m:
        r, g, b = (max(0, min(255, round(float(c)))) for c in m.group(1, 2, 3))
        alpha = m.group(4)
        if alpha is None:
            a = 255
        elif alpha.endswith("%"):
            a = round(max(0.0, min(100.0, float(alpha[:-1]))) * 2.55)
        else:
            a = round(max(0.0, min(1.0, float(alpha))) * 255)
        return QColor(r, g, b, a)
    colour = QColor(text)
    return colour if colour.isValid() else QColor(TEXT_PRIMARY)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    """Blend hex_a toward hex_b by fraction t (0.0 = hex_a, 1.0 = hex_b)."""
    a, b = _hex_to_rgb(hex_a), _hex_to_rgb(hex_b)
    return _rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def mix(hex_a: str, hex_b: str, t: float) -> str:
    """An OPAQUE blend of two tokens, for a tinted fill that must not let the
    surface behind it through (a state row, a warning strip). tint() is the
    translucent one; this is the one to reach for when a widget sits on an
    unknown background."""
    return _mix(hex_a, hex_b, t)


# ---------------------------------------------------------------------------
# Palettes (queue 129) -- TWO of them, one set of token names
# ---------------------------------------------------------------------------
#
# Samsara was dark-only. Dark is not universally accessible: astigmatism,
# some low-vision conditions and a bright room all read light-on-dark worse,
# not better. So the token names below are a CONTRACT and the values behind
# them come from whichever palette is active. Nothing outside this module
# learns which theme is running -- no surface may branch on it, and
# tests/test_colour_tokens.py fails the build on a colour literal anywhere
# in the app.
#
# The contract, token by token:
#   BG0  window background            BG1  cards, footers, nav bars
#   BG2  inputs, hover fills          BORDER / BORDER_FAINT  hairlines
#   TEXT_PRIMARY / TEXT_SECONDARY / TEXT_DISABLED   the three text tiers
#   ACCENT  the one brand colour      TEXT_ON_ACCENT  text on an accent fill
#   SUCCESS / WARNING / ERROR         state colours (never decorative)
#   INK_LIGHT / INK_DARK  the palette's two extremes, for ink_on()
#   RECORDING  live capture           AVA  the on-device assistant
#   ICON_IDLE  idle marks AND the muted note text used all over Settings
#   HOVER_WASH / PRESS_WASH  the translucent overlay on a hover/press
#
# Derived per palette, never hand-picked: ACCENT_HOVER, ACCENT_PRESSED,
# ACCENT_DISABLED, ACCENT_DIM, BRAND_RED, ICON_HUB.
#
# CONTRAST IS THE ACCEPTANCE GATE, NOT TASTE. Every pair that renders text
# clears WCAG AA 4.5:1 on all three surfaces in BOTH palettes; the measured
# table is in tests/test_theme_contrast.py, which fails on a regression.
# TEXT_DISABLED is the one deliberate exception -- WCAG 2.2 SC 1.4.3 exempts
# inactive controls, and a disabled control that reads as enabled is its own
# accessibility bug.

#: Dark: the original palette, value for value. `dark` must stay the default
#: and must not shift -- this is the app every existing screenshot shows.
_DARK = {
    "POLARITY": "dark",
    "BG0": "#0b0e14",
    "BG1": "#131820",
    "BG2": "#1a2030",
    # Sharpened border: a translucent white hairline, not a flat gray.
    "BORDER": "rgba(255,255,255,0.16)",
    "BORDER_FAINT": "rgba(255,255,255,0.08)",
    "TEXT_PRIMARY": "#e4e8ef",
    # 65% white was too subdued for explanatory text against all three dark
    # surfaces; 75% is the accessibility fix every consumer inherits.
    "TEXT_SECONDARY": "rgba(255,255,255,0.75)",
    "TEXT_DISABLED": "rgba(255,255,255,0.40)",
    "ACCENT": "#5cc4d4",
    "TEXT_ON_ACCENT": "#0b0e14",      # = BG0; dark ink on the bright fill
    "SUCCESS": "#6ee7a0",
    "ERROR": "#f87171",
    "WARNING": "#fbbf24",
    "RECORDING": "#c0392b",
    # The two extremes of ink this palette has to offer. ink_on() picks
    # between them; nothing else should read them directly.
    "INK_LIGHT": "#ffffff",
    "INK_DARK": "#0b0e14",
    "AVA": "#a78bfa",
    "ICON_IDLE": "#8b929c",
    "HOVER_WASH": "rgba(255,255,255,0.06)",
    "PRESS_WASH": "rgba(255,255,255,0.10)",
}

#: Light: the same roles with the polarity flipped. Surfaces run white-ish
#: (BG1 is the paper the cards are printed on, BG0 the desk under them, BG2
#: the recessed input). Every hue keeps its identity -- the accent is still
#: the same cyan family, red is still red -- but each is darkened until it
#: clears 4.5:1 on white, because a colour tuned to glow on near-black is
#: invisible on paper. Worst text pair here is 5.36:1 (ICON_IDLE on BG2).
#: The reds are also held apart from each other by the same perceptual
#: distance they have in the dark palette -- ERROR and RECORDING are both
#: red on purpose, but they must not become the same red.
_LIGHT = {
    "POLARITY": "light",
    "BG0": "#f3f6fa",
    "BG1": "#ffffff",
    "BG2": "#e8edf4",
    "BORDER": "rgba(0,0,0,0.22)",
    "BORDER_FAINT": "rgba(0,0,0,0.10)",
    "TEXT_PRIMARY": "#11151c",
    "TEXT_SECONDARY": "rgba(0,0,0,0.68)",
    "TEXT_DISABLED": "rgba(0,0,0,0.38)",
    "ACCENT": "#05687f",
    "TEXT_ON_ACCENT": "#ffffff",      # light ink on the dark fill
    "SUCCESS": "#0d6a35",
    "ERROR": "#b81f1f",
    "WARNING": "#7f4c00",
    "RECORDING": "#7d1a0e",
    "INK_LIGHT": "#ffffff",
    "INK_DARK": "#0a0c11",
    "AVA": "#5731bf",
    "ICON_IDLE": "#5c6068",
    "HOVER_WASH": "rgba(0,0,0,0.05)",
    "PRESS_WASH": "rgba(0,0,0,0.09)",
}

PALETTES = {"dark": _DARK, "light": _LIGHT}

#: Values a config key may carry. "system" is resolved, never stored active.
THEME_CHOICES = ("dark", "light", "system")
DEFAULT_THEME = "dark"

#: Every name _install() binds as a module attribute. The colour-literal test
#: reads this, so a token added to a palette without being listed here fails.
PALETTE_TOKENS = tuple(_DARK)
DERIVED_TOKENS = (
    "ACCENT_HOVER", "ACCENT_PRESSED", "ACCENT_DISABLED", "ACCENT_DIM",
    "BRAND_RED", "ICON_HUB",
)

#: The palette currently bound to the module attributes. Read it with
#: active_theme(); nothing outside this module may branch on it to pick a
#: colour -- that is what the tokens are for. It exists so the app can tell
#: the user which theme is live and so tests can assert a switch happened.
_ACTIVE = DEFAULT_THEME
_REQUESTED_SETTING = DEFAULT_THEME
_SYSTEM_THEME_TIMER = None
_RETHEME_REPLACEMENTS: tuple[tuple[str, str], ...] = ()


class _ThemeChangeSignal(QObject):
    """The one live-palette notification channel for Qt surfaces.

    A token is a module value, not a live reference: a QSS f-string made
    while a window is constructed retains the old colour text.  The signal is
    emitted only for ``set_theme(..., refresh=True)`` so boot may still bind a
    palette before Qt exists without needlessly restyling anything.
    """

    changed = Signal(str)


_THEME_CHANGE_SIGNAL = _ThemeChangeSignal()


def theme_change_signal() -> _ThemeChangeSignal:
    """Return the shared signal whose payload is the resolved palette name."""
    return _THEME_CHANGE_SIGNAL


def _install(name: str) -> None:
    """Bind one palette's tokens, and everything derived from them, onto this
    module. Every colour in the app resolves through these attributes, so
    this one call is the whole theme switch."""
    global _ACTIVE
    values = dict(PALETTES[name])

    accent, bg0, bg1 = values["ACCENT"], values["BG0"], values["BG1"]
    if values["POLARITY"] == "dark":
        # A bright accent on a dark surface: lift on hover, sink on press.
        values["ACCENT_HOVER"] = _mix(accent, "#ffffff", 0.10)
        values["ACCENT_PRESSED"] = _mix(accent, "#000000", 0.15)
    else:
        # A dark accent on a light surface: the same gesture goes down BOTH
        # times, or hover would wash the fill out toward the page.
        values["ACCENT_HOVER"] = _mix(accent, "#000000", 0.12)
        values["ACCENT_PRESSED"] = _mix(accent, "#000000", 0.26)
    # Dim accent, not gray-on-gray -- same trick as ARC's #1e3a6e: blend the
    # accent itself toward the window background rather than desaturating.
    values["ACCENT_DISABLED"] = _mix(accent, bg0, 0.65)
    # A tinted accent SURFACE (selected rows, "armed" chips), never text.
    values["ACCENT_DIM"] = _mix(accent, bg1, 0.78)
    # Retired as a brand colour; the name is kept for existing importers and
    # its only role is now RECORDING (live capture).
    values["BRAND_RED"] = values["RECORDING"]
    # Hub disc behind the eye, so the eye reads on any taskbar colour.
    values["ICON_HUB"] = bg1

    globals().update(values)
    _ACTIVE = name
    # Derived artefacts that bake token values in. Rebuilt on every switch,
    # never cached across one.
    globals()["ARROW_PATH"] = _write_arrow_svg()
    globals()["SCROLLBAR_QSS"] = _scrollbar_qss()


def active_theme() -> str:
    """Which palette is bound right now: "dark" or "light" (never "system")."""
    return _ACTIVE


def detect_os_theme() -> str:
    """The OS's own light/dark preference, or DEFAULT_THEME if it cannot be
    read. Windows keeps it in AppsUseLightTheme (1 = light, 0 = dark); a
    missing key, a non-Windows host and a frozen build with no registry
    access all fall back rather than guessing."""
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return DEFAULT_THEME
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return DEFAULT_THEME
    try:
        return "light" if int(value) == 1 else "dark"
    except (TypeError, ValueError):
        return DEFAULT_THEME


def resolve_theme(setting) -> str:
    """Map a config value to a palette name. "system" asks the OS; anything
    unrecognised (a hand-edited config, an older build's value) falls back to
    the default rather than raising -- a config typo must not cost the user
    their window."""
    name = str(setting or "").strip().lower()
    if name == "system":
        return detect_os_theme()
    return name if name in PALETTES else DEFAULT_THEME


def set_theme(setting, *, refresh: bool = True) -> str:
    """Make `setting` ("dark" / "light" / "system") the live palette and
    return the palette name it resolved to. Call it ONCE at startup before
    any window is built; call it again with refresh=True to switch a running
    app (see refresh_all() for what that can and cannot reach)."""
    global _REQUESTED_SETTING, _RETHEME_REPLACEMENTS

    requested = str(setting or "").strip().lower()
    _REQUESTED_SETTING = requested if requested in THEME_CHOICES else DEFAULT_THEME
    name = resolve_theme(_REQUESTED_SETTING)
    if name != _ACTIVE:
        old_values = _live_theme_values()
        _install(name)
        _RETHEME_REPLACEMENTS = _build_retheme_replacements(old_values)
        if refresh:
            _THEME_CHANGE_SIGNAL.changed.emit(name)
            refresh_all()
    _ensure_system_theme_monitor()
    return name


def _live_theme_values() -> dict[str, str]:
    """Snapshot every live string token before rebinding the palette."""
    return {
        name: value for name, value in globals().items()
        if name.isupper() and isinstance(value, str)
    }


def _build_retheme_replacements(old_values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Map rendered token values from the old palette to the new one.

    A few long-lived Settings controls own tiny inline sheets. They are built
    from theme tokens, but Qt stores the rendered strings, not the token
    expressions. Keeping this mapping here lets those controls repaint
    without rebuilding the Settings window (and discarding unsaved edits).
    """
    replacements: dict[str, str] = {}
    for name, old in old_values.items():
        new = globals().get(name)
        if not (isinstance(old, str) and isinstance(new, str) and old != new):
            continue
        # Several semantic tokens deliberately share an old literal.  For
        # example dark BG0 and INK_DARK are both #0a0c11, but only BG0 changes
        # in light mode.  _live_theme_values preserves palette order, with
        # the surface token first; never let a later unchanged alias erase
        # that required live-surface replacement.
        replacements.setdefault(old, new)
    # tint() and wash() are deliberately rendered into inline QSS strings.
    # Cover their two-decimal rgba output as well as the named tokens above.
    # (That is the exact precision _rgba() writes.)
    for name, old in old_values.items():
        new = globals().get(name)
        if not (isinstance(old, str) and isinstance(new, str)
                and old.startswith("#") and new.startswith("#")):
            continue
        for hundredths in range(101):
            alpha = hundredths / 100
            replacements[_rgba(old, alpha)] = _rgba(new, alpha)
    old_polarity = old_values.get("POLARITY")
    if old_polarity in ("dark", "light"):
        old_ink = "#ffffff" if old_polarity == "dark" else "#000000"
        new_ink = "#ffffff" if POLARITY == "dark" else "#000000"
        for hundredths in range(101):
            alpha = hundredths / 100
            replacements[_rgba(old_ink, alpha)] = _rgba(new_ink, alpha)
    return tuple(sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True))


def retheme_stylesheet(stylesheet: str) -> str:
    """Rebind token-derived values in an already-assigned widget stylesheet.

    This is deliberately narrow: it is for a widget that was constructed
    under the immediately preceding palette. New widgets still build their
    stylesheets directly from current tokens.
    """
    restyled = stylesheet
    for old, new in _RETHEME_REPLACEMENTS:
        restyled = restyled.replace(old, new)
    return restyled


def _ensure_system_theme_monitor(app=None) -> None:
    """Poll Windows' app-theme preference while the user chose ``system``.

    ``apply_early_theme`` intentionally runs before Qt exists, so the first
    window that installs application scrollbars starts this Qt-owned timer.
    The timer never crosses threads and stops as soon as the user chooses a
    concrete palette.
    """
    global _SYSTEM_THEME_TIMER
    try:
        from PySide6.QtCore import QTimer  # noqa: PLC0415
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError:
        return
    app = app or QApplication.instance()
    if app is None:
        return
    if _REQUESTED_SETTING != "system":
        if _SYSTEM_THEME_TIMER is not None:
            _SYSTEM_THEME_TIMER.stop()
        return
    if _SYSTEM_THEME_TIMER is None:
        _SYSTEM_THEME_TIMER = QTimer(app)
        _SYSTEM_THEME_TIMER.setInterval(1000)
        _SYSTEM_THEME_TIMER.timeout.connect(_follow_system_theme)
    if not _SYSTEM_THEME_TIMER.isActive():
        _SYSTEM_THEME_TIMER.start()


def _follow_system_theme() -> None:
    """Apply a Windows app-theme change to live widgets, if one occurred."""
    if _REQUESTED_SETTING == "system":
        set_theme("system", refresh=True)


def refresh_all() -> int:
    """Re-style what is already on screen, and return how many widgets took
    it. Best effort by design: a window builds most of its per-widget
    stylesheets in its constructor, so only widgets that expose an
    `apply_theme()` of their own can be repainted in place. Everything else
    picks the new palette up the next time it is opened -- which is why the
    Settings control says so out loud instead of leaving the user in front of
    a half-themed app."""
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    app = QApplication.instance()
    if app is None:
        return 0
    app.setStyleSheet((app.styleSheet() or "").split(_SCROLLBAR_MARKER)[0])
    install_app_scrollbars(app)

    restyled = 0
    seen = set()
    # A hide-on-close window is not open. It will receive current tokens if
    # rebuilt later; walking it here wastes work and can retain stale Qt test
    # wrappers. Collect each visible window's descendants once: repeatedly
    # calling findChildren() from every descendant is quadratic on Settings.
    stack = []
    for window in app.topLevelWidgets():
        try:
            if window.isVisible():
                stack.append(window)
                if not getattr(window, "_theme_signal_manages_descendants", False):
                    stack.extend(window.findChildren(QWidget))
        except RuntimeError:
            continue
    while stack:
        widget = stack.pop()
        if id(widget) in seen:
            continue
        seen.add(id(widget))
        # A subscribed root already restyled its full tree synchronously
        # before refresh_all() began.  Avoid a second stylesheet pass (and
        # attendant extra repaint) over that same visible window.
        if getattr(widget, "_theme_signal_manages_descendants", False):
            continue
        # A visible detached surface may not have a bespoke hook.  Its direct
        # QSS is still a token-expanded snapshot, so rebind it at the shared
        # seam before asking any richer hook to rebuild dynamic content.
        # `styleSheet()` is direct-widget QSS only; inherited application QSS
        # is left to install_app_scrollbars() above.
        try:
            inline = widget.styleSheet()
            if inline:
                widget.setStyleSheet(retheme_stylesheet(inline))
        except RuntimeError:
            continue
        hook = getattr(widget, "apply_theme", None)
        if callable(hook):
            try:
                hook()
                restyled += 1
            except Exception:   # a repaint must never take the app down
                pass
        try:
            widget.update()
        except RuntimeError:
            pass
    return restyled


# ---------------------------------------------------------------------------
# Contrast math -- the acceptance gate for a palette, not a test-only helper
# ---------------------------------------------------------------------------

def _relative_luminance(rgb) -> float:
    channels = []
    for value in rgb:
        channel = value / 255.0
        channels.append(channel / 12.92 if channel <= 0.04045
                        else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def composite(value, over_hex: str):
    """A token as an opaque (r,g,b), flattening a translucent one onto
    `over_hex`.

    Several text tokens are `rgba(...)` with a 0-1 alpha, so the contrast the
    user actually sees depends on the surface behind them. Measuring such a
    token as if it were opaque reports a ratio that is never on screen."""
    colour = qcolor(value)
    alpha = colour.alpha() / 255.0
    base = _hex_to_rgb(over_hex)
    return tuple(round(base[i] * (1.0 - alpha) + channel * alpha)
                 for i, channel in enumerate(colour.getRgb()[:3]))


def ink_on(fill_hex: str) -> str:
    """The ink from this palette that reads best on a saturated fill.

    There is no one answer, in either palette. White reads on the dark
    palette's RECORDING (5.44:1) and disappears on its ERROR (2.77:1); the
    window colour reads on ERROR (6.98:1) and disappears on RECORDING
    (3.55:1). A badge that picks one and keeps it is unreadable half the
    time, so the choice is measured rather than written down."""
    return max((INK_LIGHT, INK_DARK), key=lambda ink: contrast_ratio(ink, fill_hex))


def contrast_ratio(foreground, background_hex: str) -> float:
    """WCAG 2.2 contrast of a token against an opaque surface token. 4.5:1 is
    the AA floor for normal text, 3:1 for large text and for graphics."""
    fore = _relative_luminance(composite(foreground, background_hex))
    back = _relative_luminance(_hex_to_rgb(background_hex))
    lighter, darker = max(fore, back), min(fore, back)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Type scale -- ONE scale for the whole app, with enforced floors (queue 83)
# ---------------------------------------------------------------------------
#
# Every font size in the app is one of the TYPE_* tokens below, in CSS/Qt
# logical px (Qt 6 scales px with Windows display scaling and with Samsara's
# own Interface size). tests/test_type_floor.py fails the build on a numeric
# size literal anywhere in samsara/, plugins/ or dictation.py, on a size that
# bypasses these tokens, on a point-size QFont, and on thin weights.
#
# The floors, and why these numbers (an agent cannot see small text; the
# standard has to be written down):
#   * WCAG 2.2 sets NO minimum size -- SC 1.4.4 only requires text to survive
#     200% zoom, SC 1.4.3 defines "large text" as 18pt / 14pt bold. So the
#     floors come from platform and low-vision guidance instead.
#   * Windows 11 Fluent type ramp: Body 14 epx, Caption 12 epx. Captions at
#     12 are a convention for typical sight; this app is for people who are
#     not typical, and the owner reports the 12-14 px text as hard to read.
#   * Browser default text, and NN/g, GOV.UK and USWDS body guidance: 16 px.
#     RNIB Clear Print: 12 pt minimum for reading text -- 16 px at 96 dpi.
#   TYPE_MIN (14 px): the ABSOLUTE floor -- nothing in the app is smaller than
#     Windows' own body text. Captions, badges, letterspaced section labels.
#   TYPE_BODY (16 px): the BODY floor -- anything a user reads as sentences
#     (body, descriptions, instructions, buttons, inputs, list rows).
#   Weight: regular (400) or heavier. No Light/Semilight/Thin faces or
#     font-weight below 400: a thin stroke at 14 px reads smaller than a
#     regular stroke at 13 (the owner: "small AND thin").
# Headings keep their step above body so hierarchy survives the floor.
TYPE_FLOOR_ABSOLUTE = 14
TYPE_FLOOR_BODY = 16
FONT_WEIGHT_MIN = 400

TYPE_MIN = TYPE_FLOOR_ABSOLUTE   # captions, badges, metadata, chips
TYPE_BODY = TYPE_FLOOR_BODY      # body copy, buttons, inputs, list rows
TYPE_EMPHASIS = 17               # nav rows, card titles, row titles
TYPE_HEADING = 18                # section headings, the one state line
TYPE_TITLE = 22                  # window/page titles
TYPE_DISPLAY = 26                # display wordmark, large labels
TYPE_FIGURE = 28                 # usage figures
TYPE_HERO = 40                   # splash title, the biggest in-window text
TYPE_BANNER = 56                 # full-screen demo banners
TYPE_GLYPH = 96                  # single decorative glyphs (not reading text)

FONT_FAMILY = "'Segoe UI', system-ui, sans-serif"
# The four shared-scale names predate the hub scale; kept for importers, now
# aliases of the one scale.
FONT_SIZE_TITLE = TYPE_TITLE
FONT_SIZE_HEADING = TYPE_HEADING
FONT_SIZE_BODY = TYPE_BODY
FONT_SIZE_CAPTION = TYPE_MIN


def qfont(px: int, family: str = "Segoe UI", weight=None, italic: bool = False):
    """A QFont sized in px from a TYPE_* token -- never QFont(family, points).

    Qt's point sizes are 1.33x px at 96 dpi, so a point literal silently
    means a different size from every stylesheet token (a 9 pt label is
    12 px). Painted text and overlays use this so the one scale and its
    floor apply to them too."""
    from PySide6.QtGui import QFont  # noqa: PLC0415

    font = QFont(family)
    font.setPixelSize(int(px))  # type-floor: exempt -- this IS the helper every caller uses
    if weight is not None:
        font.setWeight(weight)
    if italic:
        font.setItalic(True)
    return font

# Inscription-style display face, used once: the creed on the Home page's
# identity strip ("Free - Open source - Accessibility first"). Letterspaced
# capitals in a titling face; colour stays ICON_IDLE/TEXT_SECONDARY, never a
# texture. Every family in the stack ships with Windows or Office, and the
# generic serif closes the fallback chain.
FONT_FAMILY_DISPLAY = "'Perpetua Titling MT', 'Palatino Linotype', 'Book Antiqua', Georgia, serif"
# The wordmark beside the mark in the hub window's header band (38): the
# display face at a real display size, letterspaced. Additive tokens.
FONT_SIZE_DISPLAY = TYPE_DISPLAY
LETTER_SPACING_DISPLAY = "0.14em"

# Hub type scale (41): the hub window and its Home page. Role names kept;
# values follow the one scale above (83).
TYPE_NAV = TYPE_EMPHASIS            # sidebar rows
TYPE_STATE = TYPE_HEADING           # Home's one state line
TYPE_CARD_TITLE = TYPE_EMPHASIS     # capability card titles, the outcome kind
TYPE_SECONDARY = TYPE_BODY          # instruction line, descriptions, notes (reading text)
TYPE_SECTION_LABEL = TYPE_MIN       # letterspaced capitals ("WHAT YOU CAN DO")
LETTER_SPACING_SECTION = "0.08em"
TYPE_CREED = TYPE_BODY              # the creed, in the display face

#: Home's text roles -> px (the table tests/test_home_qt.py checks).
#: "page title" and "tagline" are additive (queue 86): Home now opens with a
#: title and the owner's one-line tagline instead of a state panel.
HOME_TYPE_SCALE = {
    "page title": TYPE_FIGURE,
    "tagline": TYPE_SECONDARY,
    "nav": TYPE_NAV,
    "state line": TYPE_STATE,
    "instruction": TYPE_SECONDARY,
    "stop reason": TYPE_SECONDARY,
    "menu item": TYPE_BODY,
    "button": TYPE_BODY,
    "section label": TYPE_SECTION_LABEL,
    "outcome kind": TYPE_CARD_TITLE,
    "outcome text": TYPE_SECONDARY,
    "note": TYPE_SECONDARY,
    "card title": TYPE_CARD_TITLE,
    "card description": TYPE_SECONDARY,
    "card value": TYPE_BODY,
    "usage figure": TYPE_FIGURE,
    "usage label": TYPE_SECONDARY,
    "creed": TYPE_CREED,
    "status label": TYPE_SECTION_LABEL,
    "status value": TYPE_BODY,
    "header badge": TYPE_SECTION_LABEL,
}

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
    # Approximates TEXT_SECONDARY composited over BG2, whichever way the
    # palette points: blend BG2 toward the ink colour, not toward white.
    ink = "#ffffff" if POLARITY == "dark" else "#000000"
    fill = _mix(BG2, ink, 0.75)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6">'
        f'<path d="M0 0 L5 6 L10 0 Z" fill="{fill}"/></svg>'
    )
    # One file per theme: Qt caches QSS url() images by path, so reusing one
    # filename would keep painting the dark arrow after a switch to light.
    path = Path(tempfile.gettempdir()) / f"samsara_theme_combo_arrow_{POLARITY}.svg"
    path.write_text(svg, encoding="utf-8")
    return path.as_posix()   # QSS url() requires forward slashes, even on Windows

# ---------------------------------------------------------------------------
# Per-widget stylesheets (used directly by make_primary/make_secondary, not
# just via the dialog-wide QSS class selectors -- see module docstring).
# ---------------------------------------------------------------------------

# These are FUNCTIONS, not module constants (queue 129). An f-string
# evaluated at import time freezes whichever palette happened to be active
# when the module first loaded, which is exactly how a theme switch leaves
# one dialog black in the middle of a light app. Every QSS string in this
# file, and in every window that has its own sheet, is built on demand.

def primary_button_qss() -> str:
    return (
        f"QPushButton{{background:{ACCENT};color:{TEXT_ON_ACCENT};"
        f"border:none;border-radius:6px;font-weight:600;padding:10px 24px;}}"
        f"QPushButton:hover{{background:{ACCENT_HOVER};color:{TEXT_ON_ACCENT};}}"
        f"QPushButton:pressed{{background:{ACCENT_PRESSED};color:{TEXT_ON_ACCENT};}}"
        f"QPushButton:disabled{{background:{ACCENT_DISABLED};color:{TEXT_DISABLED};}}"
    )


def secondary_button_qss() -> str:
    return (
        f"QPushButton{{background:transparent;color:{TEXT_PRIMARY};"
        f"border:1px solid {BORDER};border-radius:6px;padding:10px 24px;}}"
        f"QPushButton:hover{{background:{HOVER_WASH};color:{TEXT_PRIMARY};"
        f"border-color:{BORDER};}}"
        f"QPushButton:pressed{{background:{PRESS_WASH};color:{TEXT_PRIMARY};}}"
        f"QPushButton:disabled{{background:transparent;color:{TEXT_DISABLED};"
        f"border-color:{BORDER_FAINT};}}"
    )


# Lower-emphasis than secondary: no border, muted text -- for de-emphasized
# actions like "Skip" that shouldn't compete with the primary/secondary pair.
def ghost_button_qss() -> str:
    return (
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
    btn.setStyleSheet(primary_button_qss())
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def make_secondary(btn: QPushButton) -> None:
    """Style btn as the secondary (outlined) action. See make_primary()."""
    btn.setProperty("class", "secondary")
    btn.setStyleSheet(secondary_button_qss())
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def make_ghost(btn: QPushButton) -> None:
    """Style btn as a low-emphasis, borderless action (e.g. "Skip"). See
    make_primary()."""
    btn.setProperty("class", "ghost")
    btn.setStyleSheet(ghost_button_qss())
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def footer_qss() -> str:
    return f"background:{BG1};border-top:1px solid {BORDER};"


def style_footer(widget: QWidget) -> None:
    """Apply the footer/nav-bar treatment: BG1 fill + a 1px top border so it
    visually separates from the body above it."""
    widget.setStyleSheet(footer_qss())


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


def tint(hex_color: str, alpha: float) -> str:
    """A token at partial opacity, for a tinted surface or a hairline.

    The public spelling of the private helper above. Use it instead of
    writing `rgba(94,234,212,0.15)` -- that literal is the dark accent, and
    on a light surface it is a barely-there wash of a colour that is no
    longer the accent at all."""
    return _rgba(hex_color, alpha)


def wash(alpha: float) -> str:
    """A translucent overlay in the palette's OWN ink direction: white on a
    dark surface, black on a light one.

    Hover and press states all over the app were written as
    `rgba(255,255,255,0.06)`. On paper that is invisible -- the same gesture
    has to go the other way, and only this module knows which way that is."""
    ink = "#ffffff" if POLARITY == "dark" else "#000000"
    return _rgba(ink, alpha)


def _scrollbar_qss() -> str:
    bar = SCROLLBAR_WIDTH + 2 * _SCROLLBAR_MARGIN
    radius = SCROLLBAR_WIDTH // 2
    # The handle is a control, not decoration: WCAG 1.4.11 wants 3:1
    # against the page behind it. A 35% wash of ICON_IDLE clears that on
    # the dark surfaces but not on paper, so the light palette leans on
    # the token harder rather than on a second colour.
    base = 0.35 if POLARITY == "dark" else 0.60
    rest, hover, pressed = (_rgba(ICON_IDLE, base), _rgba(ICON_IDLE, base + 0.20),
                            _rgba(ACCENT, 0.70))
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
    subcontrol-origin: margin; height: 0px; width: 0px;
    border: none; background-color: transparent; image: none;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    subcontrol-origin: margin; width: 0px; height: 0px;
    border: none; background-color: transparent; image: none;
}}
QScrollBar::up-arrow, QScrollBar::down-arrow, QScrollBar::left-arrow, QScrollBar::right-arrow {{
    width: 0px; height: 0px; border: none; background-color: transparent; image: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}
"""


_SCROLLBAR_MARKER = "/* samsara-scrollbars */"


def install_app_scrollbars(app=None) -> None:
    """Add SCROLLBAR_QSS to the QApplication stylesheet once (idempotent), so
    windows without a sheet of their own inherit the same scrollbars."""
    if app is None:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
        app = QApplication.instance()
    if app is None:
        return
    _ensure_system_theme_monitor(app)
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
QPushButton[class="secondary"]:hover {{ background-color: {HOVER_WASH}; border-color: {BORDER}; }}
QPushButton[class="secondary"]:pressed {{ background-color: {PRESS_WASH}; }}
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
    border: 1px solid {_rgba(ACCENT, 0.25)};
    border-radius: 8px;
}}
QFrame[class="successBanner"] {{
    background-color: {_rgba(ACCENT, 0.08)};
    border: 1px solid {_rgba(ACCENT, 0.3)};
    border-radius: 8px;
}}
QFrame[class="guideCard"] {{
    background-color: {BG1};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame[class="guideCard"]:hover {{ border-color: {_rgba(ACCENT, 0.4)}; }}
QFrame[class="tipFrame"] {{
    background-color: {_rgba(ACCENT, 0.08)};
    border: 1px solid {_rgba(ACCENT, 0.25)};
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
""" + _scrollbar_qss()


# ---------------------------------------------------------------------------
# Bind the default palette. samsara.ui.theme_boot calls set_theme() with the
# user's `ui.theme` before the app builds a single window; importing this
# module on its own (a test, a tool) gets dark, the shipped default.
# ---------------------------------------------------------------------------
_install(DEFAULT_THEME)
