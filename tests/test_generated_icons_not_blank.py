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
# Approved articulated artwork uses 80 visible pixels for recording at 16px.
# Retain the 90px floor at larger sizes and the same four-pixel tolerance
# for the 80px 16px artwork. The source-render alpha mask is checked too.
MIN_OPAQUE_PIXELS = {16: 76, 24: 90, 32: 90, 48: 90, 64: 90}


def _opaque_pixels(path: Path) -> int:
    with Image.open(path).convert("RGBA") as image:
        return sum(alpha > 0 for *_rgb, alpha in image.getdata())


def _generator_module():
    path = REPO / "tools" / "gen_icons.py"
    spec = importlib.util.spec_from_file_location("gen_icons_207", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_generated_state_and_size_has_visible_pixels(qapp):
    from PySide6.QtGui import QImage
    generator = _generator_module()
    for state in tray_qt.MARK_STATES:
        for size in TRAY_SIZES:
            path = STATES_DIR / f"samsara_{state}_{size}.png"
            assert _opaque_pixels(path) > MIN_OPAQUE_PIXELS[size], path
            generated = QImage(str(path))
            reference = generator.render(state, size)
            assert generated.size() == reference.size(), path
            # A nonblank but wrong/truncated state must fail too. Allow one
            # alpha step for Qt patch-version antialias rounding.
            assert max(abs(generated.pixelColor(x, y).alpha() - reference.pixelColor(x, y).alpha())
                       for y in range(size) for x in range(size)) <= 1, path


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
