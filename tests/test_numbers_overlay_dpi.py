"""Pure regressions for Show Numbers physical-to-Qt coordinate mapping."""

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


def test_fractional_scale_uses_monitor_relative_origin():
    mappings = [
        ((-3840, 0, 0, 2160), (-3840, 0, -1280, 1440), 1.5),
    ]
    assert _map_physical_to_qt(-1920, 1080, mappings) == (-2560, 720)
