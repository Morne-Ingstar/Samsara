from datetime import datetime

from samsara.ui.settings_qt import _format_alarm_next
from samsara.ui.status_overlay import _format_alarm_remaining


def test_settings_alarm_next_states_and_local_time():
    now = datetime(2026, 7, 14, 9, 0).timestamp()
    later = datetime(2026, 7, 14, 9, 30).timestamp()

    assert _format_alarm_next(None, enabled=False, now=now) == "—"
    assert _format_alarm_next(None, enabled=True, now=now) == "Paused"
    assert _format_alarm_next(now, enabled=True, now=now) == "Due"
    assert _format_alarm_next(later, enabled=True, now=now) == "9:30 AM"
    assert _format_alarm_next(later, active=True, now=now) == "Active"


def test_status_alarm_remaining_states_and_rounding():
    now = 1000.0

    assert _format_alarm_remaining(None, enabled=False, now=now) == "disabled"
    assert _format_alarm_remaining(None, enabled=True, now=now) == "paused"
    assert _format_alarm_remaining(now, enabled=True, now=now) == "due"
    assert _format_alarm_remaining(now + 1, enabled=True, now=now) == "in 1 min"
    assert _format_alarm_remaining(now + 61, enabled=True, now=now) == "in 2 min"
    assert _format_alarm_remaining(now + 3660, enabled=True, now=now) == "in 1 hr 1 min"
    assert _format_alarm_remaining(now + 1, active=True, now=now) == "active now"
