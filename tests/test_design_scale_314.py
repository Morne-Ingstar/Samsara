"""Focused contract checks for the approved Settings/guides scale (queue 314)."""
from pathlib import Path

from samsara.ui import design_scale, theme


ROOT = Path(__file__).resolve().parents[1]
SURFACES = (
    "samsara/ui/settings/assembly.py",
    "samsara/ui/first_run_wizard_qt.py",
    "samsara/ui/tutorial_qt.py",
    "samsara/ui/ava_guide_qt.py",
    "samsara/ui/command_cheatsheet_qt.py",
    "samsara/ui/quick_reference_qt.py",
    "samsara/ui/mic_setup_wizard_qt.py",
    "samsara/ui/stress_wizard_qt.py",
)


def test_theme_reexports_the_approved_scale():
    assert theme.TYPE_MIN == design_scale.TYPE_SCALE["caption"]
    assert theme.TYPE_BODY == design_scale.TYPE_SCALE["body"]
    assert theme.TYPE_EMPHASIS == design_scale.TYPE_SCALE["label"]
    assert theme.TYPE_HEADING == design_scale.TYPE_SCALE["heading"]
    assert theme.TYPE_TITLE == design_scale.TYPE_SCALE["title"]
    assert theme.TYPE_DISPLAY == design_scale.TYPE_SCALE["display"]
    assert theme.HIT_TARGET_MIN == design_scale.TAP_TARGET == 44
    assert theme.SPACE_SCALE == design_scale.SPACE_SCALE


def test_every_selected_surface_applies_the_shared_b_treatment():
    for relative_path in SURFACES:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "theme.apply_design_scale(self)" in source, relative_path


def test_ghost_actions_are_real_secondary_buttons():
    qss = design_scale._button_qss(False).replace(" ", "")
    assert f"background:{theme.BG2}" in qss
    assert "border:1pxsolid" in qss
    assert f"min-height:{theme.HIT_TARGET_MIN}px" in qss
