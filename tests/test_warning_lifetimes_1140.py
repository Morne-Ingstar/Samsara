"""1140: one-off warnings time out; condition warnings last until cleared."""
from samsara.message_durations import (
    MAX_WARNING_MS, MIN_WARNING_MS, message_duration_ms, message_protected,
    message_should_wait, ReceiptDwell)
from samsara.message_lifetime import MessageSlot

REFUSED = "Didn't catch that - say it again"


def test_one_off_warning_has_a_reading_time():
    ms = message_duration_ms(REFUSED, 'warning', 1800)
    assert MIN_WARNING_MS <= ms <= MAX_WARNING_MS


def test_long_warning_is_capped():
    assert message_duration_ms('x' * 500, 'warning', 1800) == MAX_WARNING_MS


def test_condition_warning_without_ttl_stays():
    assert message_duration_ms('mic missing', 'warning', None) is None
    assert message_protected('mic missing', 'warning', None, 10**6)


def test_non_result_warning_stays_with_its_owner():
    assert message_duration_ms('x', 'warning', 1800, purpose='running') is None


def test_expired_warning_is_not_protected_and_does_not_block():
    ms = message_duration_ms(REFUSED, 'warning', 1800)
    assert message_protected(REFUSED, 'warning', 100 + ms / 1000, 100 + 1)
    assert not message_protected(REFUSED, 'warning', 100 + ms / 1000, 100 + ms / 1000 + 1)
    assert not message_should_wait('warning', 'warning', protected=False)


def test_errors_still_stay():
    # 1141: a one-off error (numeric ttl) now fades; only ttl None persists.
    assert message_duration_ms('Paste failed', 'error', 1800) is not None
    assert message_duration_ms('Paste failed', 'error', None) is None


def test_slot_warning_ends_and_newer_message_not_cleared_by_old_timer():
    now = [0.0]
    slot = MessageSlot(clock=lambda: now[0])
    slot.offer(REFUSED, 'warning', 1800)
    first = slot.current
    assert first.deadline is not None
    now[0] = first.deadline + 0.1
    assert slot.advance() is None
    # a newer message shown after expiry survives the first one's deadline
    slot.offer('typed', 'success', 1800)
    newer = slot.current
    now[0] = first.deadline + 0.2
    assert slot.advance() is newer


def test_condition_warning_clears_on_producer_dismiss():
    now = [0.0]
    slot = MessageSlot(clock=lambda: now[0])
    slot.offer('mic missing', 'warning', None)
    now[0] = 10**5
    assert slot.advance() is not None
    slot.dismiss()
    assert slot.current is None


def test_receipt_dwell_warning_times_out():
    shown, timers = [], []
    d = ReceiptDwell(lambda *a: shown.append(a), timers.append)
    d.show(REFUSED, kind='warning')
    assert timers and timers[0] >= MIN_WARNING_MS
    d.deadline = 0.0
    assert d.restore() is False


def test_refused_decode_wording_is_plain():
    for banned in ('decode', 'staged', 'confidence'):
        assert banned not in REFUSED.lower()
