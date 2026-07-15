import json

from samsara.ui_scale import (
    UI_SCALE_OPTIONS,
    apply_early_ui_scale,
    normalize_ui_scale,
    ui_scale_label,
)


def test_supported_scale_is_applied_before_qt(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ui_scale": 1.15}), encoding="utf-8")
    environ = {}
    assert apply_early_ui_scale(path, environ) == 1.15
    assert environ["QT_SCALE_FACTOR"] == "1.15"


def test_bad_or_missing_config_falls_back_to_standard(tmp_path):
    environ = {}
    assert apply_early_ui_scale(tmp_path / "missing.json", environ) == 1.0
    assert environ["QT_SCALE_FACTOR"] == "1"


def test_normalization_snaps_to_supported_sizes():
    assert normalize_ui_scale("1.16") == 1.15
    assert normalize_ui_scale("nonsense") == 1.0
    assert ui_scale_label(1.30) == "Extra large (130%)"
    assert set(UI_SCALE_OPTIONS.values()) == {1.0, 1.15, 1.30}
