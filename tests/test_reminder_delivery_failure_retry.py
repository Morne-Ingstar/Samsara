"""Test for reminder delivery ordering + bounded retry (P1-B).

Ordering regression (covered here for completeness): a reminder used to be
marked delivered as soon as show_notification() was POSTED, not once
delivery was actually confirmed -- a delivery failure (e.g. the overlay
raising) would then lose the reminder forever, since last_fired was already
recorded. The fix (samsara/notifications.py _fire_reminder/
_mark_reminder_fired) defers recording last_fired until reminder_toast's
on_shown callback confirms the toast row was really drawn -- so both a
synchronous show_notification() failure AND on_shown silently never firing
(async drop) must leave the reminder pending for the next cycle to retry.

Bounded retry (this task's addition): without a cap, a reminder whose
delivery keeps failing would retry every 30s forever. _fire_reminder now
disables a reminder after _MAX_DELIVERY_ATTEMPTS consecutive unconfirmed
attempts, logging why, instead of spinning indefinitely -- but an
intermittent failure that eventually succeeds must NOT count toward that
cap (the attempt counter clears on confirmed delivery).

Each scenario below uses its own NotificationManager/reminder (own tmp_path
subdir) so the attempt counter -- which correctly accumulates across
repeated _fire_reminder calls against the SAME reminder, by design -- can't
leak between what are logically independent scenarios. NotificationManager
only needs a filesystem config_dir (no audio/Whisper/Qt setup), so it's
instantiated directly rather than via the Mock-as-self pattern
tests/test_dictation_app.py uses for the much heavier DictationApp.
"""
import sys
from pathlib import Path
from unittest.mock import patch
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.notifications import NotificationManager, _MAX_DELIVERY_ATTEMPTS


def _make_manager_and_reminder(tmp_path, name):
    mgr = NotificationManager(tmp_path / name)
    reminder = mgr.add_reminder(
        name="Test Reminder",
        schedule={"type": "interval", "minutes": 60},
        message="Take a break",
    )
    return mgr, reminder


def test_reminder_delivery_failure_retry(tmp_path):
    now = datetime.now()

    # 1) Synchronous delivery failure must NOT mark the reminder fired --
    #    it stays pending so the next cycle retries it.
    mgr, reminder = _make_manager_and_reminder(tmp_path, "sync_failure")
    with patch.object(mgr, 'show_notification', return_value=False):
        mgr._fire_reminder(reminder, now)
    assert reminder['last_fired'] is None
    assert reminder.get('enabled', True) is True

    # 2) show_notification() returning True without on_shown ever firing
    #    (silently dropped mid-shutdown, widget construction raised
    #    afterward, etc.) must ALSO be treated as undelivered.
    mgr, reminder = _make_manager_and_reminder(tmp_path, "async_drop")

    def _posted_but_never_shown(title, message, on_shown=None):
        return True  # on_shown deliberately never called

    with patch.object(mgr, 'show_notification', side_effect=_posted_but_never_shown):
        mgr._fire_reminder(reminder, now)
    assert reminder['last_fired'] is None

    # 3) An intermittent failure that eventually succeeds must clear the
    #    attempt counter -- it does NOT count toward the retry cap.
    mgr, reminder = _make_manager_and_reminder(tmp_path, "intermittent")
    calls = {"n": 0}

    def _flaky_then_ok(title, message, on_shown=None):
        calls["n"] += 1
        if calls["n"] <= 1:
            return False
        on_shown()
        return True

    with patch.object(mgr, 'show_notification', side_effect=_flaky_then_ok):
        mgr._fire_reminder(reminder, now)  # fails
        mgr._fire_reminder(reminder, now)  # succeeds
    assert reminder['last_fired'] is not None
    assert reminder.get('enabled', True) is True
    assert mgr._delivery_attempts.get(reminder['id']) is None

    # 4) A reminder that fails to confirm delivery _MAX_DELIVERY_ATTEMPTS
    #    times in a row is disabled instead of retried forever.
    mgr, reminder = _make_manager_and_reminder(tmp_path, "bounded_retry")
    with patch.object(mgr, 'show_notification', return_value=False) as mock_show:
        for _ in range(_MAX_DELIVERY_ATTEMPTS + 1):
            mgr._fire_reminder(reminder, now)

    # Exactly _MAX_DELIVERY_ATTEMPTS real delivery attempts were made -- the
    # extra call detected exhaustion and gave up instead of trying again.
    assert mock_show.call_count == _MAX_DELIVERY_ATTEMPTS
    stored = mgr.get_reminder(reminder['id'])
    assert stored['enabled'] is False
    assert stored.get('delivery_failed') is True
    assert stored['last_fired'] is None
    assert mgr._delivery_attempts.get(reminder['id']) is None
