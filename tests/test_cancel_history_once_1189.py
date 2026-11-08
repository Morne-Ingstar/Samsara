"""One cancelled/failed take keeps one privacy-aware History identity (1189)."""
from types import SimpleNamespace as NS
import threading
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery as decoder, dictation_history as links, dictation_retry as retry
from samsara import mic_score, recent_privacy
from samsara.hold_final_policy import final_decode
from tests.test_lost_recording_1042 import capture, rows


def guidance(app):
    assert any('Try again' in call.args[0] for call in app._show_outcome_chip.call_args_list)


@pytest.mark.parametrize('preview', ['', 'Accepted synthetic preview.'])
def test_cancel_preserves_one_row_preview_audio_and_retry_link(capture, preview):
    session, app, pcm = capture
    session._last_partial = preview
    seen = []
    app._show_outcome_chip.side_effect = lambda label, *a: seen.append(links.link_for(app, label))
    session.cancel()
    slot = app._hold_retry_audio
    session.on_cancelled()
    session.on_final('Discarded synthetic late result.')
    row, = rows(app)
    assert row['id'] == slot.history_entry_id == seen[0].entry_id
    assert row['display_text'] == (preview or "Didn't catch that - Try again")
    assert row['duration_ms'] == 1200 and row['status'] == 'failed'
    np.testing.assert_array_equal(slot.audio, pcm)
    app.add_to_history.assert_not_called()
    guidance(app)


def test_stop_racing_cancel_reuses_the_same_row(capture):
    session, app, pcm = capture
    def stop():
        session.cancel()
        return pcm
    app._dictation_consumer.stop_streaming.side_effect = stop
    session._stop_capture()
    row, = rows(app)
    assert row['id'] == app._hold_retry_audio.history_entry_id
    assert 'Try again' in row['display_text']
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    guidance(app)


@pytest.mark.parametrize('reason', ['password_field', 'unverified_target'])
def test_failed_capture_with_recent_words_off_keeps_one_concealed_row(capture, monkeypatch, reason):
    session, app, pcm = capture
    app.config['home'] = {'show_recent_words': False}
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy(reason))
    session._stop_capture()
    session.on_final(None)
    row, = rows(app)
    assert row['id'] == app._hold_retry_audio.history_entry_id
    assert row['raw_text'] == row['display_text'] == row['app_context'] == ''
    assert row['privacy_reason'] == reason
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    guidance(app)


def test_no_history_store_keeps_audio_and_guidance_without_creating_a_row(capture):
    session, app, pcm = capture
    db, app.history_db = app.history_db, None
    session._stop_capture()
    session.on_final(None)
    assert len(db.recent()) == 0
    assert app._hold_retry_audio.history_entry_id is None
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    guidance(app)
    assert all('History' not in call.args[0] for call in app._show_outcome_chip.call_args_list)


def install_retry(app, monkeypatch, outcome='success'):
    text = '' if outcome == 'empty' else 'Recovered synthetic words.'
    decoded = NS(segments=[NS(text=text)] if text else [], info=NS(language='en'), missed=[])
    decode = Mock(return_value=decoded)
    monkeypatch.setattr(retry, 'decode_slot', decode)
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, fn, **kw: fn())
    app._filter_dictation_language = lambda *a: True
    app._paste_preserving_clipboard = Mock(return_value=(True, True))
    return decode


@pytest.mark.parametrize('outcome', ['success', 'empty'])
@pytest.mark.parametrize('preview', ['', 'Accepted synthetic preview.'])
def test_try_again_after_cancel_updates_original_row_and_binds_result(capture, monkeypatch, outcome, preview):
    session, app, pcm = capture
    session._last_partial = preview
    session.cancel()
    original, = rows(app)
    slot = app._hold_retry_audio
    guidance(app)
    decode = install_retry(app, monkeypatch, outcome)
    seen = []
    app._show_outcome_chip.side_effect = lambda label, *a: seen.append(links.link_for(app, label))
    assert retry.request(app, expected=slot)
    row, = rows(app)
    assert row['id'] == original['id'] == slot.history_entry_id
    assert seen[-1].entry_id == original['id']
    decode.assert_called_once()
    np.testing.assert_array_equal(decode.call_args.args[1].audio, pcm)
    if outcome == 'success':
        assert row['raw_text'] == row['display_text'] == 'Recovered synthetic words.'
        assert row['status'] == 'recovered'
        assert retry.RETRY_DONE in app._show_outcome_chip.call_args.args[0]
    else:
        assert row['display_text'] == original['display_text']
        assert app._hold_retry_audio.history_entry_id == row['id']
        assert retry.RETRY_FAILED in app._show_outcome_chip.call_args.args[0]
        guidance(app)


def test_recovered_private_take_cannot_reveal_concealed_history(capture, monkeypatch):
    session, app, _ = capture
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy('password_field'))
    session.cancel()
    original, = rows(app)
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy('', 'editor.exe'))
    install_retry(app, monkeypatch)
    assert retry.request(app)
    row, = rows(app)
    assert row['id'] == original['id']
    assert row['raw_text'] == row['display_text'] == row['app_context'] == ''
    assert row['privacy_reason'] == 'password_field'


def test_retry_cannot_recreate_deleted_take(capture, monkeypatch):
    session, app, _ = capture
    session.cancel()
    row, = rows(app)
    app.history_db.delete(row['id'])
    install_retry(app, monkeypatch)
    assert retry.request(app)
    assert rows(app) == []


def test_new_capture_cannot_reuse_previous_cancelled_take(capture):
    session, app, _ = capture
    session.cancel()
    original, = rows(app)
    with links.capture_history(app):
        app._log_history(raw_text='Synthetic new capture.', mode='streaming')
    assert len(rows(app)) == 2
    assert rows(app)[0]['id'] != original['id']
    assert app._hold_retry_audio.history_entry_id == original['id']


@pytest.mark.parametrize('outcome', ['success', 'empty'])
def test_spoken_try_again_carries_original_id_from_decode_to_delivery(capture, monkeypatch, outcome):
    session, app, pcm = capture
    session.cancel()
    original, = rows(app)
    slot = app._hold_retry_audio
    voice_session = NS(app=app, cancel_event=threading.Event(), _last_partial='',
                       _overlay=Mock(), _mark_done=Mock())
    worker = NS(_session=voice_session, _decoder_guard=lambda *a, **kw: None)
    command = decoder.DecodedHold(segments=[NS(text='try again', avg_logprob=-.2)],
                                  info=NS(language='en'))
    recovered = decoder.DecodedHold(
        segments=[NS(text='Recovered synthetic words.', avg_logprob=-.2)] if outcome == 'success' else [],
        info=NS(language='en'), missed=[] if outcome == 'success' else [retry.MissedRange(0, 1.2)])
    monkeypatch.setattr(decoder, '_transcribe', lambda *a, **kw: command)
    decode = Mock(return_value=recovered)
    monkeypatch.setattr(retry, 'decode_slot', decode)
    _, _, text = final_decode(worker, np.zeros(16000, np.float32), {})
    assert voice_session._hold_final_audit.retry_history_slot is slot
    np.testing.assert_array_equal(decode.call_args.args[1].audio, pcm)
    if outcome == 'success':
        mic_score.deliver_with_history(voice_session,
            lambda: app._log_history(raw_text=text, mode='streaming', status='recovered'))
    else:
        mic_score.deliver_with_history(voice_session, decoder.deliver_failure, voice_session, 1.2)
        guidance(app)
    row, = rows(app)
    assert row['id'] == original['id']
    assert row['display_text'] == (text if outcome == 'success' else "Didn't catch that - Try again")
