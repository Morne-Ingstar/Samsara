"""The recording snake keeps its head/tail; only small bodies gain weight."""
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from samsara.ui import theme, tray_qt
from samsara.ui.listening_indicator import ListeningIndicator


def visible_ring(svg):
    root = ET.fromstring(svg)
    group = next(node for node in root if node.get('display', 'inline') == 'inline')
    return next(node for node in group if node.get('display', 'inline') == 'inline')


@pytest.mark.parametrize('palette', ('dark', 'light'))
@pytest.mark.parametrize('brand', (False, True))
@pytest.mark.parametrize('size', (32, 36, 48))
@pytest.mark.parametrize('rotation', (0, 45, 135))
def test_large_recording_matches_listening_geometry_and_alpha(qapp, monkeypatch, palette, brand, size, rotation):
    previous = theme.active_theme()
    rings = []
    original = tray_qt.mark_svg

    def capture(*args, **kwargs):
        svg = original(*args, **kwargs)
        if args[3] == 'ring':
            rings.append(visible_ring(svg))
        return svg

    monkeypatch.setattr(tray_qt, 'mark_svg', capture)
    try:
        # Renderer palette coverage must not retheme unrelated live widgets.
        theme.set_theme(palette, refresh=False)
        tray_qt.clear_mark_caches()
        listening = tray_qt.render_mark('listening', 'armed', size, rotation, brand=brand)
        recording = tray_qt.render_mark('recording', 'armed', size, rotation, brand=brand)
        assert [node.attrib for node in rings[-2]] == [node.attrib for node in rings[-1]]
        assert rings[-1].get('fill') == theme.RECORDING
        assert listening != recording
        assert [listening.pixelColor(x, y).alpha() for y in range(size) for x in range(size)] == [
            recording.pixelColor(x, y).alpha() for y in range(size) for x in range(size)]
    finally:
        theme.set_theme(previous, refresh=False)


@pytest.mark.parametrize('weight', (tray_qt.RING_LINE_WIDTH, tray_qt.RING_BRAND_WIDTH))
@pytest.mark.parametrize('segment', (0, 1, 2))
def test_small_body_is_midpoint_width_with_identical_head_tail_and_joints(weight, segment):
    ordinary = tray_qt.ring_stroke_outline(segment, weight)
    thicker = tray_qt.ring_stroke_outline(segment, weight, 1.5)
    samples = tray_qt.ring_centreline(segment, weight)
    assert len(ordinary) == len(thicker)
    for index, (u, _angle, x, y) in enumerate(samples):
        protected = (u <= (tray_qt.tail_fraction(weight) if segment == tray_qt.TAIL_SEGMENT else 0.08)
                     or u >= (1 - tray_qt.HEAD_FRACTION if segment == tray_qt.HEAD_SEGMENT else 0.92))
        if protected:
            assert ordinary[index] == thicker[index]
        if 0.57 <= u <= 0.62:
            assert math.dist(thicker[index], (x, y)) == pytest.approx(1.5 * math.dist(ordinary[index], (x, y)))
    cap_points = tray_qt._CAP_SAMPLES - 1
    if segment == tray_qt.HEAD_SEGMENT:
        assert ordinary[len(samples):len(samples)+cap_points] == thicker[len(samples):len(samples)+cap_points]
    if segment == tray_qt.TAIL_SEGMENT:
        assert ordinary[-cap_points:] == thicker[-cap_points:]


@pytest.mark.parametrize('size', (16, 20, 24))
@pytest.mark.parametrize('brand', (False, True))
@pytest.mark.parametrize('scale', (1.0, 1.5))
def test_small_recording_uses_regular_head_and_tail_in_red_at_both_scales(qapp, monkeypatch, size, brand, scale):
    seen = []
    original = tray_qt.mark_svg

    def capture(*args, **kwargs):
        svg = original(*args, **kwargs)
        if args[3] == 'ring':
            seen.append(visible_ring(svg))
        return svg

    monkeypatch.setattr(tray_qt, 'mark_svg', capture)
    tray_qt.clear_mark_caches()
    pixels = round(size * scale)
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.scale(scale, scale)
        tray_qt.paint_mark(painter, QRectF(0, 0, size, size), 'recording', 'off', 45, brand=brand)
    finally:
        painter.end()
    ring = seen[-1]
    assert ring.get('id') == ('ring-brand' if brand else 'ring-hollow')
    assert ring.get('fill') == theme.RECORDING
    base = visible_ring(original('recording', 'off', False, 'ring', brand=brand))
    assert ring[-1].attrib == base[-1].attrib  # the approved jaw is unchanged
    weight = tray_qt.RING_BRAND_WIDTH if brand else tray_qt.RING_LINE_WIDTH
    assert [node.get('d') for node in ring if node.get('data-role') == 'segment'] == [
        tray_qt.ring_segment_path_data(i, weight, 1.0 if brand else 1.5) for i in range(3)]
    assert any(image.pixelColor(x, y).alpha() > 0 for x in range(pixels) for y in range(pixels))


@pytest.mark.parametrize('size', (16, 24, 32, 48, 64))
def test_generated_recording_assets_match_live_regular_snake(qapp, size):
    previous = theme.active_theme()
    try:
        theme.set_theme('dark', refresh=False)
        tray_qt.clear_mark_caches()
        actual = tray_qt.render_mark('recording', 'off', size, small_max=tray_qt.TASKBAR_SMALL_MAX)
        expected = QImage(str(Path(__file__).resolve().parents[1] /
                             f'assets/icon/states/samsara_recording_{size}.png'))
        from tools.gen_icons import image_diff
        comparison = image_diff(actual, expected)
        assert comparison['matches'], comparison
        assert actual == tray_qt.render_mark('recording', 'off', size, small_max=0)
    finally:
        theme.set_theme(previous, refresh=False)


def test_renderer_cache_separates_small_body_from_regular(qapp):
    tray_qt.clear_mark_caches()
    small = tray_qt._renderer('recording', 'off', False, 'ring', body_scale=1.5)
    regular = tray_qt._renderer('recording', 'off', False, 'ring')
    assert small is not regular
    assert tray_qt._renderer('recording', 'off', False, 'ring', body_scale=1.5) is small
    assert tray_qt._renderer('recording', 'off', False, 'ring') is regular


@pytest.mark.parametrize('armed', (False, True))
@pytest.mark.parametrize('session', (None, 'DICTATE', 'AVA'))
def test_compact_capture_selects_recording_without_changing_eye_or_spin(
        qapp, armed, session, qt_widget_scope):
    with qt_widget_scope(ListeningIndicator()) as widget:
        widget._wake_armed = armed
        widget._session_mode_name = session
        widget._listening = True
        widget._spin = lambda _pace: 37.0
        capture, eye, rotation, opacity = widget._base_glyph()
        assert capture == 'recording'
        assert eye == ('armed' if armed and session is None else 'off')
        assert (rotation, opacity) == (37.0, 1.0)
