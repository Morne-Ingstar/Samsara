"""1244: receipt combinations retain uncertainty and clipboard loss together."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, clipboard_recovery as recovery, paste
from samsara import session_feedback as feedback, session_modes as facade


COMBINED_WARNING = ("Sent, but not confirmed, and the old clipboard couldn't be "
                    "fully restored. Your words are in History.")


@pytest.mark.parametrize('kind', ['dictate_committed', 'dictate_injected'])
@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('restored', [False, True])
@pytest.mark.parametrize('image_removed', [False, True])
def test_session_receipt_matrix(kind, confirmed, restored, image_removed):
    detail = {'delivery_unverified': not confirmed,
              'clipboard_restore_failed': not restored,
              'clipboard_image_removed': image_removed}
    before = deepcopy(detail)
    if not restored:
        expected = (recovery.RESTORE_MESSAGE if confirmed else COMBINED_WARNING, 'warning')
    elif image_removed:
        expected = (recovery.IMAGE_REMOVED_CONFIRMED if confirmed
                    else recovery.IMAGE_REMOVED_UNCONFIRMED, 'warning')
    else:
        expected = ('typed', 'success') if confirmed else ('sent, unconfirmed', 'warning')
    assert feedback.outcome_chip(kind, detail) == expected
    assert facade.outcome_chip(kind, detail) == expected
    assert detail == before


@pytest.mark.parametrize('sent', [False, True])
@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('restored', [False, True])
def test_standalone_sent_confirmed_restored_matrix(sent, confirmed, restored):
    app = SimpleNamespace(_show_outcome_chip=Mock(), play_sound=Mock(),
                          _schedule_ui=lambda fn: fn(),
                          listening_indicator=SimpleNamespace(flash_success=Mock()))
    outcome = cb.DeliveryOutcome.CONFIRMED if confirmed else cb.DeliveryOutcome.UNVERIFIED
    token = recovery._attempt.set(recovery.Attempt(restore_failed=not restored))
    try:
        assert paste.show_delivery_result(app, sent, outcome) is sent
    finally:
        recovery._attempt.reset(token)
    app._show_outcome_chip.assert_called_once()
    message, severity, *ttl = app._show_outcome_chip.call_args.args
    if not sent:
        assert (message, severity) == (recovery.RESTORE_NOT_SENT if not restored
                                      else paste.FAILED_DELIVERY, 'error')
    elif not restored or confirmed:
        # Clipboard-loss receipts match both session routes. Ordinary
        # unconfirmed delivery already uses longer standalone History copy.
        for kind in ('dictate_committed', 'dictate_injected'):
            assert (message, severity) == feedback.outcome_chip(kind, {
                'delivery_unverified': not confirmed,
                'clipboard_restore_failed': not restored})
        if not restored and not confirmed:
            assert message == COMBINED_WARNING
    else:
        assert (message, severity) == (paste.UNVERIFIED_DELIVERY, 'warning')
    if not (sent and confirmed and restored):
        app.play_sound.assert_not_called()
        app.listening_indicator.flash_success.assert_not_called()
