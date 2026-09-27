"""Layout and coverage regressions for the theme surfaces queue 129 missed."""

from PySide6.QtGui import QFont, QFontMetrics

from samsara.ui import theme
from tests._theme_stub_app import StubApp
from tests._theme_surfaces import SURFACES


def test_queue_129_gap_surfaces_are_shared_by_the_switch_test_and_proof_tool():
    """The switch test and proof tool both consume this exact shared table."""
    from tests import test_theme_switch

    expected = {"profile_manager", "voice_training", "ava_guide"}
    assert expected <= SURFACES.keys()
    assert expected <= set(test_theme_switch.SURFACES)


def test_ava_guide_step_labels_fit_and_do_not_overlap_connectors(qapp):
    from samsara.ui.ava_guide_qt import _WizardWindow

    theme.set_theme("dark", refresh=False)
    guide = _WizardWindow(StubApp())
    guide.show()
    qapp.processEvents()
    try:
        strip = guide._step_indicator
        assert strip.height() >= theme.HIT_TARGET_MIN
        font = theme.qfont(theme.TYPE_MIN, weight=QFont.Weight.DemiBold)
        for index, name in enumerate(strip.steps):
            bounds = strip.visualItemRect(strip.item(index))
            assert bounds.width() >= QFontMetrics(font).horizontalAdvance(name)
            marker_bottom = bounds.top() + theme.TYPE_TITLE * 0.85 + theme.TYPE_BODY * 0.76
            label_top = bounds.top() + theme.TYPE_TITLE * 1.6
            assert marker_bottom < label_top < bounds.bottom()
    finally:
        guide.close()
        theme.set_theme("dark", refresh=False)
