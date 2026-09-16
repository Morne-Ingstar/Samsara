"""Layout and coverage regressions for the theme surfaces queue 129 missed."""

from PySide6.QtCore import QRect

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
        strip = guide._step_strip
        assert strip.height() >= 52
        connector_bounds = [QRect(line.geometry()) for line in guide._step_connectors]
        for _dot, label in guide._dots:
            assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
            assert label.geometry().bottom() < strip.height()
            assert not any(label.geometry().intersects(bounds) for bounds in connector_bounds)
    finally:
        guide.close()
        theme.set_theme("dark", refresh=False)
