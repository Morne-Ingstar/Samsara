"""Acceptance checks for the fixed, taskbar-readable brand icon family."""

import struct
from pathlib import Path

from PySide6.QtGui import QImage


REPO = Path(__file__).resolve().parents[1]
BRAND = REPO / "assets" / "brand"
SIZES = (16, 20, 24, 32, 48, 256)


def _ico_sizes(path: Path) -> list[int]:
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1)
    return [256 if (width := data[6 + index * 16]) == 0 else width
            for index in range(count)]


def _linear(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _contrast(pixel: int, background: str) -> float:
    from PySide6.QtGui import QColor
    fg, bg = QColor.fromRgba(pixel), QColor(background)
    fg_l = 0.2126 * _linear(fg.red()) + 0.7152 * _linear(fg.green()) + 0.0722 * _linear(fg.blue())
    bg_l = 0.2126 * _linear(bg.red()) + 0.7152 * _linear(bg.green()) + 0.0722 * _linear(bg.blue())
    return (max(fg_l, bg_l) + 0.05) / (min(fg_l, bg_l) + 0.05)


def _right_ring_width(image: QImage) -> int:
    """Opaque run through the outer-right ring, its least forgiving segment."""
    x = image.width() - max(2, round(image.width() * 0.13))
    runs, current = [], 0
    for y in range(image.height()):
        if (image.pixel(x, y) >> 24) & 0xFF >= 180:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return max(runs, default=0)


def test_ico_contains_exact_taskbar_and_app_sizes():
    assert _ico_sizes(BRAND / "samsara.ico") == list(SIZES)
    assert all((BRAND / f"samsara_{size}.png").is_file() for size in SIZES)


def test_small_icons_keep_a_two_pixel_ring_and_a_solid_eye():
    for size in (16, 20, 24):
        image = QImage(str(BRAND / f"samsara_{size}.png"))
        assert not image.isNull()
        assert _right_ring_width(image) >= 2, size
        # Centre is an opaque, filled eye—not a hairline at taskbar sizes.
        assert (image.pixel(size // 2, size // 2) >> 24) & 0xFF == 255


def test_outline_or_cyan_pixels_clear_three_to_one_on_dark_and_light_taskbars():
    for size in SIZES:
        image = QImage(str(BRAND / f"samsara_{size}.png"))
        pixels = [image.pixel(x, y) for y in range(size) for x in range(size)
                  if (image.pixel(x, y) >> 24) & 0xFF == 255]
        assert max(_contrast(pixel, "#0b0e14") for pixel in pixels) >= 3.0, size
        assert max(_contrast(pixel, "#ffffff") for pixel in pixels) >= 3.0, size


def test_frozen_window_and_idle_tray_use_the_generated_brand_icon():
    spec = (REPO / "scripts" / "samsara.spec").read_text(encoding="utf-8")
    tray = (REPO / "samsara" / "ui" / "tray_qt.py").read_text(encoding="utf-8")
    app = (REPO / "dictation.py").read_text(encoding="utf-8")
    assert "assets' / 'brand' / 'samsara.ico" in spec
    assert "assets', 'brand', 'samsara.ico" in app
    assert "idle_brand_icon_path" in tray and "frame.capture == \"idle\"" in tray
    assert 'SetCurrentProcessExplicitAppUserModelID(\n            "MorneIngstar.Samsara"' in app
