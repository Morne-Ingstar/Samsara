"""Queue 129: contrast is the acceptance gate for a palette, not taste.

Astra's Home review found the old palette's worst text pair at 4.79:1 --
passing WCAG AA by 0.29. A palette tuned by eye lands there, and the next
tweak lands below it without anyone noticing. So every pair that renders text
is measured here, in both palettes, and the numbers are printed by
`pytest -s` so a reviewer can read the table rather than trust a green tick.

The gates, and where each comes from:

  * Normal text, WCAG 2.2 SC 1.4.3 AA: 4.5:1. Applies to every TEXT token on
    every surface token, in both palettes.
  * Non-text contrast, SC 1.4.11: 3:1. Applies to the tokens that draw a
    control or a mark rather than a word -- the recording mark, the scrollbar
    handle -- against the surface they are drawn on.
  * Disabled text is exempt. SC 1.4.3 exempts inactive components, and a
    disabled control that reads as enabled is its own accessibility bug.
  * State colours must stay DISTINGUISHABLE from each other, which contrast
    ratio cannot measure -- it is luminance-only, so two obviously different
    hues of the same lightness score about 1.0. Perceptual distance (CIE76
    dE in Lab) is the right instrument and is what this file uses.
"""

from __future__ import annotations

import itertools

import pytest

from samsara.ui import theme

#: Pairs where the token is drawn as WORDS. ICON_IDLE is on this list on
#: purpose: its name says "icon", but it is the muted note colour under
#: half the rows in Settings.
TEXT_TOKENS = (
    "TEXT_PRIMARY", "TEXT_SECONDARY", "ACCENT",
    "SUCCESS", "WARNING", "ERROR", "AVA", "ICON_IDLE",
)
SURFACES = ("BG0", "BG1", "BG2")

#: Drawn as a shape, not a word: WCAG's 3:1 non-text gate applies.
GRAPHIC_TOKENS = ("RECORDING",)

#: RECORDING is the live-capture mark. It is drawn on the window and on cards
#: -- never on an input surface -- and in the dark palette it does not clear
#: 3:1 against BG2 (2.99). Rather than quietly widen the gate, the surfaces it
#: is actually drawn on are named.
GRAPHIC_SURFACES = {"RECORDING": ("BG0", "BG1")}

AA_NORMAL_TEXT = 4.5
NON_TEXT = 3.0
#: CIE76 dE. 2.3 is the "just noticeable difference"; a state colour has to be
#: far past that to be told apart at a glance on a moving indicator. 20 is the
#: dark palette's own closest pair rounded down -- ERROR and RECORDING, which
#: are both red BY DESIGN (theme.py: "red is never decorative: it means
#: live/recording or failure"). The light palette is held to the same
#: separation so the two reds do not collapse into one there.
STATE_DISTANCE = 20.0


@pytest.fixture(params=("dark", "light"))
def palette(request):
    """Bind one palette for the duration of a test, then put dark back."""
    previous = theme.active_theme()
    theme.set_theme(request.param, refresh=False)
    yield request.param
    theme.set_theme(previous, refresh=False)


def _lab(rgb):
    """CIE Lab under D65, from sRGB. Enough for a perceptual distance."""
    def linear(channel):
        c = channel / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.00000
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _distance(hex_a: str, hex_b: str) -> float:
    a, b = _lab(theme._hex_to_rgb(hex_a)), _lab(theme._hex_to_rgb(hex_b))
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


# --- the gate -------------------------------------------------------------------

def test_every_text_pair_meets_wcag_aa(palette, capsys):
    rows, failures = [], []
    for token in TEXT_TOKENS:
        for surface in SURFACES:
            ratio = theme.contrast_ratio(getattr(theme, token), getattr(theme, surface))
            rows.append((ratio, token, surface))
            if ratio < AA_NORMAL_TEXT:
                failures.append((ratio, token, surface))
    with capsys.disabled():
        print(f"\n  {palette} palette -- text contrast (AA = {AA_NORMAL_TEXT}:1)")
        for ratio, token, surface in sorted(rows):
            mark = "AAA" if ratio >= 7 else "AA "
            print(f"    {ratio:6.2f}  {mark}  {token:<15} on {surface}")
    assert not failures, "\n".join(
        f"{t} on {s} is {r:.2f}:1 in the {palette} palette" for r, t, s in failures)


#: The accent fill and its interaction states carry the primary button's
#: label, so the ink for them is a named token rather than a computed one.
ACCENT_FILLS = ("ACCENT", "ACCENT_HOVER", "ACCENT_PRESSED")

#: Every token the app ever fills a control with and then writes on.
TEXT_BEARING_FILLS = ACCENT_FILLS + ("RECORDING",)


def test_text_on_the_accent_fill_meets_wcag_aa(palette):
    for fill in ACCENT_FILLS:
        ratio = theme.contrast_ratio(theme.TEXT_ON_ACCENT, getattr(theme, fill))
        assert ratio >= AA_NORMAL_TEXT, (
            f"TEXT_ON_ACCENT on {fill} is {ratio:.2f}:1 in the {palette} palette")


