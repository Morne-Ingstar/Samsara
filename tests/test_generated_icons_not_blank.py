"""Regression gates for generated tray icons (Prompt 207)."""
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

from samsara.ui import tray_qt


REPO = Path(__file__).resolve().parent.parent
STATES_DIR = REPO / "assets" / "icon" / "states"
SVG_PATH = REPO / "assets" / "icon" / "samsara.svg"
TRAY_SIZES = (16, 24, 32, 48, 64)
# The current non-Ava baseline bottoms out at 94 opaque pixels (idle/off at
# 16 px).  Leave four pixels of renderer tolerance while rejecting blanks.
MIN_OPAQUE_PIXELS = 90


def _opaque_pixels(path: Path) -> int:
    with Image.open(path).convert("RGBA") as image:
        return sum(alpha > 0 for *_rgb, alpha in image.getdata())


def _generator_module():
    path = REPO / "tools" / "gen_icons.py"
    spec = importlib.util.spec_from_file_location("gen_icons_207", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_generated_state_and_size_has_visible_pixels():
    for state in tray_qt.MARK_STATES:
        for size in TRAY_SIZES:
            path = STATES_DIR / f"samsara_{state}_{size}.png"
            assert _opaque_pixels(path) > MIN_OPAQUE_PIXELS, path


def test_every_capture_table_ring_resolves_regular_and_small():
    root = ET.parse(SVG_PATH).getroot()
    available = {element.get("id") for element in root.iter() if element.get("id")}
    for table in (tray_qt.mark_capture(), tray_qt.brand_capture()):
        for _colour, ring in table.values():
            for suffix in ("", "-small"):
                assert ring + suffix in available


def test_generator_fails_loudly_when_a_required_mark_id_is_absent():
    generator = _generator_module()
    broken = SVG_PATH.read_text(encoding="utf-8").replace(
        ' id="ring-brand-small"', ' id="missing-brand-small"', 1)
    with pytest.raises(ValueError, match="ring-brand-small"):
        generator.require_mark_ids(broken)
