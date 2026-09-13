"""One visual identity (owner decision 2026-09-13): cyan brand, red = recording,
eye = hands-free. Tokens, the single-source SVG, and the generated icon set."""

import importlib.util
import re
import struct
from pathlib import Path

import pytest

from samsara.ui import theme

REPO = Path(__file__).resolve().parent.parent
HEX = re.compile(r"^#[0-9a-f]{6}$")


@pytest.fixture(scope="module")
def gen_icons():
    spec = importlib.util.spec_from_file_location("gen_icons", REPO / "tools" / "gen_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_accent_one_semantic_tokens():
    assert theme.ACCENT == "#5cc4d4"
    assert theme.RECORDING == "#c0392b"
    assert theme.BRAND_RED == theme.RECORDING       # retired brand name, recording role
    assert theme.ICON_HUB == theme.BG1
    for token in (theme.RECORDING, theme.AVA, theme.ICON_IDLE, theme.ICON_HUB):
        assert HEX.match(token), token
    assert len({theme.ACCENT, theme.RECORDING, theme.AVA, theme.ICON_IDLE}) == 4
    assert not [name for name in vars(theme) if "GOLD" in name.upper()]


def test_svg_colours_are_all_theme_tokens():
    svg = (REPO / "assets" / "icon" / "samsara.svg").read_text(encoding="utf-8")
    body = svg[svg.index("<svg"):]
    colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', body))
    tokens = {theme.ACCENT, theme.RECORDING, theme.AVA, theme.ICON_IDLE, theme.ICON_HUB}
    assert colours and colours <= tokens


def test_generated_assets_are_up_to_date(qapp, gen_icons):
    assert gen_icons.stale_assets() == []


def test_ico_holds_every_app_size(gen_icons):
    data = (REPO / "assets" / "icon" / "samsara.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    assert gen_icons._ico_sizes(data) == [16, 24, 32, 48, 64, 128, 256]
    assert count == 7


def _opaque(image, x, y):
    return image.pixelColor(x, y).alpha() > 128


def _eye_pixels(image, gen_icons):
    """Hub pixels that are not the hub disc colour: the eye's SHAPE."""
    hub = theme._hex_to_rgb(theme.ICON_HUB)
    size = image.width()
    centre, radius = size / 2, size * 0.22
    marks = set()
    for y in range(size):
        for x in range(size):
            if (x + 0.5 - centre) ** 2 + (y + 0.5 - centre) ** 2 > radius ** 2:
                continue
            c = image.pixelColor(x, y)
            if c.alpha() > 128 and max(abs(c.red() - hub[0]), abs(c.green() - hub[1]),
                                       abs(c.blue() - hub[2])) > 40:
                marks.add((x, y))
    return marks


@pytest.mark.parametrize("size", [16, 32])
def test_state_is_carried_by_shape_not_colour_alone(qapp, gen_icons, size):
    idle = gen_icons.render("idle", size)
    recording = gen_icons.render("recording", size)
    ring_idle = sum(_opaque(idle, x, y) for x in range(size) for y in range(size))
    ring_rec = sum(_opaque(recording, x, y) for x in range(size) for y in range(size))
    assert ring_rec > ring_idle          # filled segments, not just a red outline

    asleep = _eye_pixels(gen_icons.render("asleep", size), gen_icons)
    armed = _eye_pixels(gen_icons.render("armed", size), gen_icons)
    heard = _eye_pixels(gen_icons.render("heard", size), gen_icons)
    assert asleep and armed and heard
    assert asleep != armed != heard
    assert len(heard) > len(armed) or size > 16   # 16 px: filled ellipse vs dot-in-ellipse


def test_16px_uses_the_simplified_drawing(gen_icons):
    small = gen_icons.state_svg("armed", small=True).decode("utf-8")
    regular = gen_icons.state_svg("armed", small=False).decode("utf-8")
    assert 'id="small" display="inline"' in small and 'id="regular" display="none"' in small
    assert 'id="regular" display="inline"' in regular and 'id="small" display="none"' in regular


def test_montage_renders(qapp, gen_icons, tmp_path):
    out = gen_icons.write_montage(tmp_path / "icon_states.png")
    assert out.exists() and out.stat().st_size > 0