def test_ink_on_picks_an_accessible_ink_for_every_fill(palette):
    """ink_on() must clear AA on every fill the app writes on, and its answer
    for the accent must agree with the named TEXT_ON_ACCENT token -- a token
    and a measurement that disagree is a bug in one of them."""
    for fill in TEXT_BEARING_FILLS:
        value = getattr(theme, fill)
        ratio = theme.contrast_ratio(theme.ink_on(value), value)
        assert ratio >= AA_NORMAL_TEXT, (
            f"ink_on({fill}) is only {ratio:.2f}:1 in the {palette} palette")
    assert theme.ink_on(theme.ACCENT) == theme.TEXT_ON_ACCENT


def test_ink_on_changes_its_mind_within_one_palette(palette):
    """The reason ink_on() exists rather than a TEXT_ON_STATE token: in the
    dark palette the states are not all the same lightness, so one fixed ink
    is unreadable on some of them. If this ever stops being true, a token is
    simpler and this function should go."""
    if palette != "dark":
        pytest.skip("the dark palette is the one with both light and dark states")
    assert theme.ink_on(theme.ERROR) != theme.ink_on(theme.RECORDING)


def test_non_text_marks_meet_the_graphics_gate(palette):
    for token in GRAPHIC_TOKENS:
        for surface in GRAPHIC_SURFACES[token]:
            ratio = theme.contrast_ratio(getattr(theme, token), getattr(theme, surface))
            assert ratio >= NON_TEXT, (
                f"{token} on {surface} is {ratio:.2f}:1 in the {palette} palette")


def test_the_surface_ladder_stays_visible(palette):
    """BG0 -> BG1 -> BG2 must be three steps a user can actually see; a
    palette whose card is indistinguishable from its window has no cards."""
    for lower, upper in (("BG0", "BG1"), ("BG1", "BG2")):
        ratio = theme.contrast_ratio(getattr(theme, lower), getattr(theme, upper))
        assert ratio >= 1.05, (
            f"{lower} and {upper} are {ratio:.3f}:1 apart in {palette}")


def test_state_colours_stay_distinguishable_from_each_other(palette, capsys):
    states = ("ACCENT", "SUCCESS", "WARNING", "ERROR", "RECORDING", "AVA", "ICON_IDLE")
    rows, failures = [], []
    for a, b in itertools.combinations(states, 2):
        distance = _distance(getattr(theme, a), getattr(theme, b))
        rows.append((distance, a, b))
        if distance < STATE_DISTANCE:
            failures.append((distance, a, b))
    with capsys.disabled():
        print(f"\n  {palette} palette -- state separation (dE >= {STATE_DISTANCE})")
        for distance, a, b in sorted(rows)[:5]:
            print(f"    {distance:6.1f}  {a} vs {b}   (closest pairs)")
    assert not failures, "\n".join(
        f"{a} and {b} are only dE {d:.1f} apart in {palette}" for d, a, b in failures)


# --- the palettes themselves ----------------------------------------------------

def test_both_palettes_define_exactly_the_same_tokens():
    assert set(theme.PALETTES) == {"dark", "light"}
    dark, light = theme.PALETTES["dark"], theme.PALETTES["light"]
    assert set(dark) == set(light), set(dark) ^ set(light)
    assert set(dark) == set(theme.PALETTE_TOKENS)


def test_dark_is_the_default_and_is_unchanged():
    """The shipped app must not move. Every screenshot, every doc and every
    existing test describes this palette."""
    assert theme.DEFAULT_THEME == "dark"
    dark = theme.PALETTES["dark"]
    assert dark["BG0"] == "#0b0e14"
    assert dark["BG1"] == "#131820"
    assert dark["BG2"] == "#1a2030"
    assert dark["ACCENT"] == "#5cc4d4"
    assert dark["TEXT_PRIMARY"] == "#e4e8ef"
    assert dark["TEXT_SECONDARY"] == "rgba(255,255,255,0.75)"
    assert dark["SUCCESS"] == "#6ee7a0"
    assert dark["ERROR"] == "#f87171"
    assert dark["WARNING"] == "#fbbf24"
    assert dark["RECORDING"] == "#c0392b"
    assert dark["AVA"] == "#a78bfa"
    assert dark["ICON_IDLE"] == "#8b929c"


def test_the_two_palettes_actually_point_opposite_ways():
    """A "light" palette whose window is darker than its text is a typo, and
    every contrast assertion above would still pass."""
    for name, lighter_surface in (("dark", False), ("light", True)):
        palette = theme.PALETTES[name]
        surface = theme._relative_luminance(theme._hex_to_rgb(palette["BG0"]))
        ink = theme._relative_luminance(theme.composite(palette["TEXT_PRIMARY"],
                                                        palette["BG0"]))
        assert (surface > ink) is lighter_surface, name


def test_disabled_text_is_the_one_documented_exception(palette):
    """Not an oversight: WCAG 2.2 SC 1.4.3 exempts inactive components, and a
    disabled control that reads as enabled is its own accessibility bug. The
    test pins the intent so nobody 'fixes' it into an enabled-looking grey."""
    for surface in SURFACES:
        ratio = theme.contrast_ratio(theme.TEXT_DISABLED, getattr(theme, surface))
        assert ratio < AA_NORMAL_TEXT, (
            f"TEXT_DISABLED now reads as live text on {surface} in {palette} "
            f"({ratio:.2f}:1) -- if that is intended, delete this test")
        assert ratio >= 2.0, (
            f"TEXT_DISABLED is invisible on {surface} in {palette} ({ratio:.2f}:1)")
