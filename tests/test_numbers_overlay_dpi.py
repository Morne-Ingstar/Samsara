"""Pure regressions for Show Numbers physical-to-Qt coordinate mapping."""

import ctypes
from types import SimpleNamespace

import samsara.ui.numbers_overlay_qt as overlay
from samsara.ui.numbers_overlay_qt import _map_physical_to_qt


MAPPINGS = [
    ((0, 0, 3840, 2160), (0, 0, 2560, 1440), 1.5),
    ((3840, 1, 5760, 1081), (3840, 1, 5760, 1081), 1.0),
    ((5760, -496, 9600, 1664), (5760, -496, 9600, 1664), 1.0),
]


def test_primary_4k_150_percent_maps_physical_pixels_to_qt_dips():
    assert _map_physical_to_qt(3000, 1500, MAPPINGS) == (2000, 1000)


def test_100_percent_secondary_preserves_native_desktop_origin():
    assert _map_physical_to_qt(4000, 501, MAPPINGS) == (4000, 501)


def test_negative_y_monitor_origin_is_preserved():
    assert _map_physical_to_qt(6000, -400, MAPPINGS) == (6000, -400)


def test_unknown_point_fails_safe_to_identity():
    assert _map_physical_to_qt(-500, -500, MAPPINGS) == (-500, -500)


def test_uia_border_just_above_mixed_dpi_screen_still_maps_to_qt():
    mappings = [
        ((5760, 5, 8640, 1625), (5760, 5, 7680, 1085), 1.5),
    ]
    # Real failure evidence: UIA returned y=3 for a control on a monitor whose
    # top is y=5.  Strict containment left physical x=8436 outside Qt's x=7680
    # right edge.  The slight border overflow must use the nearby screen map.
    assert _map_physical_to_qt(8436, 3, mappings) == (7544, 4)


def test_point_well_outside_monitor_does_not_use_nearest_mapping():
    mappings = [
        ((5760, 5, 8640, 1625), (5760, 5, 7680, 1085), 1.5),
    ]
    assert _map_physical_to_qt(8436, -100, mappings) == (8436, -100)


def test_fractional_scale_uses_monitor_relative_origin():
    mappings = [
        ((-3840, 0, 0, 2160), (-3840, 0, -1280, 1440), 1.5),
    ]
    assert _map_physical_to_qt(-1920, 1080, mappings) == (-2560, 720)


def test_dpi_thread_context_uses_handle_compatible_pointer(monkeypatch):
    class Setter:
        argtypes = None
        restype = None

        def __init__(self):
            self.argument = None

        def __call__(self, argument):
            self.argument = argument

    setter = Setter()
    monkeypatch.setattr(overlay.sys, "platform", "win32")
    monkeypatch.setattr(
        overlay.ctypes,
        "windll",
        SimpleNamespace(
            user32=SimpleNamespace(SetThreadDpiAwarenessContext=setter),
        ),
    )

    overlay._ensure_dpi_thread_context()

    assert setter.argtypes == [ctypes.c_void_p]
    assert setter.restype is ctypes.c_void_p
    assert isinstance(setter.argument, ctypes.c_void_p)
