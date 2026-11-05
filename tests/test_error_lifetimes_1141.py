"""1141: one-off errors fade by reading time; ongoing conditions persist."""
import pytest

from samsara import message_durations as md
from samsara.failure_reasons import reason_for
from tests.test_message_lifetime_1038 import display


def test_one_off_error_fades_by_reading_time():
    d = md.message_duration_ms('Nothing heard - check the mic, or speak louder', 'error')
    assert md.MIN_WARNING_MS <= d <= md.MAX_WARNING_MS


def test_error_with_button_may_last_longer():
    long = 'x' * 400
    assert md.message_duration_ms(long, 'error') == md.MAX_WARNING_MS
    assert md.message_duration_ms(long, 'error', action=True) == md.MAX_ACTION_MS


def test_error_without_ttl_is_a_condition():
    assert md.message_duration_ms('Mic lost', 'error', None) is None
    assert md.message_protected('Mic lost', 'error', None, 0)


def test_expired_error_does_not_block_newer_message():
    assert md.message_protected('Oops', 'error', 10.0, 5.0)
    assert not md.message_protected('Oops', 'error', 10.0, 11.0)


def test_receipt_dwell_error_expires():
    shown, timers = [], []
    dwell = md.ReceiptDwell(lambda *a: shown.append(a), timers.append)
    dwell.show('Paste failed', False)
    assert timers and dwell.deadline is not None
    dwell.deadline = md.monotonic() - 1
    assert dwell.restore() is False


def test_failure_wording_and_echo_chip_plain():
    from samsara.transcript_gates import _CONTEXT_ECHO_CHIP
    assert 'staged' not in _CONTEXT_ECHO_CHIP
    text, hint = reason_for('long_failure', {'config': {}})
    assert text and hint == 'Report a problem'


def test_windows_warning_has_clearing_owner():
    from samsara.live_surface.feedback import feedback_for, WINDOWS_WARNING_OWNER
    assert feedback_for('warning', source='windows_warning').owner == WINDOWS_WARNING_OWNER


def test_mic_recovery_clears_windows_warning(monkeypatch):
    from samsara import microphone_recovery as mr
    from samsara.live_surface import feedback
    cleared = []
    monkeypatch.setattr(feedback, 'schedule_owner_clear', lambda app, owner: cleared.append(owner))
    monkeypatch.setattr(mr, '_boot_grace', lambda *a, **k: True)
    class App:
        _no_microphone = True
        _using_default_mic = False
    mr.set_state(App(), False)
    assert cleared == [feedback.WINDOWS_WARNING_OWNER]


@pytest.mark.parametrize('handoff', ['timer', 'clear', 'incoming'])
def test_delayed_queue_starts_error_reading_time_on_display(display, handoff):
    d = display
    d.show('First problem', 'error')
    d.show('Second problem', 'error')
    d.show('Later information')
    assert d.text() == 'First problem'

    # Simulate a delayed UI callback: queued messages have never been displayed.
    d.clock[0] = 600
    if handoff == 'timer':
        d.expire()
    elif handoff == 'clear':
        d.i.clear_outcome(); d.flush()
    else:
        d.show('Latest information')
    assert d.text() == 'Second problem'
    deadline = d.c.view().notice.expires_at if d.c else d.i._chip_deadline
    assert deadline == 604

    # Routine hide/clear and stale timer callbacks cannot erase the promoted error.
    for seconds in (600, 601, 603.999):
        d.clock[0] = seconds
        d.i.clear_outcome(); d.i.hide(); d.flush(); d.expire()
        assert d.text() == 'Second problem'
    d.clock[0] = 604
    d.expire()
    assert d.text() == ('Latest information' if handoff == 'incoming' else 'Later information')
    d.clock[0] = 606.499; d.expire()
    assert d.text()
    d.clock[0] = 606.5; d.expire()
    assert d.text() == ''


def test_expired_error_clear_and_hide_leave_no_persistent_message(display):
    d = display
    d.show('Synthetic problem', 'error')
    d.clock[0] = 4
    d.i.clear_outcome(); d.i.hide(); d.flush(); d.expire()
    assert d.text() == ''
