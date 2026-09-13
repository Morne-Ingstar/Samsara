"""One visual identity (owner decisions 2026-09-13): cyan brand, red = recording,
eye = hands-free. Tokens, the single-source SVG, the one drawing routine
(samsara.ui.tray_qt.render_mark), the generated icon set, the surfaces that
consume it, and the taskbar AUMID. dictation.py is read as source, never
imported, here."""

import ast
import importlib.util
import io
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
        assert capture in tray_qt.MARK_CAPTURE and eye in tray_qt.MARK_EYE
        for size in (16, 24, 32, 48, 64):
            assert (REPO / "assets" / "icon" / "states" / f"samsara_{state}_{size}.png").exists()
    assert tray_qt.MARK_EYE["off"] is None                  # OFF = ring only
    assert tray_qt.APP_MARK == ("listening", "asleep")       # cyan wheel, lid closed


def test_mark_is_monochrome_per_state_and_heard_is_the_red_eye():
    for capture, (colour, _ring) in tray_qt.MARK_CAPTURE.items():
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
    assert 'rx="13" ry="8.5"' in svg                         # 16 px dot-in-ellipse


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_generated_assets_are_up_to_date(qapp, gen_icons):
    assert gen_icons.stale_assets() == []


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
def test_state_is_carried_by_shape_not_colour_alone(qapp, gen_icons, size):
    assert _opaque_count(gen_icons.render("recording", size)) > _opaque_count(gen_icons.render("idle", size))
    off = _centre_marks(gen_icons.render("off", size))
    asleep = _centre_marks(gen_icons.render("asleep", size))
    armed = _centre_marks(gen_icons.render("armed", size))
    assert not off                                           # no eye, no disc
    assert asleep and armed and asleep != armed


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


# ---------------------------------------------------------------------------
# 09b3: the ouroboros ring
# ---------------------------------------------------------------------------

def _svg_regular_and_small():
    svg = (REPO / "assets" / "icon" / "samsara.svg").read_text(encoding="utf-8")
    split = svg.index('<g id="small"')
    return svg[:split], svg[split:]


def test_svg_ring_is_the_shared_geometry_function(gen_icons):
    regular, _small = _svg_regular_and_small()
    paths = re.findall(r'<path data-role="segment" d="([^"]*)"', regular)
    expected = [tray_qt.ring_segment_path_data(i) for i in range(3)]
    assert paths == expected + expected            # hollow group, then filled group
    assert gen_icons.ring_segment_path_data is tray_qt.ring_segment_path_data


def test_width_function_tail_head_and_nose():
    w = tray_qt.RING_WIDTH
    tail, head, plain = tray_qt.TAIL_SEGMENT, tray_qt.HEAD_SEGMENT, 1
    assert tray_qt.ring_width(tail, 0.0) == 0.0                            # tail starts as a point
    assert tray_qt.ring_width(tail, tray_qt.TAIL_FRACTION) == pytest.approx(w)
    assert tray_qt.ring_width(head, 1.0) == pytest.approx(w * tray_qt.HEAD_SCALE)
    over = tray_qt.HEAD_OVERSHOOT_DEG / tray_qt.SEGMENT_SPAN_DEG
    assert tray_qt.ring_width(head, 1.0 + over) == pytest.approx(0.0)       # nose closes
    assert 0 < tray_qt.ring_width(head, 1.0 + over / 2) < w * tray_qt.HEAD_SCALE
    assert all(tray_qt.ring_width(plain, u / 10) == w for u in range(11))
    # Segment angles and ring radius unchanged from 09b2.
    assert (tray_qt.SEGMENT_START_DEG, tray_qt.SEGMENT_SPAN_DEG, tray_qt.RING_RADIUS) == (-84.0, 108.0, 24.5)
    outline = tray_qt.ring_segment_outline(head)
    assert all(0.0 <= x <= 64.0 and 0.0 <= y <= 64.0 for x, y in outline)


def test_16px_is_the_plain_ring_without_head_or_tail(qapp):
    _regular, small = _svg_regular_and_small()
    small_paths = re.findall(r'<path data-role="segment" d="([^"]*)"', small)
    assert small_paths and all(" A 27 27 " in d and " L " not in d for d in small_paths)
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
    assert tray_qt.SPIN_SECONDS_PER_TURN == {"thinking": 2.4, "transcribing": 0.9}


def test_spin_sheet_renders(qapp, gen_icons, tmp_path):
    out = gen_icons.write_spin_sheet(tmp_path / "ouroboros_spin.png")
    assert out.exists() and out.stat().st_size > 0
    assert gen_icons.SPIN_SHEET_ANGLES == (0, 45, 90, 135, 180, 225, 270, 315)


def test_check_flags_svg_drift(gen_icons, monkeypatch):
    regular, small = _svg_regular_and_small()
    drifted = regular.replace(tray_qt.ring_segment_path_data(0), "M 0,0 L 1,1 Z", 1) + small
    assert gen_icons.synced_svg_text(drifted) != drifted
    assert gen_icons.synced_svg_text(regular + small) == regular + small


def test_montage_renders(qapp, gen_icons, tmp_path):
    out = gen_icons.write_montage(tmp_path / "icon_states.png")
    assert out.exists() and out.stat().st_size > 0
    assert gen_icons.MONTAGE_STATES == ("off", "asleep", "idle", "listening",
                                        "recording", "ava", "armed", "heard")


def test_dictation_chase_timer_spins_at_transcribing_speed_without_reset():
    tree, cls = _dictation_class()
    source = (REPO / "dictation.py").read_text(encoding="utf-8-sig")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    tick = ast.get_source_segment(source, methods["_icon_chase_tick"])
    assert "SPIN_SECONDS_PER_TURN['transcribing']" in tick
    for name in ("_start_icon_chase", "_stop_icon_chase"):
        assert "_icon_rotation = 0.0" not in ast.get_source_segment(source, methods[name]), name


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


def test_dictation_tray_render_uses_the_mark_and_no_palette():
    tree, cls = _dictation_class()
    source = (REPO / "dictation.py").read_text(encoding="utf-8-sig")
    assigned = {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not {"_WHEEL_COLORS", "_WHEEL_IDLE", "_WHEEL_SNOOZE", "_WHEEL_GOLD"} & assigned
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert "_arc_polygon" not in methods
    for name in ("_tray_mark", "create_icon_image", "_push_tray_icon", "_flash_tray_heard",
                 "_icon_chase_tick", "_stop_icon_chase"):
        segment = ast.get_source_segment(source, methods[name])
        assert _string_hex_literals(segment) == [], name
    assert "MarkFrame" in ast.get_source_segment(source, methods["create_icon_image"])
    assert "HEARD_KEYFRAMES" in ast.get_source_segment(source, methods["_flash_tray_heard"])


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
    exec(compile(ast.Module(body=[node], type_ignores=[]), "dictation.py", "exec"), namespace)
    shell32.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with("MorneIngstar.Samsara")
    assert qapp_calls == []


def test_spec_ships_the_icon_and_the_mark():
    spec = (REPO / "scripts" / "samsara.spec").read_text(encoding="utf-8")
    assert "icon=str(app_dir / 'assets' / 'icon' / 'samsara.ico')" in spec
    assert "'samsara.svg'), 'assets/icon'" in spec
