"""One visual identity: articulated cyan body, warm brand head, solid red = recording,
open centre eye = armed. Tokens, the single-source SVG, the one drawing routine
(samsara.ui.tray_qt.render_mark), the generated icon set, the surfaces that
consume it, and the taskbar AUMID. dictation.py is read as source, never
imported, here."""

import ast
import importlib.util
import io
import json
import math
import re
import struct
import sys
import tokenize
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from samsara.ui import theme
from samsara.ui import tray_qt

REPO = Path(__file__).resolve().parent.parent
HEX = re.compile(r"^#[0-9a-f]{6}$")
HEX_IN_TEXT = re.compile(r"#[0-9a-fA-F]{6}\b")
NAMED_STATES = {"off", "asleep", "idle", "listening", "recording", "ava", "armed", "heard"}


@pytest.fixture(scope="module")
def gen_icons():
    spec = importlib.util.spec_from_file_location("gen_icons", REPO / "tools" / "gen_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Tokens and the SVG
# ---------------------------------------------------------------------------

def test_one_accent_one_semantic_tokens():
    assert theme.ACCENT == "#5cc4d4"
    assert theme.RECORDING == "#c0392b"
    assert theme.BRAND_RED == theme.RECORDING       # retired brand name, recording role
    for token in (theme.RECORDING, theme.AVA, theme.ICON_IDLE):
        assert HEX.match(token), token
    assert len({theme.ACCENT, theme.RECORDING, theme.AVA, theme.ICON_IDLE}) == 4
    assert not [name for name in vars(theme) if "GOLD" in name.upper()]
    # Surfaces stay on the existing cool ladder (owner decision 6).
    assert (theme.BG0, theme.BG1, theme.BG2) == ("#0b0e14", "#131820", "#1a2030")


def test_svg_has_no_hub_disc_and_only_token_colours():
    svg = (REPO / "assets" / "icon" / "samsara.svg").read_text(encoding="utf-8")
    body = svg[svg.index("<svg"):]
    assert "hub" not in body and "<circle id=" not in body
    colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', body))
    assert colours and colours <= {theme.ACCENT, theme.RECORDING, theme.AVA, theme.ICON_IDLE}


# ---------------------------------------------------------------------------
# The state model
# ---------------------------------------------------------------------------

def test_state_map_covers_all_eight_states_with_assets():
    assert set(tray_qt.MARK_STATES) == NAMED_STATES
    for state, (capture, eye) in tray_qt.MARK_STATES.items():
        assert capture in tray_qt.mark_capture() and eye in tray_qt.MARK_EYE
        for size in (16, 24, 32, 48, 64):
            assert (REPO / "assets" / "icon" / "states" / f"samsara_{state}_{size}.png").exists()
    assert tray_qt.MARK_EYE["off"] is None                  # OFF = ring only
    assert tray_qt.APP_MARK == ("listening", "asleep")       # cyan wheel, lid closed


def test_mark_is_monochrome_per_state_and_heard_is_the_red_eye():
    for capture, (colour, _ring) in tray_qt.mark_capture().items():
        for eye in ("off", "asleep", "armed"):
            assert tray_qt.mark_colours(capture, eye) == (colour, colour)
    ring, eye = tray_qt.mark_colours("listening", "heard")
    assert eye == theme.RECORDING
    assert ring != theme.ACCENT and ring == theme._mix(theme.ACCENT, theme.TEXT_PRIMARY, tray_qt._HEARD_RING_LIFT)


def test_heard_keyframes_flash_then_close_then_hand_back():
    frames = tray_qt.HEARD_KEYFRAMES
    times = [at for at, _eye in frames]
    assert times == sorted(times) and times[0] == 0
    eyes = [eye for _at, eye in frames]
    assert eyes[0] == "heard" and "heard" in eyes[1:-2]
    assert eyes[-2] == "asleep" and eyes[-1] is None       # lid closes, then the live state
    assert 900 <= times[-2] <= 1100                          # ~1 s of flashing


def test_armed_face_is_unchanged_open_eye():
    svg = (REPO / "assets" / "icon" / "samsara.svg").read_text(encoding="utf-8")
    assert 'd="M 20,32 Q 32,21 44,32"' in svg and 'd="M 20,32 Q 32,43 44,32"' in svg
    assert '<circle data-role="pupil" cx="32" cy="32" r="2.5"' in svg
    # 16-20 px (19): a solid pupil dot, no hairline ellipse.
    assert '<circle data-role="pupil" cx="32" cy="32" r="7" fill="#8b929c" stroke="none"/>' in svg
    assert 'rx="13" ry="8.5"' not in svg


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_generated_assets_are_up_to_date(qapp, gen_icons):
    assert gen_icons.stale_assets() == []


def test_generated_assets_ignore_the_active_light_palette(qapp, gen_icons):
    previous = theme.active_theme()
    try:
        theme.set_theme("light", refresh=False)
        assert gen_icons.stale_assets() == []
        assert theme.active_theme() == "light"
    finally:
        theme.set_theme(previous, refresh=False)


def _isolated_generated_asset(gen_icons, monkeypatch, tmp_path):
    with gen_icons._asset_palette():
        image = gen_icons.render("recording", 32)
    path = tmp_path / "recording.png"
    assert image.save(str(path))
    monkeypatch.setattr(gen_icons, "REPO", tmp_path)
    monkeypatch.setattr(gen_icons, "STATES_DIR", tmp_path / "states")
    monkeypatch.setattr(gen_icons, "expected_outputs", lambda: {path: image})
    monkeypatch.setattr(gen_icons, "stale_brand_assets", lambda: [])
    return path, image


def test_generated_asset_check_accepts_sparse_channel_rounding(qapp, gen_icons, monkeypatch, tmp_path):
    path, expected = _isolated_generated_asset(gen_icons, monkeypatch, tmp_path)
    actual = expected.copy()
    x, y = next((x, y) for y in range(32) for x in range(32)
                if actual.pixelColor(x, y).alpha() == 255)
    colour = actual.pixelColor(x, y)
    colour.setRed(colour.red() - 2)
    actual.setPixelColor(x, y, colour)
    assert actual.save(str(path))
    comparison = gen_icons.image_diff(actual, expected)
    assert comparison["max_channel_delta"] == [2, 0, 0, 0]
    assert comparison["differing_pixel_count"] == 1
    assert comparison["alpha_mask_equal"] and comparison["matches"]
    assert gen_icons.main(["--check"]) == 0


@pytest.mark.parametrize("damage", ["shape", "colour", "broad_drift", "size", "unreadable"])
def test_generated_asset_check_rejects_deliberately_stale_asset(
        qapp, gen_icons, monkeypatch, tmp_path, damage):
    path, expected = _isolated_generated_asset(gen_icons, monkeypatch, tmp_path)
    actual = expected.copy()
    opaque = [(x, y) for y in range(32) for x in range(32)
              if (actual.pixelColor(x, y).alpha() > 0 if damage == "broad_drift"
                  else actual.pixelColor(x, y).alpha() == 255)]
    if damage in {"shape", "colour", "broad_drift"}:
        for x, y in opaque if damage == "broad_drift" else opaque[:1]:
            colour = actual.pixelColor(x, y)
            if damage == "shape":
                colour.setAlpha(0)
            else:
                colour.setRed(colour.red() - (3 if damage == "colour" else 1))
            actual.setPixelColor(x, y, colour)
    elif damage == "size":
        actual = actual.scaled(31, 32)
    assert actual.save(str(path))
    if damage == "unreadable":
        path.write_bytes(b"invalid PNG")
    comparison = gen_icons.image_diff(gen_icons.QImage(str(path)), expected)
    if damage == "broad_drift":
        assert comparison["max_channel_delta"] == [1, 0, 0, 0]
        assert comparison["differing_pixel_share"] > gen_icons.MAX_DIFFERING_PIXEL_SHARE
    assert not comparison["matches"], comparison
    assert gen_icons.stale_assets() == [path]
    assert gen_icons.main(["--check"]) == 1


def test_icon_diagnostics_preserve_sources_and_include_ico_frames(qapp, gen_icons, monkeypatch, tmp_path):
    def no_source_writes():
        pytest.fail("diagnostics must not regenerate into the repository")

    monkeypatch.setattr(gen_icons, "write_assets", no_source_writes)
    assert gen_icons.main(["--diagnostics", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "pixel-diff.json").read_text(encoding="utf-8"))
    assert report["runtime"]["qt"]
    files = report["files"]
    frames = files["assets/icon/samsara.ico"]["frames"]
    assert set(frames) == {str(size) for size in gen_icons.APP_SIZES}
    assert all(frame["matches"] for frame in frames.values())
    for name, comparison in files.items():
        assert (tmp_path / "committed" / name).read_bytes() == (REPO / name).read_bytes()
        assert (tmp_path / "generated" / name).exists()
        if name.endswith(".png"):
            assert comparison["matches"], (name, comparison)
            assert comparison["alpha_mask_equal"]
            assert comparison["max_channel_delta"] is not None
            assert comparison["differing_pixel_count"] is not None


def test_ico_holds_every_app_size(gen_icons):
    data = (REPO / "assets" / "icon" / "samsara.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    assert gen_icons._ico_sizes(data) == [16, 24, 32, 48, 64, 128, 256]
    assert count == 7


def test_gen_icons_draws_with_the_shared_routine(gen_icons):
    assert gen_icons.render_mark is tray_qt.render_mark
    assert not hasattr(gen_icons, "state_svg") and not hasattr(gen_icons, "CAPTURE")


def _opaque_count(image):
    size = image.width()
    return sum(image.pixelColor(x, y).alpha() > 128 for x in range(size) for y in range(size))


def _centre_marks(image):
    """Opaque pixels in the eye's own box (+-12.5 x +-6.5 of the 64-unit
    viewBox): the eye's SHAPE. The ouroboros head swells inward near 12
    o'clock but never enters this box (its inner edge stays >= 13 units out)."""
    size = image.width()
    centre = size / 2
    half_w, half_h = size * 12.5 / 64, size * 6.5 / 64
    return {
        (x, y) for y in range(size) for x in range(size)
        if abs(x + 0.5 - centre) <= half_w and abs(y + 0.5 - centre) <= half_h
        and image.pixelColor(x, y).alpha() > 128
    }


@pytest.mark.parametrize("size", [16, 32])
def test_armed_and_recording_states_have_a_distinct_shape(qapp, gen_icons, size):
    assert _opaque_count(gen_icons.render("recording", size)) != _opaque_count(gen_icons.render("idle", size))
    off = _centre_marks(gen_icons.render("off", size))
    asleep = _centre_marks(gen_icons.render("asleep", size))
    armed = _centre_marks(gen_icons.render("armed", size))
    assert not off                                           # no eye, no disc
    assert not asleep and armed  # no disconnected dash on the resting mark


def test_heard_frame_eye_is_recording_red(qapp):
    image = tray_qt.render_mark("listening", "heard", 64)
    red = theme._hex_to_rgb(theme.RECORDING)
    c = image.pixelColor(32, 32)                              # pupil centre
    assert c.alpha() > 200 and max(abs(c.red() - red[0]), abs(c.green() - red[1]), abs(c.blue() - red[2])) < 30


def test_ring_spins_but_eye_does_not(qapp):
    still = tray_qt.render_mark("listening", "asleep", 64)
    # 180: the head swings from 12 to 6 o'clock, clear of the eye box.
    spun = tray_qt.render_mark("listening", "asleep", 64, rotation=180.0)
    assert still != spun
    assert _centre_marks(still) == _centre_marks(spun)


def test_resting_mark_has_two_articulated_joints_and_an_empty_centre(qapp):
    """The body is a cycle of three parts, not a generic loading circle."""
    image = tray_qt.render_mark("listening", "asleep", 256, brand=True)
    assert image.pixelColor(128, 128).alpha() < 40
    for angle in (28, 148):
        a = math.radians(angle)
        x = round(128 + tray_qt.RING_RADIUS * 4 * math.cos(a))
        y = round(128 + tray_qt.RING_RADIUS * 4 * math.sin(a))
        assert image.pixelColor(x, y).alpha() < 40, f"joint closed at {angle} degrees"
    for angle in (90, 210):
        a = math.radians(angle)
        x = round(128 + tray_qt.RING_RADIUS * 4 * math.cos(a))
        y = round(128 + tray_qt.RING_RADIUS * 4 * math.sin(a))
        assert image.pixelColor(x, y).alpha() > 100


def test_snake_head_has_negative_space_eye_not_a_floating_centre_eye(qapp):
    image = tray_qt.render_mark("listening", "off", 256, brand=True)
    assert image.pixelColor(27 * 4, 9 * 4).alpha() < 40
    assert image.pixelColor(128, 128).alpha() < 40


def test_head_has_two_distinct_jaws_around_the_tapered_tail(qapp):
    image = tray_qt.render_mark("listening", "off", 256, brand=True)
    # Both jaws project forward beyond the blunt arc; the small dark bite
    # between them carries the tail. Without these this is a rounded blob.
    assert image.pixelColor(35 * 4, 7 * 4).alpha() > 200
    assert image.pixelColor(34 * 4, 11 * 4).alpha() > 200
    assert image.pixelColor(34 * 4, 8 * 4).alpha() < 100
    assert image.pixelColor(36 * 4, 9 * 4).alpha() > 100  # tail between jaws


def test_brand_head_is_warm_but_recording_state_stays_distinct(qapp):
    for capture in ("idle", "listening"):
        brand = tray_qt.render_mark(capture, "off", 256, brand=True)
        head = brand.pixelColor(22 * 4, 11 * 4)
        body = brand.pixelColor(32 * 4, 54 * 4)
        assert head.alpha() > 200 and body.alpha() > 200
        assert head.red() > head.blue() + 30
        assert body.blue() > body.red() + 30
        assert head.name() != theme.RECORDING


@pytest.mark.parametrize("capture", ("idle", "listening"))
@pytest.mark.parametrize("size", (24, 256))
@pytest.mark.parametrize("first,target", (("armed", "off"), ("off", "armed")))
def test_brand_ring_cache_preserves_state_independent_of_render_order(qapp, capture, size, first, target):
    tray_qt.clear_mark_caches()
    try:
        expected = tray_qt.render_mark(capture, target, size, brand=True)
        tray_qt.clear_mark_caches()
        tray_qt.render_mark(capture, first, size, brand=True)
        actual = tray_qt.render_mark(capture, target, size, brand=True)
        assert actual == expected
    finally:
        tray_qt.clear_mark_caches()


def test_small_tray_mark_keeps_two_legible_joints(qapp):
    image = tray_qt.render_mark("listening", "asleep", 32, small_max=32)
    for angle in (30, 150):
        a = math.radians(angle)
        x = round(16 + 12.5 * math.cos(a))
        y = round(16 + 12.5 * math.sin(a))
        assert image.pixelColor(x, y).alpha() < 40
    assert image.pixelColor(16, 16).alpha() < 40


# ---------------------------------------------------------------------------
# 09b3/09b4: the ouroboros ring -- one centreline per segment, two weights
# ---------------------------------------------------------------------------

def _svg_regular_and_small():
    svg = (REPO / "assets" / "icon" / "samsara.svg").read_text(encoding="utf-8")
    split = svg.index('<g id="small"')
    return svg[:split], svg[split:]


def test_svg_ring_is_the_shared_centreline_at_three_weights(gen_icons):
    regular, _small = _svg_regular_and_small()
    centrelines = re.findall(r'<path data-role="centreline" d="([^"]*)"', regular)
    assert centrelines == [tray_qt.ring_centreline_path_data(i) for i in range(3)]
    paths = re.findall(r'<path data-role="segment" d="([^"]*)"', regular)
    hollow = [tray_qt.ring_segment_path_data(i, tray_qt.RING_LINE_WIDTH) for i in range(3)]
    band = [tray_qt.ring_segment_path_data(i, tray_qt.RING_BAND_WIDTH) for i in range(3)]
    brand = [tray_qt.ring_segment_path_data(i, tray_qt.RING_BRAND_WIDTH) for i in range(3)]
    assert paths == hollow + band + brand          # #ring-hollow, #ring-filled, #ring-brand (38)
    assert gen_icons.ring_segment_path_data is tray_qt.ring_segment_path_data
    # Hollow is a filled thin stroke-expansion, never an outline stroke.
    assert '<g id="ring-hollow" fill="#8b929c" fill-rule="evenodd" stroke="none">' in regular
    assert '<g id="ring-brand" fill="#8b929c" fill-rule="evenodd" stroke="none" display="none">' in regular


@pytest.mark.parametrize("segment", [0, 1, 2])
def test_hollow_and_recording_are_one_centreline_at_two_widths(segment):
    """Every sample's left/right edge pair, at BOTH weights, straddles the same
    centreline point; the widths follow each weight's own profile (19)."""
    samples = tray_qt.ring_centreline(segment)
    n = len(samples)
    for weight in (tray_qt.RING_LINE_WIDTH, tray_qt.RING_BAND_WIDTH):
        # 42: a weight with a nose lead (the band) strokes its own nose
        # samples on the same circle and takes no extra nose cap.
        samples = tray_qt.ring_centreline(segment, weight)
        n = len(samples)
        outline = tray_qt.ring_stroke_outline(segment, weight)
        left = outline[:n]
        capped = segment == tray_qt.HEAD_SEGMENT and tray_qt.nose_lead(weight) == 0.0
        right_start = n + (tray_qt._CAP_SAMPLES - 1 if capped else 0)
        right = outline[right_start:right_start + n][::-1]
        for (u, _a, x, y), (lx, ly), (rx, ry) in zip(samples, left, right):
            assert (lx + rx) / 2 == pytest.approx(x, abs=1e-9)
            assert (ly + ry) / 2 == pytest.approx(y, abs=1e-9)
            assert math.hypot(lx - rx, ly - ry) == pytest.approx(tray_qt.ring_width(segment, u, weight))
    # Away from head and tail both weights are the plain stroke.
    for u, *_rest in samples:
        if 0.5 <= u <= 0.6:
            for weight in (tray_qt.RING_LINE_WIDTH, tray_qt.RING_BAND_WIDTH):
                assert tray_qt.ring_width(segment, u, weight) == pytest.approx(weight)


@pytest.mark.parametrize("segment", [0, 1, 2])
def test_head_stays_on_the_constant_ring_radius(segment):
    for _u, _a, x, y in tray_qt.ring_centreline(segment):
        assert math.hypot(x - tray_qt.RING_CENTRE, y - tray_qt.RING_CENTRE) == pytest.approx(
            tray_qt.RING_RADIUS, abs=1e-9)


def test_whole_ring_shrinks_so_the_band_weight_head_fits():
    widest_head = max(tray_qt.RING_LINE_WIDTH * tray_qt.head_scale(tray_qt.RING_LINE_WIDTH),
                      tray_qt.RING_BAND_WIDTH * tray_qt.head_scale(tray_qt.RING_BAND_WIDTH))
    assert tray_qt.RING_RADIUS == pytest.approx(32.0 - tray_qt.VIEWBOX_MARGIN - widest_head / 2)
    for segment in range(3):
        for weight in (tray_qt.RING_LINE_WIDTH, tray_qt.RING_BAND_WIDTH):
            assert all(0.0 <= x <= 64.0 and 0.0 <= y <= 64.0
                       for x, y in tray_qt.ring_stroke_outline(segment, weight))
    assert (tray_qt.SEGMENT_START_DEG, tray_qt.SEGMENT_SPAN_DEG, tray_qt.SEGMENT_STEP_DEG) == (-88.0, 112.0, 120.0)
    assert tray_qt.HEAD_SPAN_DEG == 115.0


@pytest.mark.parametrize("weight", [3.0, 11.0])
def test_width_function_tail_head_and_nose(weight):
    tail, head, plain = tray_qt.TAIL_SEGMENT, tray_qt.HEAD_SEGMENT, 1
    tip = tray_qt.tip_fraction(weight)
    scale = tray_qt.head_scale(weight)
    assert tray_qt.stroke_scale(tail, 0.0, weight) == pytest.approx(tip)     # round-capped tip, not chiselled
    assert tray_qt.stroke_scale(tail, tray_qt.tail_fraction(weight), weight) == pytest.approx(1.0)
    # 42: the head is widest at 1 - nose_lead (the segment end when the lead is 0)
    peak = 1.0 - tray_qt.nose_lead(weight)
    assert tray_qt.stroke_scale(head, peak, weight) == pytest.approx(scale)
    *_profile, nose = tray_qt.weight_profile(weight)
    end = 1.0 + nose * tray_qt.HEAD_OVERSHOOT_DEG / tray_qt.HEAD_SPAN_DEG
    # without a lead the nose closes to a blunt TIP stub; with one (the band,
    # 42) it closes to its own round front
    closed = tip if tray_qt.nose_lead(weight) == 0.0 else 0.0
    assert tray_qt.stroke_scale(head, end, weight) == pytest.approx(closed, abs=1e-6)
    assert closed < tray_qt.stroke_scale(head, (peak + end) / 2, weight) < scale
    assert all(tray_qt.stroke_scale(plain, u / 10, weight) == 1.0 for u in range(11))


def test_head_and_tail_depend_on_weight():
    """19: the thin hollow line needs a bigger head and a longer tail to read;
    the recording band a small head so its nose stops eating the gap."""
    line, band = tray_qt.RING_LINE_WIDTH, tray_qt.RING_BAND_WIDTH
    assert tray_qt.weight_profile(line) == tray_qt.HOLLOW_PROFILE
    assert tray_qt.weight_profile(band) == tray_qt.BAND_PROFILE
    assert tray_qt.head_scale(line) == pytest.approx(1.9)
    # 38: 1.25x read as a flat stub in the 26 px header; the band head now
    # sits above BAND_HEAD_MIN and the gap test below still holds.
    assert tray_qt.head_scale(band) == pytest.approx(tray_qt.BAND_PROFILE[0])
    assert tray_qt.head_scale(band) >= tray_qt.BAND_HEAD_MIN >= 1.5
    assert tray_qt.tail_fraction(line) > tray_qt.tail_fraction(band)
    mid = (line + band) / 2
    assert tray_qt.head_scale(band) < tray_qt.head_scale(mid) < tray_qt.head_scale(line)
    assert not hasattr(tray_qt, "HEAD_SCALE") and not hasattr(tray_qt, "TAIL_FRACTION")
    # The band nose ends a quarter of the way into the centreline's overshoot
    # and the stroke stops there (no thin tip across the gap); the hollow nose
    # uses the whole overshoot. 42: the band nose closes to zero width there
    # (its own round front, begun nose_lead back into the segment).
    over = tray_qt.HEAD_OVERSHOOT_DEG / tray_qt.HEAD_SPAN_DEG
    head = tray_qt.HEAD_SEGMENT
    assert tray_qt.stroke_scale(head, 1.0 + 0.25 * over, band) == pytest.approx(0.0, abs=1e-6)
    assert tray_qt.stroke_scale(head, 1.0 + 0.2 * over, band) > 0.0
    assert tray_qt.stroke_scale(head, 1.0 + 0.5 * over, band) == 0.0
    assert tray_qt.stroke_scale(head, 1.0 + over, line) == pytest.approx(tray_qt.tip_fraction(line))
    # Nose samples land on the centreline's nose samples, so the band ends on a sample.
    assert (tray_qt.BAND_PROFILE[3] * tray_qt._NOSE_SAMPLES).is_integer()


def test_recording_head_leaves_the_12_oclock_gap_open(qapp):
    """19: on the ring's centreline, part of the gap between the band head and
    the tail stays clear at 256 px."""
    size = 256
    image = tray_qt.render_mark("recording", "off", size)
    k = size / 64.0
    clear = 0
    for tenth in range(-950, -870, 5):                  # head end -93 deg to tail start -88 deg
        a = math.radians(tenth / 10.0)
        x = size / 2 + tray_qt.RING_RADIUS * k * math.cos(a)
        y = size / 2 + tray_qt.RING_RADIUS * k * math.sin(a)
        clear += image.pixelColor(int(x), int(y)).alpha() < 40
    assert clear >= 1                                   # a bite, not a closed ring


def test_small_drawing_is_heavy_with_no_hairlines():
    """19: the #small ring is at least 6 units wide (1.5 px at 16 px), the
    open eye a solid dot; the resting state has no centre bar."""
    _regular, small = _svg_regular_and_small()
    small_paths = re.findall(r'<path data-role="segment" d="([^"]*)"', small)
    assert len(small_paths) == 9
    assert all(" A 25 25 " in d and " L " not in d for d in small_paths)
    widths = [float(w) for w in re.findall(r'stroke-width="([0-9.]+)"', small)]
    assert widths and min(widths) >= 6
    assert '<line data-role="eye" x1="21" y1="32" x2="43" y2="32" stroke="#8b929c" stroke-width="8"/>' in small
    assert "<ellipse" not in small and "<circle" in small
    assert tray_qt._SMALL_MAX == 20 and tray_qt.TASKBAR_SMALL_MAX == 32


def test_taskbar_sizes_use_the_heavy_drawing(qapp):
    for size in (16, 20):
        assert tray_qt.render_mark("listening", "off", size) == \
            tray_qt.render_mark("listening", "off", size, small_max=tray_qt.TASKBAR_SMALL_MAX)
    for size in (24, 32):
        heavy = tray_qt.render_mark("listening", "off", size, small_max=tray_qt.TASKBAR_SMALL_MAX)
        regular = tray_qt.render_mark("listening", "off", size)
        assert _opaque_count(heavy) >= 1.8 * _opaque_count(regular)


def test_16px_is_the_plain_ring_without_head_or_tail(qapp):
    # More than scale: the 24 px (ouroboros) drawing shrunk to 16 differs from
    # the 16 px (plain) drawing by far more than resampling noise.
    plain = tray_qt.render_mark("listening", "off", 16)
    shrunk = tray_qt.render_mark("listening", "off", 24).scaled(
        16, 16, tray_qt.Qt.AspectRatioMode.IgnoreAspectRatio,
        tray_qt.Qt.TransformationMode.SmoothTransformation)
    differing = sum(abs(plain.pixelColor(x, y).alpha() - shrunk.pixelColor(x, y).alpha()) > 96
                    for x in range(16) for y in range(16))
    assert differing >= 12


def test_small_sizes_cycle_24_frames_and_large_sizes_rotate_live(qapp):
    assert tray_qt.frame_step(7.0) == 0 and tray_qt.frame_step(20.0) == 1 and tray_qt.frame_step(-15.0) == 23
    for size in (16, 24):
        assert tray_qt.render_mark("listening", "armed", size, rotation=7.0) == \
            tray_qt.render_mark("listening", "armed", size, rotation=0.0)
        assert tray_qt.render_mark("listening", "armed", size, rotation=20.0) == \
            tray_qt.render_mark("listening", "armed", size, rotation=15.0)
    assert tray_qt.render_mark("listening", "armed", 32, rotation=7.0) != \
        tray_qt.render_mark("listening", "armed", 32, rotation=0.0)


def test_spin_speeds_are_a_state_channel():
    assert tray_qt.SPIN_SECONDS_PER_TURN == {
        "armed": 3.0, "listening": 3.0, "recording": 1.5, "thinking": 2.4, "transcribing": 0.9}


def test_spin_sheet_renders(qapp, gen_icons, tmp_path):
    out = gen_icons.write_spin_sheet(tmp_path / "ouroboros_spin.png")
    assert out.exists() and out.stat().st_size > 0
    assert gen_icons.SPIN_SHEET_ANGLES == (0, 45, 90, 135, 180, 225, 270, 315)


def test_check_flags_svg_drift(gen_icons, monkeypatch):
    regular, small = _svg_regular_and_small()
    drifted = regular.replace(
        tray_qt.ring_segment_path_data(0, tray_qt.RING_LINE_WIDTH), "M 0,0 L 1,1 Z", 1) + small
    assert gen_icons.synced_svg_text(drifted) != drifted
    assert gen_icons.synced_svg_text(regular + small) == regular + small


def test_check_fails_on_an_unparseable_svg(gen_icons, monkeypatch, tmp_path):
    broken = tmp_path / "samsara.svg"
    broken.write_text("<svg><!-- a -- b --></svg>", encoding="utf-8")
    monkeypatch.setattr(gen_icons, "SVG_PATH", broken)
    assert gen_icons.stale_assets() == [broken]


def test_weight_sheet_renders(qapp, gen_icons, tmp_path):
    out = gen_icons.write_weight_sheet(tmp_path / "ouroboros_weights.png")
    assert out.exists() and out.stat().st_size > 0
    assert gen_icons.WEIGHT_SHEET_ANGLES == (0, 45)


def test_montage_renders(qapp, gen_icons, tmp_path):
    out = gen_icons.write_montage(tmp_path / "icon_states.png")
    assert out.exists() and out.stat().st_size > 0
    assert gen_icons.MONTAGE_STATES == ("off", "asleep", "idle", "listening",
                                        "recording", "ava", "armed", "heard")


def test_dictation_chase_timer_spins_every_capture_state_without_reset():
    tree, cls = _dictation_class()
    source = (REPO / "dictation.py").read_text(encoding="utf-8-sig")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    tick = _tray_method_source(source, methods["_icon_chase_tick"])
    assert "SPIN_SECONDS_PER_TURN[pace]" in tick
    for pace in ("'recording'", "'transcribing'", "'listening'", "'armed'"):
        assert re.search(r"tick_interval, pace = ICON_TICK_\w+, " + pace, tick), pace
    for name in ("_start_icon_chase", "_stop_icon_chase"):
        assert "_icon_rotation = 0.0" not in _tray_method_source(source, methods[name]), name


# ---------------------------------------------------------------------------
# Surfaces: no colour literal outside theme tokens
# ---------------------------------------------------------------------------

def _string_hex_literals(source: str) -> list[str]:
    """#rrggbb inside any string token (comments are not strings)."""
    return [
        match
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.STRING
        for match in HEX_IN_TEXT.findall(tok.string)
    ]


@pytest.mark.parametrize("relpath", [
    "samsara/ui/tray_qt.py",
    "samsara/ui/listening_indicator.py",
    "samsara/ui/splash_qt.py",
])
def test_surface_has_no_colour_literals(relpath):
    source = (REPO / relpath).read_text(encoding="utf-8")
    assert _string_hex_literals(source) == []


def _dictation_class():
    tree = ast.parse((REPO / "dictation.py").read_text(encoding="utf-8-sig"))
    return tree, next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")


def _tray_method_source(source, method):
    """Inspect the facade and its locally imported animation implementation."""
    segment = ast.get_source_segment(source, method)
    for node in method.body:
        if isinstance(node, ast.ImportFrom) and node.module == "samsara.tray_animation":
            target = next(alias.name for alias in node.names
                          if (alias.asname or alias.name) == method.name)
            moved_source = (REPO / "samsara/tray_animation.py").read_text(encoding="utf-8")
            moved = next(n for n in ast.parse(moved_source).body
                         if isinstance(n, ast.FunctionDef) and n.name == target)
            return segment + "\n" + ast.get_source_segment(moved_source, moved)
    return segment


def test_dictation_tray_render_uses_the_mark_and_no_palette():
    tree, cls = _dictation_class()
    source = (REPO / "dictation.py").read_text(encoding="utf-8-sig")
    assigned = {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not {"_WHEEL_COLORS", "_WHEEL_IDLE", "_WHEEL_SNOOZE", "_WHEEL_GOLD"} & assigned
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert "_arc_polygon" not in methods
    for name in ("_tray_mark", "create_icon_image", "_push_tray_icon", "_flash_tray_heard",
                 "_icon_chase_tick", "_stop_icon_chase"):
        segment = _tray_method_source(source, methods[name])
        assert _string_hex_literals(segment) == [], name
    assert "MarkFrame" in _tray_method_source(source, methods["create_icon_image"])
    assert "HEARD_KEYFRAMES" in _tray_method_source(source, methods["_flash_tray_heard"])


# ---------------------------------------------------------------------------
# AUMID
# ---------------------------------------------------------------------------

def _aumid_block():
    tree = ast.parse((REPO / "dictation.py").read_text(encoding="utf-8-sig"))
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.If) and "SetCurrentProcessExplicitAppUserModelID" in ast.unparse(node):
            return tree, index, node
    raise AssertionError("no module-level AUMID call in dictation.py")


def test_aumid_is_set_at_import_before_any_qt_application(monkeypatch):
    tree, index, node = _aumid_block()
    source = ast.unparse(tree)
    assert source.count("SetCurrentProcessExplicitAppUserModelID") == 1   # exactly one call
    class_index = next(i for i, n in enumerate(tree.body)
                       if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    assert index < class_index
    # Nothing at module level before it creates Qt; Qt starts only inside
    # functions (qt_runtime.ensure_started / QApplication in DictationApp and
    # the splash), which run after import.
    for earlier in tree.body[:index]:
        text = ast.unparse(earlier)
        assert "QApplication(" not in text and "ensure_started(" not in text

    shell32 = MagicMock()
    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = types.SimpleNamespace(shell32=shell32)
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setitem(sys.modules, "winsound", types.ModuleType("winsound"))
    qapp_calls = []
    namespace = {"sys": types.SimpleNamespace(platform="win32"),
                 "QApplication": lambda *a: qapp_calls.append(a)}
    # This is one isolated statement with a fake ctypes boundary, not an
    # execution of the application source/bootstrap.
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<aumid-statement-test>", "exec"), namespace)
    shell32.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with("MorneIngstar.Samsara")
    assert qapp_calls == []


def test_spec_ships_the_icon_and_the_mark():
    spec = (REPO / "scripts" / "samsara.spec").read_text(encoding="utf-8")
    # The owner-supplied app artwork moved to brand; the live mark stays icon.
    assert "icon=str(app_dir / 'assets' / 'brand' / 'samsara.ico')" in spec
    assert "'samsara.ico'), 'assets/brand'" in spec
    assert "'samsara.svg'), 'assets/icon'" in spec


# ---------------------------------------------------------------------------
# 29: one rounded scrollbar treatment app-wide, no arrow buttons
# ---------------------------------------------------------------------------

_SCROLLBAR_SHEETS_PY = ("samsara/ui/main_window_qt.py", "samsara/ui/settings_qt.py", "samsara/ui/history_view.py",
                        "samsara/ui/dictionary_panel_qt.py")


def _rule(qss, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", qss)
    assert m, selector
    return m.group(1)


def test_scrollbar_rule_is_in_every_window_stylesheet():
    from samsara.ui import dictionary_panel_qt, history_view, main_window_qt, settings_qt
    qss = theme.SCROLLBAR_QSS
    assert qss in theme.build_stylesheet()
    assert qss in settings_qt.stylesheet()
    assert qss in main_window_qt._ss()
    assert qss in dictionary_panel_qt._ss()
    assert qss in history_view.build_stylesheet()


def test_scrollbar_rule_shape():
    qss = theme.SCROLLBAR_QSS
    width, margin = theme.SCROLLBAR_WIDTH, theme._SCROLLBAR_MARGIN
    assert (width, margin, theme._SCROLLBAR_MIN_GRAB) == (10, 2, 44)
    assert f"width: {width + 2 * margin}px" in _rule(qss, "QScrollBar:vertical")
    assert f"height: {width + 2 * margin}px" in _rule(qss, "QScrollBar:horizontal")
    for orient, grab in (("vertical", "min-height"), ("horizontal", "min-width")):
        assert "background: transparent" in _rule(qss, f"QScrollBar:{orient}")
        handle = _rule(qss, f"QScrollBar::handle:{orient}")
        assert f"border-radius: {width // 2}px" in handle             # fully rounded
        assert f"margin: {margin}px" in handle
        assert f"{grab}: 44px" in handle                               # grabbable, never a sliver
        lines = _rule(qss, f"QScrollBar::add-line:{orient}, QScrollBar::sub-line:{orient}")
        assert "width: 0px" in lines and "height: 0px" in lines        # no arrow buttons
    assert "background: transparent" in _rule(qss, "QScrollBar::add-page, QScrollBar::sub-page")
    # visible, not overlay-only: nothing hides the bar
    assert "display" not in qss and "opacity" not in qss and "none;" not in qss.replace("border: none;", "") \
        .replace("background: none;", "").replace("image: none;", "")


def test_scrollbar_colours_come_from_tokens():
    qss = theme.SCROLLBAR_QSS
    assert not HEX_IN_TEXT.findall(qss)
    idle = ",".join(str(c) for c in theme._hex_to_rgb(theme.ICON_IDLE))
    accent = ",".join(str(c) for c in theme._hex_to_rgb(theme.ACCENT))
    rest = 0.35 if theme.POLARITY == "dark" else 0.60
    assert f"rgba({idle},{rest:.2f})" in _rule(qss, "QScrollBar::handle:vertical")
    assert f"rgba({idle},{rest + 0.20:.2f})" in _rule(
        qss, "QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover")
    assert f"rgba({accent},0.70)" in _rule(
        qss, "QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed")
    block = (REPO / "samsara/ui/theme.py").read_text(encoding="utf-8")
    block = block[block.index("# Scrollbars -- ONE treatment"):block.index("# Dialog-wide stylesheet")]
    # Queue 129: wash() lives in this block and names the two ink extremes
    # (#ffffff / #000000) to pick a direction from the polarity. Those are the
    # only literals allowed here -- every COLOUR still comes from a token.
    assert sorted(_string_hex_literals(block)) == ["#000000", "#ffffff"]


def test_no_local_scrollbar_overrides_left_in_the_owned_windows():
    paths = [REPO / p for p in _SCROLLBAR_SHEETS_PY] + sorted((REPO / "samsara/ui/settings").glob("*.py"))
    for path in paths:
        source = path.read_text(encoding="utf-8")
        # 515 deliberately gives Settings' destination scrollbars their own
        # quiet treatment. All other window/page sheets keep the shared rule.
        if path.name == 'calm_layout.py':
            assert 'scroll.verticalScrollBar().setStyleSheet' in source
            assert not HEX_IN_TEXT.findall(source)
        else:
            assert "QScrollBar" not in source, path
    vertical_policies = sum(p.read_text(encoding="utf-8").count("setVerticalScrollBarPolicy") for p in paths)
    assert vertical_policies == 1      # Settings' destination sidebar policy.


def test_app_stylesheet_install_is_idempotent(qapp):
    before = qapp.styleSheet()
    try:
        theme.install_app_scrollbars(qapp)
        theme.install_app_scrollbars(qapp)
        assert qapp.styleSheet().count(theme._SCROLLBAR_MARKER) == 1
        assert theme.SCROLLBAR_QSS in qapp.styleSheet()
    finally:
        qapp.setStyleSheet(before)


def test_scrolled_list_renders_without_arrow_subcontrols(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidget, QStyle, QStyleOptionSlider

    lst = QListWidget()
    lst.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    lst.setStyleSheet(theme.build_stylesheet())
    lst.addItems([f"row {i}" for i in range(300)])
    lst.resize(240, 200)
    lst.show()
    qapp.processEvents()
    try:
        bar = lst.verticalScrollBar()
        assert bar.isVisible() and bar.maximum() > 0
        assert bar.width() == theme.SCROLLBAR_WIDTH + 2 * theme._SCROLLBAR_MARGIN
        opt = QStyleOptionSlider()
        bar.initStyleOption(opt)
        style = bar.style()
        cc = QStyle.ComplexControl.CC_ScrollBar
        for sub in (QStyle.SubControl.SC_ScrollBarAddLine, QStyle.SubControl.SC_ScrollBarSubLine):
            rect = style.subControlRect(cc, opt, sub, bar)
            assert rect.isEmpty() or rect.height() == 0 or rect.width() == 0, (sub, rect)
        handle = style.subControlRect(cc, opt, QStyle.SubControl.SC_ScrollBarSlider, bar)
        assert handle.height() >= 44
        image = bar.grab().toImage()
        c = image.pixelColor(handle.center())
        idle = theme._hex_to_rgb(theme.ICON_IDLE)                       # premultiplied round trip: +-2
        assert max(abs(a - b) for a, b in zip((c.red(), c.green(), c.blue()), idle)) <= 2
        assert abs(c.alpha() - round(0.35 * 255)) <= 2
        corner = image.pixelColor(handle.left(), handle.top())
        assert corner.alpha() < c.alpha()                                 # rounded, not a square grip
        top = image.pixelColor(bar.width() // 2, 0)
        assert top.alpha() == 0                                           # no arrow button at the top
    finally:
        lst.close()


# ---------------------------------------------------------------------------
# 38: the brand presentation (window header, Home) versus the tray rules
# ---------------------------------------------------------------------------

def test_brand_presentation_keeps_state_colours_and_resting_centre_empty():
    """Brand body stays cyan; a small warm head is not the recording ring."""
    assert tray_qt.brand_capture()["idle"][0] == theme.ACCENT
    assert tray_qt.brand_capture()["listening"][0] == theme.ACCENT
    assert tray_qt.brand_capture()["recording"] == (theme.RECORDING, "ring-filled")
    assert theme.ICON_IDLE not in {c for c, _ in tray_qt.brand_capture().values()}
    assert set(tray_qt.BRAND_EYE) == set(tray_qt.MARK_EYE)
    assert tray_qt.BRAND_EYE["off"] is None and tray_qt.BRAND_EYE["asleep"] is None
    assert tray_qt.BRAND_EYE["armed"] == "eye-open"
    for eye in tray_qt.MARK_EYE:
        assert tray_qt.mark_eye_id(eye, brand=True) == tray_qt.BRAND_EYE[eye]
    assert tray_qt.mark_colours("idle", "off", brand=True) == (theme.ACCENT, theme.ACCENT)
    assert tray_qt.mark_colours("recording", "armed", brand=True) == (theme.RECORDING, theme.RECORDING)
    # tray rules unchanged
    assert tray_qt.mark_capture()["idle"][0] == theme.ICON_IDLE and tray_qt.MARK_EYE["off"] is None
    assert tray_qt.mark_colours("idle", "off") == (theme.ICON_IDLE, theme.ICON_IDLE)


def test_brand_weight_sits_between_hollow_and_band():
    """Chosen from the 26 px row of the weight sheet: heavier than the tray's
    hollow line so head and tail read at 26 px, well short of the band."""
    assert tray_qt.RING_LINE_WIDTH < tray_qt.RING_BRAND_WIDTH < tray_qt.RING_BAND_WIDTH
    assert tray_qt.RING_BRAND_WIDTH >= 5.0
    assert tray_qt.head_scale(tray_qt.RING_BRAND_WIDTH) > tray_qt.head_scale(tray_qt.RING_BAND_WIDTH)


def _colour_pixels(image, hex_colour, tolerance=28):
    r, g, b = theme._hex_to_rgb(hex_colour)
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            if c.alpha() > 200 and abs(c.red() - r) <= tolerance and abs(c.green() - g) <= tolerance \
                    and abs(c.blue() - b) <= tolerance:
                count += 1
    return count


@pytest.mark.parametrize("eye", ["off", "asleep", "armed"])
def test_brand_mark_at_rest_renders_accent_never_grey(qapp, eye):
    """The centre is empty until armed, at both header and Home sizes."""
    for size in (26, 60):
        image = tray_qt.render_mark("idle", eye, size, brand=True)
        assert _colour_pixels(image, theme.ACCENT) > size, (size, eye)
        # The warm/cool bridge can pass through a greyish antialiased pixel;
        # it must not produce a grey region that looks like an idle icon.
        assert _colour_pixels(image, theme.ICON_IDLE) <= 2, (size, eye)
        centre = image.pixelColor(size // 2, size // 2)
        if eye == "armed":
            assert centre.alpha() > 100
        else:
            assert centre.alpha() < 40
    tray = tray_qt.render_mark("idle", "off", 60)
    assert _colour_pixels(tray, theme.ICON_IDLE) > 60 and tray.pixelColor(30, 30).alpha() < 40


def test_brand_mark_is_heavier_than_the_tray_hollow_line(qapp):
    size = 60
    brand = _colour_pixels(tray_qt.render_mark("listening", "off", size, brand=True), theme.ACCENT)
    tray = _colour_pixels(tray_qt.render_mark("listening", "off", size), theme.ACCENT)
    assert brand > tray * 1.3


def test_hub_type_scale_is_additive_and_has_no_role_below_the_minimum():
    """41: one type scale for the hub and Home, added beside the shared four
    sizes without changing them."""
    # The shared names follow the canonical design scale (506).
    from samsara.ui.design_scale import TYPE_SCALE
    assert (theme.FONT_SIZE_TITLE, theme.FONT_SIZE_HEADING, theme.FONT_SIZE_BODY,
            theme.FONT_SIZE_CAPTION, theme.FONT_SIZE_DISPLAY) == tuple(
                TYPE_SCALE[role] for role in ('title', 'heading', 'body', 'caption', 'display'))
    tokens = {theme.TYPE_NAV, theme.TYPE_STATE, theme.TYPE_CARD_TITLE, theme.TYPE_BODY,
              theme.TYPE_SECONDARY, theme.TYPE_SECTION_LABEL, theme.TYPE_FIGURE, theme.TYPE_CREED}
    assert set(theme.HOME_TYPE_SCALE.values()) <= tokens
    assert min(theme.HOME_TYPE_SCALE.values()) >= theme.TYPE_MIN >= 14
