"""Regression gates for generated tray icons (Prompt 207)."""
import importlib.util
import copy
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

from samsara.ui import tray_qt


REPO = Path(__file__).resolve().parent.parent
STATES_DIR = REPO / "assets" / "icon" / "states"
SVG_PATH = REPO / "assets" / "icon" / "samsara.svg"
TRAY_SIZES = (16, 24, 32, 48, 64)
# 879a: approved calm artwork footprints, retaining the four-pixel tolerance.
MIN_OPAQUE_PIXELS = {16: 146, 24: 287, 32: 468, 48: 956, 64: 1620}


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
    # 879a keeps capture-table names as runtime vocabulary; all resolve to
    # the approved ring, never the old width-generated segment drawings.
    assert {"regular", "small", "ring", "eye", "ring-small", "eye-small"} <= available
    for brand, table in ((False, tray_qt.mark_capture()), (True, tray_qt.brand_capture())):
        for capture in table:
            for small in (False, True):
                rendered = ET.fromstring(tray_qt.mark_svg(capture, "off", small, "ring", brand=brand))
                visible = "small" if small and not brand and capture != "recording" else "regular"
                layer = next(el for el in rendered if el.get("id") == visible)
                assert layer.get("display") == "inline"
                ring, eye = list(layer)
                assert ring.get("display") == "inline"
                assert eye.get("display") == "none"
                original = next(el for el in root if el.get("id") == visible)
                assert ring[0].get("d") == original[0][0].get("d")


def test_generator_fails_loudly_when_a_required_mark_id_is_absent():
    generator = _generator_module()
    broken = SVG_PATH.read_text(encoding="utf-8").replace(
        ' id="ring-small"', ' id="missing-ring-small"', 1)
    with pytest.raises(ValueError, match="ring-small"):
        generator.require_mark_ids(broken)


def test_every_state_matches_complete_calm_art_and_rotates_only_the_ring(qapp):
    """Independent SVG fixture: no generated assets or runtime SVG composition."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    source = ET.parse(SVG_PATH).getroot()
    regular = next(el for el in source if el.get("id") == "regular")
    for capture, eye in tray_qt.MARK_STATES.values():
        for size in (16, 24, 48):
            for angle in (0, 45, 90):
                expected = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
                expected.fill(Qt.GlobalColor.transparent)
                painter = QPainter(expected)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                for part, colour in zip(regular, tray_qt.mark_colours(capture, eye)):
                    root = ET.Element(source.tag, {"viewBox": source.get("viewBox")})
                    element = copy.deepcopy(part)
                    element.set("fill", colour)
                    root.append(element)
                    renderer = QSvgRenderer(ET.tostring(root))
                    painter.save()
                    if part.get("id") == "ring":
                        painter.translate(size / 2, size / 2)
                        painter.rotate(angle)
                        painter.translate(-size / 2, -size / 2)
                    renderer.render(painter, QRectF(0, 0, size, size))
                    painter.restore()
                painter.end()
                actual = tray_qt.render_mark(capture, eye, size, angle)
                assert actual == expected.convertToFormat(QImage.Format.Format_ARGB32), (capture, eye, size, angle)
