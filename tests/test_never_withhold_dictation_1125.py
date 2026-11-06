"""1125: good words survive, retry inserts only omissions on either display."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara import decode_recovery as decoder, dictation_retry as retry, speech_presence
from samsara.message_lifetime import consume_acknowledgement
from tests.test_silent_hold_387 import make_worker
from tests.test_hold_decoder_1052d import configure, evidence, segment
from tests.test_message_lifetime_1038 import display


@pytest.mark.parametrize('path', ['stream', 'batch'])
@pytest.mark.parametrize('failed', [0, 1, 2])
def test_failed_chunk_keeps_other_chunks_in_order(make_worker, monkeypatch, path, failed):
    worker, session, audio = configure(make_worker, monkeypatch, 65)
    chunks = decoder.chunk_audio(audio, 16000, evidence(65, [(0, 65)]))
    def transcribe(pcm, **params):
        offset = (pcm.ctypes.data - audio.ctypes.data) // audio.itemsize
        index = next(i for i, chunk in enumerate(chunks) if chunk.start == offset)
        if index == failed:
            raise RuntimeError('synthetic chunk failure')
        return iter([segment(f'Sentence {index}.')]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    text = (decoder.stream_final(worker, audio, {})[2] if path == 'stream' else
            decoder.decode_hold(session.app, audio, {}, 65).text)
    assert text == ' '.join(f'Sentence {i}.' for i in range(3) if i != failed)
    slot = session.app._hold_retry_audio
    assert len(slot.ranges) == 1
    assert slot.ranges[0].start == chunks[failed].core_start / 16000
    assert slot.ranges[0].end == chunks[failed].core_end / 16000


def test_lazy_iterator_failure_keeps_already_decoded_segment(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    def broken():
        yield segment('First sentence.', 0, 1)
        raise RuntimeError('synthetic lazy failure')
    session.app.model.transcribe.side_effect = lambda *a, **k: (broken(), NS(language='en'))
    assert decoder.stream_final(worker, audio, {})[2] == 'First sentence.'
    assert session.app._hold_retry_audio.ranges == (retry.MissedRange(1., 4.),)


def test_bad_quality_segment_does_not_erase_its_neighbors(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([
        segment('First sentence.', 0, 1), segment('Rejected.', 1, 2, compression_ratio=12.),
        segment('Last sentence.', 2, 3)]), NS(language='en'))
    assert decoder.stream_final(worker, audio, {})[2] == 'First sentence. Last sentence.'
    assert session.app._hold_retry_audio.ranges == (retry.MissedRange(1., 2.),)


@pytest.mark.parametrize('path', ['stream', 'batch'])
@pytest.mark.parametrize('duration', [.032, .160])
def test_any_measured_speech_relaxes_once(make_worker, monkeypatch, path, duration):
    worker, session, audio = configure(make_worker, monkeypatch, 2.3)
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: evidence(2.3, [(.704, .704 + duration)], level=.15))
    calls = []
    def transcribe(pcm, **params):
        calls.append(params)
        return iter([] if len(calls) == 1 else [segment('Short phrase.')]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    params = dict(no_speech_threshold=.6, log_prob_threshold=-1., compression_ratio_threshold=2.4)
    text = (decoder.stream_final(worker, audio, params)[2] if path == 'stream' else
            decoder.decode_hold(session.app, audio, params, 2.3).text)
    assert text == 'Short phrase.'
    assert len(calls) == 2 and calls[1]['no_speech_threshold'] is None
    assert calls[1]['log_prob_threshold'] == -1. and calls[1]['compression_ratio_threshold'] == 2.4


@pytest.mark.parametrize('path', ['stream', 'batch'])
def test_silence_has_no_decode_note_or_retry(make_worker, monkeypatch, path):
    worker, session, audio = configure(make_worker, monkeypatch, 2.3, spans=[])
    text = (decoder.stream_final(worker, audio, {})[2] if path == 'stream' else
            decoder.decode_hold(session.app, audio, {}, 2.3).text)
    assert text == ''
    session.app.model.transcribe.assert_not_called()
    session.app._show_outcome_chip.assert_not_called()
    assert retry.live(session.app) is None


def install_retry(app, monkeypatch):
    app._schedule_ui = lambda fn, *a: fn(*a)
    app._paste_preserving_clipboard = Mock(return_value=True)
    app._filter_dictation_language = lambda *a: True
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, fn, **kw: fn())


@pytest.mark.parametrize('lane', ['dictation_hold', 'command_key', 'ava_hold', 'wake'])
def test_voice_retry_in_every_lane_inserts_only_missing_part(make_worker, monkeypatch, lane):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    install_retry(session.app, monkeypatch)
    decoder.retain(session.app, audio, {}, 'synthetic', [retry.MissedRange(1., 2.)])
    seen = []
    def transcribe(pcm, **params):
        seen.append(pcm.copy())
        return iter([segment('Recovered words.')]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    from samsara.ava_pending import consume
    assert consume(session.app, 'Try again.', capture_path=lane)
    session.app._paste_preserving_clipboard.assert_called_once_with('Recovered words.', return_delivery_confirmation=True)  # 1165: explicit receipt
    np.testing.assert_array_equal(seen[0], audio[16000:32000])
    assert not consume_acknowledgement(session.app, 'try again')


@pytest.mark.parametrize('empty', [False, True])
def test_note_button_redecodes_and_inserts_on_both_displays(display, make_worker, monkeypatch, qapp, empty):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    app = session.app
    install_retry(app, monkeypatch)
    app.listening_indicator = display.i
    app._show_outcome_chip = lambda *a: display.i.show_outcome(*a)  # 1165: show retry completion too
    decoder.retain(app, audio, {}, 'synthetic', [retry.MissedRange(1., 2.)])
    app.model.transcribe.side_effect = lambda *a, **k: (iter([segment('Recovered words.')]), NS(language='en'))
    retry.show_note(app, empty=empty)
    display.flush(); qapp.processEvents()
    assert display.text() == (retry.EMPTY if empty else retry.MISSED)
    button = display.band().report
    assert button.text() == 'Try again' and button.isVisible()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    display.flush(); qapp.processEvents()
    app._paste_preserving_clipboard.assert_called_once_with('Recovered words.', return_delivery_confirmation=True)  # 1165
    assert display.text() == retry.RETRY_DONE  # 1165: every retry has a visible result


def test_information_note_lasts_eight_seconds_and_clears_at_next_dictation(display, qapp):
    d = display
    d.i.show_retry_notice(retry.MISSED, lambda: None)
    d.flush(); qapp.processEvents()
    if d.c:
        assert d.c.view().notice.feedback.value == 'accent'
    else:
        assert d.i._chip_kind == 'accent'
    d.clock[0] = 7.999; d.expire(); d.flush()
    assert d.text() == retry.MISSED
    d.clock[0] = 8.; d.expire(); d.flush()
    assert d.text() == ''
    d.i.show_retry_notice(retry.EMPTY, lambda: None); d.flush()
    d.i.set_listening(True); d.flush()
    assert d.text() == ''


def test_yes_and_expired_try_again_are_ordinary_text(monkeypatch):
    app = NS(model_rate=16000)
    now = [10.]
    monkeypatch.setattr(retry.time, 'monotonic', lambda: now[0])
    decoder.retain(app, np.ones(16000), {}, 'synthetic')
    assert retry.claim(app, 'yes') is None
    now[0] = 18.
    assert retry.claim(app, 'try again') is None


def test_phrase_started_in_window_survives_decode_latency(monkeypatch):
    app = NS(model_rate=16000)
    now = [10.]
    monkeypatch.setattr(retry.time, 'monotonic', lambda: now[0])
    decoder.retain(app, np.ones(16000), {}, 'synthetic')
    now[0] = 17.
    retry.begin_capture(app)
    now[0] = 20.
    assert retry.claim(app, 'try again') is not None
    assert retry.claim(app, 'try again') is None


def test_stripped_segment_retry_does_not_duplicate_delivered_words():
    assert retry.remaining_text('Keep these four useful words. Missing middle.',
                                'Keep these four useful words.') == 'Missing middle.'


def test_final_filter_keeps_safe_pending_segment_but_drops_unsupported_outro(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    def broken():
        yield segment('Thank you.', 0, 1, avg_logprob=-3., no_speech_prob=.99)
        raise RuntimeError('synthetic native failure')
    monkeypatch.setattr('samsara.hallucination_filter._outro_is_unsupported', lambda *a: True)
    session.app.model.transcribe.side_effect = lambda *a, **k: (broken(), NS(language='en'))
    assert not decoder.stream_final(worker, audio, {})[2]


def test_legacy_notice_fallback_uses_information_and_plain_text():
    from samsara.failure_reasons import report_retained_failure
    app = NS(listening_indicator=NS(show_outcome=Mock()))
    report_retained_failure(app)
    app.listening_indicator.show_outcome.assert_called_once_with("Didn't catch that - Try again", 'info', 8000)


def test_partial_capture_loss_still_delivers_arrived_words(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    session.app._dictation_consumer.capture_loss = True
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([segment('Arrived words.')]), NS(language='en'))
    worker._final_pass()
    assert session.on_final.call_args.args == ('Arrived words.',)


def test_dropped_loop_retry_decodes_only_its_timed_segment(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    install_retry(session.app, monkeypatch)
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([
        segment('First sentence.', 0, 1), segment('beep beep beep beep', 1, 2),
        segment('Last sentence.', 2, 3)]), NS(language='en'))
    assert decoder.stream_final(worker, audio, {})[2] == 'First sentence. Last sentence.'
    assert session.app._hold_retry_audio.ranges == (retry.MissedRange(1., 2.),)
    seen = []
    def transcribe(pcm, **params):
        seen.append(pcm)
        return iter([segment('Missing middle.')]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    assert retry.request(session.app)
    assert len(seen[0]) == 16000
    session.app._paste_preserving_clipboard.assert_called_once_with('Missing middle.', return_delivery_confirmation=True)  # 1165


def overflow(consumer, monkeypatch, where):
    """Exercise the real overflow checks, including finish without another frame."""
    from samsara.audio_engine import hold_capture
    monkeypatch.setattr(hold_capture.preduck, 'begin', lambda c: None)
    monkeypatch.setattr(hold_capture.preduck, 'finish', lambda c: None)
    consumer._reader = NS(_read_cursor=0)
    consumer._engine = NS(_overflow_count=0)
    consumer._capture_logger = Mock()
    consumer._capture_not_after = None
    hold_capture.begin(consumer)
    hold_capture.observe(consumer, NS(seq=0, device_epoch=1))
    consumer._engine._overflow_count = 1
    if where == 'middle':
        hold_capture.observe(consumer, NS(seq=1, device_epoch=1))
    hold_capture.finish(consumer)
    assert consumer.capture_loss is True


def wire_delivery(worker, fake, monkeypatch):
    from samsara.streaming import StreamingSession
    from samsara.failure_reasons import deliver_stream_final
    session = StreamingSession.__new__(StreamingSession)
    session.__dict__.update(fake.__dict__)
    session._state_lock = threading.Lock()
    session._parking_enabled = session._direct_paste = False
    session._last_partial = ''
    session._overlay = Mock()
    session._mark_done = Mock()
    session._paste_with_retry = Mock()
    session.app._schedule_ui = lambda fn, *a: fn(*a)
    session.app._log_history = Mock()
    monkeypatch.setattr('samsara.tutorial_capture.record_final', lambda *a: True)
    monkeypatch.setattr('samsara.benchmark_store.capture_held_final', lambda *a: None)
    worker._session = session
    session.on_final = lambda text, **kw: deliver_stream_final(session, text,
        kw.get('raw_text'), kw.get('duration_s', 0), kw.get('elapsed_ms', 0))
    return session


@pytest.mark.parametrize('where', ['middle', 'end'])
def test_overflow_reaches_real_final_delivery_and_keeps_incident(make_worker, monkeypatch, tmp_path, caplog, where):
    import json
    import logging
    from samsara import hold_recording_recovery as incident
    worker, fake, audio = configure(make_worker, monkeypatch, 4)
    session = wire_delivery(worker, fake, monkeypatch)
    session.app.config['diagnostics'] = {'keep_incident_holds': True}
    monkeypatch.setattr(incident, 'incident_folder', lambda: tmp_path)
    incident.begin_incident(session)
    overflow(session.app._dictation_consumer, monkeypatch, where)
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([
        segment('First words.', 0, 1), segment('Last words.', 2, 3)]), NS(language='en'))
    store = session.app._incident_store_1052a
    try:
        with caplog.at_level(logging.INFO):
            incident.run_incident_final(worker)
        session._paste_with_retry.assert_called_once_with('First words. Last words.')
        session.app._show_outcome_chip.assert_called_once_with(retry.MISSED + ' - Try again', 'info', 8000)
        assert 'known_gap_ms=0 overflow_gap_ms=unknown' in caplog.text
        assert '[HOLD_FINAL] segments_decoded=' in caplog.text
        assert 'final failed; audio retained' not in caplog.text
        store.worker.shutdown(wait=True)
        sidecar, = tmp_path.glob('hold_*.json')
        assert 'capture_gap' in json.loads(sidecar.read_text())['reasons']
        assert sidecar.with_suffix('.wav').exists()
    finally:
        if hasattr(store, 'expiry'):
            store.expiry.cancel()
        store.worker.shutdown(wait=True)


@pytest.mark.parametrize('where', ['none', 'middle', 'end'])
def test_silent_final_delivery_has_no_note_even_after_overflow(make_worker, monkeypatch, where):
    worker, fake, audio = configure(make_worker, monkeypatch, 4, spans=[])
    session = wire_delivery(worker, fake, monkeypatch)
    monkeypatch.setattr(speech_presence._presence, 'measure', lambda *a: evidence(4))
    if where != 'none':
        overflow(session.app._dictation_consumer, monkeypatch, where)
    worker._final_pass()
    session._paste_with_retry.assert_not_called()
    session.app.model.transcribe.assert_not_called()
    session.app._show_outcome_chip.assert_not_called()
    assert not session._overlay.update_text.call_args.args[0]
    assert retry.live(session.app) is None


def test_one_weak_speech_frame_survives_early_final_gate(make_worker, monkeypatch):
    worker, fake, audio = configure(make_worker, monkeypatch, 2.3)
    session = wire_delivery(worker, fake, monkeypatch)
    measured = evidence(2.3, [(.704, .736)], level=.15)
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: measured)
    monkeypatch.setattr(speech_presence._presence, 'measure', lambda *a: measured)
    calls = []
    def transcribe(pcm, **params):
        calls.append(params)
        return iter([] if len(calls) == 1 else [segment('Short phrase.')]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    worker._final_pass()
    session._paste_with_retry.assert_called_once_with('Short phrase.')
    assert len(calls) == 2 and calls[-1]['no_speech_threshold'] is None
    session.app._show_outcome_chip.assert_not_called()


def test_retry_returning_nothing_keeps_a_working_retry(make_worker, monkeypatch):
    worker, session, audio = configure(make_worker, monkeypatch, 4, spans=[])
    install_retry(session.app, monkeypatch)
    decoder.retain(session.app, audio, {}, 'synthetic')
    assert retry.request(session.app)
    session.app._paste_preserving_clipboard.assert_not_called()
    assert retry.live(session.app) is not None
    session.app._show_outcome_chip.assert_called_once_with(retry.RETRY_FAILED + ' - Try again', 'info', 8000)  # 1165: distinguish failed retry


@pytest.mark.parametrize('retry_fails', ['empty', 'exception'])
def test_failed_loop_retry_keeps_first_attempt_good_neighbors(make_worker, monkeypatch, retry_fails):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    calls = []
    def transcribe(pcm, **params):
        calls.append(params)
        if len(calls) == 1:
            return iter([segment('First words.', 0, 1), segment('beep beep beep beep', 1, 2),
                         segment('Last words.', 2, 3)]), NS(language='en')
        if retry_fails == 'exception':
            raise RuntimeError('synthetic retry failure')
        return iter([]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    assert decoder.stream_final(worker, audio, {})[2] == 'First words. Last words.'
    assert len(calls) == 2
    assert session.app._hold_retry_audio.ranges == (retry.MissedRange(1., 2.),)
