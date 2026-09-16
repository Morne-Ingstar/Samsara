"""Queue 164 regression coverage for HistoryView filter wiring."""

from unittest.mock import Mock

from samsara.ui import history_view as hv


def test_filter_signal_triggers_one_reload(qapp):
    """One chip change must have exactly one currentTextChanged receiver."""
    view = hv.HistoryView(None)
    reload_once = Mock()
    view._reload = reload_once

    view._filter.setCurrentText(hv.FILTER_DICTATION)

    reload_once.assert_called_once_with()
