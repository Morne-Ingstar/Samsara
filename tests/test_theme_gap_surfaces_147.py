"""Layout and coverage regressions for the theme surfaces queue 129 missed."""

from PySide6.QtGui import QFont, QFontMetrics

from samsara.ui import theme
from tests._theme_stub_app import StubApp
from tests._theme_surfaces import SURFACES


def test_queue_129_gap_surfaces_are_shared_by_the_switch_test_and_proof_tool():
    """The switch test and proof tool both consume this exact shared table."""
    from tests import test_theme_switch

    expected = {"profile_manager", "ava_guide"}
    assert expected <= SURFACES.keys()
    assert expected <= set(test_theme_switch.SURFACES)


def test_ava_guide_step_labels_fit_and_do_not_overlap_connectors(qapp, qt_widget_scope):
    from samsara.ui.ava_guide_qt import _WizardWindow

    theme.set_theme("dark", refresh=False)
    guide = _WizardWindow(StubApp())
    guide.show()
    qapp.processEvents()
    try:
        # The current Ava guide has a vertical sidebar, not the retired strip.
        assert len(guide._steps) == 5
        for index, label in enumerate(guide._steps):
            assert label.height() >= theme.HIT_TARGET_MIN
            assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
            assert label.height() >= label.fontMetrics().height()
            if index:
                previous = guide._steps[index - 1]
                assert previous.geometry().bottom() < label.geometry().top()
                assert previous.x() == label.x()
    finally:
        with qt_widget_scope(guide):
            guide.close()
        theme.set_theme("dark", refresh=False)
