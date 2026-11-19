"""1165: failed long ranges, take-bound retries and honest browser receipts."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery as decoder, dictation_retry as retry, speech_presence
from samsara.hold_final_policy import FinalAudit, current_audit
from samsara import paste
from samsara.clipboard import DeliveryOutcome
from tests.test_hold_decoder_1052d import configure, evidence, segment
from tests.test_silent_hold_387 import make_worker
from tests.test_message_lifetime_1038 import display


def setup(make_worker, monkeypatch, seconds=17):
    worker, session, audio = configure(make_worker, monkeypatch, seconds)
    monkeypatch.setattr(speech_presence, 'measure',
        lambda pcm, rate: evidence(len(pcm) / rate, [(0, len(pcm) / rate)]))
    return worker, session, audio


@pytest.mark.parametrize('path', ['stream', 'batch'])
@pytest.mark.parametrize('failed_piece', [None, 1])
def test_long_rejected_interval_is_subdivided_and_neighbors_survive(make_worker, monkeypatch, path, failed_piece):
    worker, session, audio = setup(make_worker, monkeypatch)
    calls = []
    def transcribe(pcm, **params):
        start = (pcm.ctypes.data - audio.ctypes.data) / audio.itemsize / 16000
        calls.append((start, len(pcm) / 16000, params))
        if len(pcm) > 6 * 16000:
            values = [segment('Opening.', 0, 1),
                      segment('Rejected.', 1, 16, compression_ratio=12),
                      segment('Closing.', 16, 17)]
        else:
            index = round((start - 1) / 4.5)
            values = [segment('Rejected.' if index == failed_piece else f'Piece {index}.',
                .25 if index else 0, min(1.25, len(pcm) / 16000),
                compression_ratio=12 if index == failed_piece else 1.1)]
        return iter(values), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    result = (decoder.stream_final(worker, audio, {})[2] if path == 'stream'
              else decoder.decode_hold(session.app, audio, {}, 17).text)
    assert result.startswith('Opening. Piece 0.') and result.endswith('Closing.')
    assert 'Piece 2.' in result and 'Piece 3.' in result
    assert ('Piece 1.' in result) is (failed_piece is None)
    assert all(width <= 5 for _, width, _ in calls[1:])
    assert all(p['condition_on_previous_text'] is False for _, _, p in calls)
    assert all(p['temperature'] == .4 for _, _, p in calls[1:])
    if failed_piece is None:
        assert retry.live(session.app) is None
    else:
        assert all(p.end - p.start <= 5 for p in retry.live(session.app).ranges)


def test_recovery_split_prefers_vad_pause_and_preserves_all_samples():
    pcm = np.zeros(16 * 16000, np.float32)
    measured = evidence(16, [(0, 3), (3.5, 16)])
    chunks = decoder.chunk_audio(pcm, 16000, measured, recovery=True)
    assert 3 <= chunks[0].core_end / 16000 <= 3.5
    assert all(c.end - c.start <= 5 * 16000 for c in chunks)
    assert all(a.core_end == b.core_start for a, b in zip(chunks, chunks[1:]))
    assert chunks[-1].core_end == len(pcm)


def install(app, monkeypatch):
    app._schedule_ui = lambda fn, *a: fn(*a)
    app._paste_preserving_clipboard = Mock(return_value=(True, True))
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, fn, **kw: fn())


def test_old_retry_button_cannot_claim_newer_recording(make_worker, monkeypatch):
    worker, session, audio = setup(make_worker, monkeypatch, 2)
    app = session.app
    install(app, monkeypatch)
    app.listening_indicator = NS(show_retry_notice=Mock())
    decoder.retain(app, audio, {}, 'old')
    retry.show_note(app)
    callback = app.listening_indicator.show_retry_notice.call_args.args[1]
    old = app._hold_retry_audio
    decoder.retain(app, audio * 2, {}, 'new')
    newer = app._hold_retry_audio
    assert newer is not old
    assert callback() is False
    assert app._hold_retry_audio is newer
    app.model.transcribe.assert_not_called()
    app._show_outcome_chip.assert_called_with(retry.RETRY_EXPIRED, 'info', 8000)


def test_clicked_retry_uses_original_audit_and_smaller_different_decode(make_worker, monkeypatch, caplog):
    worker, session, audio = setup(make_worker, monkeypatch, 16)
    app = session.app
    install(app, monkeypatch)
    audit = FinalAudit()
    audit.hold_seq_id = 'synthetic-original-take'
    token = current_audit.set(audit)
    try:
        decoder.retain(app, audio, {}, 'fallback_exhausted')
    finally:
        current_audit.reset(token)
    seen = []
    def transcribe(pcm, **params):
        seen.append((len(pcm), params, current_audit.get().hold_seq_id))
        return iter([segment('Recovered.', compression_ratio=12)]), NS(language='en')
    app.model.transcribe.side_effect = transcribe
    with caplog.at_level(logging.WARNING):
        assert retry.request(app)
    assert len(seen) >= 4 and max(n for n, _, _ in seen) <= 5 * 16000
    assert all(p['temperature'] == .4 and key == audit.hold_seq_id for _, p, key in seen)
    assert app._hold_retry_audio.hold_seq_id == audit.hold_seq_id
    assert 'hold_seq_id=unknown' not in caplog.text
    assert current_audit.get() is None


@pytest.mark.parametrize('outcome', ['success', 'empty', 'decode_error', 'paste_error', 'not_sent', 'unverified'])
def test_retry_always_reports_a_result_and_retains_failures(make_worker, monkeypatch, outcome):
    worker, session, audio = setup(make_worker, monkeypatch, 2)
    app = session.app
    install(app, monkeypatch)
    app._log_history = Mock()
    decoder.retain(app, audio, {}, 'synthetic')
    if outcome == 'decode_error':
        app.model.transcribe.side_effect = RuntimeError('synthetic failure')
    else:
        app.model.transcribe.side_effect = lambda *a, **k: (
            iter([] if outcome == 'empty' else [segment('Synthetic recovered words.')]), NS(language='en'))
    if outcome == 'paste_error':
        app._paste_preserving_clipboard.side_effect = RuntimeError('synthetic failure')
    elif outcome in ('not_sent', 'unverified'):
        app._paste_preserving_clipboard.return_value = (outcome == 'unverified', False)
    assert retry.request(app)
    messages = [call.args[0] for call in app._show_outcome_chip.call_args_list]
    if outcome == 'success':
        assert retry.RETRY_DONE in messages
        app._log_history.assert_called_once()
        assert retry.live(app) is None
    elif outcome == 'unverified':
        assert paste.UNVERIFIED_DELIVERY in messages
        assert 'typed' not in messages
    else:
        assert retry.RETRY_FAILED + ' - Try again' in messages
        assert retry.live(app) is not None


def test_recovery_keeps_near_silence_refusal(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 16, spans=[])
    app = session.app
    install(app, monkeypatch)
    decoder.retain(app, audio, {}, 'synthetic')
    assert retry.request(app)
    app.model.transcribe.assert_not_called()
    assert retry.live(app) is not None


def test_unverified_delivery_uses_warning_on_both_displays(display):
    app = display.app
    app.play_sound = Mock()
    app._show_outcome_chip = lambda *a: app.listening_indicator.show_outcome(*a)
    # 1165 n2: preserve 474's SENT contract; UNVERIFIED owns warning feedback.
    paste.show_delivery_result(app, True, DeliveryOutcome.UNVERIFIED)
    display.flush()
    assert display.text() == paste.UNVERIFIED_DELIVERY
    app.play_sound.assert_not_called()


def test_unconfirmed_text_offer_hook_has_no_automatic_paste():
    app = NS(_on_unconfirmed_delivery=Mock())
    paste.remember_unconfirmed(app, 'Synthetic words.', True, False, DeliveryOutcome.UNVERIFIED)
    assert app._unconfirmed_delivery_text == 'Synthetic words.'
    app._on_unconfirmed_delivery.assert_called_once_with('Synthetic words.', DeliveryOutcome.UNVERIFIED)
    paste.remember_unconfirmed(app, 'Verified.', True, True, DeliveryOutcome.CONFIRMED)
    assert app._unconfirmed_delivery_text == ''
