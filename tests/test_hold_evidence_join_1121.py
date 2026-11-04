"""Original-execution joins with synthetic PCM/editors; no live microphone."""
import json
import logging
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import benchmark_store, hold_diagnostics as evidence
from samsara import hold_recording_recovery as recovery
from samsara.hold_final_policy import FinalAudit, log_delivery
from samsara.speech_presence import Presence
from tests.test_silent_hold_387 import make_worker
from tests.test_scratch_after_unacknowledged_paste_783 import rig, deliver


def test_imported_command_fixture_does_not_change_plugin_declarations(tmp_path, monkeypatch):
    from samsara import plugin_commands
    from tests.test_commands_only_key_391 import _app
    before = {name: dict(entries) for name, entries in plugin_commands._MODULE_ENTRIES.items()}
    with monkeypatch.context() as local:
        _app(tmp_path, local, 'scroll down')
    # Imported helpers do not receive fixtures from their defining test module.
    assert plugin_commands._MODULE_ENTRIES == before


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    app = NS(config={'diagnostics': {'keep_incident_holds': True}}, model_rate=16000,
             recording=True, _incident_first_1052a=True)
    yield app
    store = getattr(app, '_incident_store_1052a', None)
    if store is not None:
        timer = getattr(store, 'expiry', None)
        if timer is not None:
            timer.cancel()
        store.worker.shutdown(wait=True)
    evidence._active = None


def hold(app, samples=1600):
    evidence.begin(app)
    session = NS(app=app, _decode_audio=np.full(samples, .05, np.float32),
                 _hold_final_audit=FinalAudit(), cancel_event=threading.Event())
    recovery.begin_incident(session)
    recovery.incident_released(session)
    session._hold_final_audit._hold_evidence_1121 = evidence.get(session)
    session._hold_final_audit.hold_seq_id = evidence.get(session).hold_seq_id
    return session


def sidecars(app):
    store = app._incident_store_1052a
    store.worker.submit(lambda: None).result(timeout=5)
    return sorted(store.folder.glob('hold_*.json'))


def metadata(app):
    return [json.loads(path.read_text()) for path in sidecars(app)]


def test_full_join_wav_and_late_delivery_receipt(owner, monkeypatch, caplog, tmp_path):
    from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
    from samsara.audio_engine.frame import FRAME_SIZE
    from samsara.audio_engine.ring import FrameBus
    clock = NS(now=100.)
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: clock.now)
    session = hold(owner, FRAME_SIZE * 3)
    bus = FrameBus()
    consumer = DictationSessionConsumer(NS(register_consumer=bus.new_reader), owner)
    consumer._hold_evidence_1121 = evidence.get(session)
    consumer._streaming_frames = [np.full(FRAME_SIZE, .05, np.float32),
                                  np.zeros(FRAME_SIZE, np.float32),
                                  np.zeros(FRAME_SIZE, np.float32)]
    consumer.wait_for_streaming_release_tail(should_abort=lambda: True)
    consumer.finish_capture(force=True)
    clock.now += .25
    with caplog.at_level(logging.DEBUG):
        pcm = consumer.stop_streaming()
        session._decode_audio = pcm
        evidence.decoded(session, 'Synthetic final words.')
        log_delivery(session, 'Synthetic final words.')
        session._hold_final_audit.drop('segment_quality')
        recovery.complete_incident(session, 'Synthetic final words.')
        assert metadata(owner)[0]['delivery_abort_reason'] == 'pending'
        evidence.receipt(session, 'Synthetic final words.', True, True)
    row, = metadata(owner)
    assert row['hold_seq_id'] == evidence.get(session).hold_seq_id
    assert row['captured_audio_ms'] == 300 and row['release_to_stop_ms'] == 250
    assert row['last_speech_end_ms'] == 100
    assert row['cutoff_set'] == 1 and row['last_speech_source'] == 'energy_tail_floor'
    assert row['decoded_words'] == row['request_words'] == row['delivered_words'] == 3
    assert row['delivery_abort_reason'] == 'none'
    assert set(evidence.Evidence().snapshot()) <= set(row)
    for prefix in ('[TAIL_CAPTURE]', '[HOLD_FINAL]', '[DELIVERY]'):
        lines = [r.message for r in caplog.records if r.message.startswith(prefix)]
        assert lines and all('hold_seq_id=' + row['hold_seq_id'] in line for line in lines)
    assert 'Synthetic final words.' not in caplog.text
    assert 'Synthetic final words.' not in json.dumps(row)
    (tmp_path / 'joined-numeric.log').write_text('\n'.join(r.message for r in caplog.records
        if r.message.startswith(('[TAIL_CAPTURE]', '[HOLD_FINAL]', '[DELIVERY]'))), encoding='utf-8')
    with wave.open(str(sidecars(owner)[0].with_suffix('.wav')), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == row['sample_count'] == len(pcm)
        np.testing.assert_array_equal(np.frombuffer(wav.readframes(len(pcm)), '<i2'),
                                      np.rint(pcm * 32768).astype('<i2'))


def test_ring_newest_five_and_independent_of_full_benchmark(owner, monkeypatch):
    owner.config['benchmark'] = {'collect_samples': True, 'max_samples': 200}
    monkeypatch.setattr(benchmark_store, '_read_rows', lambda: [{}] * 200)
    identities = []
    for index in range(8):
        session = hold(owner, 1600 + index)
        identities.append(evidence.get(session).hold_seq_id)
        session._hold_final_audit.drop('segment_quality')
        assert not benchmark_store.append_sample(owner, session._decode_audio, 16000, 'fixture', 'fixture', 'base')
        recovery.complete_incident(session, 'fixture')
        sidecars(owner)
    rows = metadata(owner)
    assert {row['hold_seq_id'] for row in rows} == set(identities[-5:])
    assert {row['sample_count'] for row in rows} == set(range(1603, 1608))
    assert len(list(owner._incident_store_1052a.folder.glob('*.wav'))) == 5


def test_consent_off_no_store_or_write(owner):
    owner.config['diagnostics']['keep_incident_holds'] = False
    session = hold(owner)
    session._hold_final_audit.drop('segment_quality')
    recovery.complete_incident(session, '')
    evidence.receipt(session, '', False, False)
    assert not hasattr(owner, '_incident_store_1052a')
    assert not recovery.incident_folder().exists()


@pytest.mark.parametrize('speech', [False, True, None])
@pytest.mark.parametrize('rehold_delay', [None, 4.99, 5.01])
def test_empty_chunk_floor_and_five_second_rehold(owner, speech, rehold_delay, monkeypatch):
    clock = NS(now=100.)
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: clock.now)
    session = hold(owner)
    evidence.presence(session, Presence(speech is not None, longest_s=.2 if speech else 0))
    session._hold_final_audit.empty_spoken_chunk = True
    session._decode_failure = 'Synthetic failure enum is observed only as a boolean'
    recovery.complete_incident(session, '')
    assert bool(metadata(owner)) is (speech is True or speech is None)
    if rehold_delay is not None:
        clock.now += rehold_delay
        hold(owner)
        rows = metadata(owner)
        if speech is False:
            assert bool(rows) is (rehold_delay <= 5)
        if rows:
            reasons = rows[0]['reasons']
            assert ('empty_spoken_chunk' in reasons) is (speech is True)
            assert ('immediate_rehold' in reasons) is (rehold_delay <= 5)


@pytest.mark.parametrize('retry_success', [True, False])
def test_no_speech_retry_always_triggers(owner, make_worker, monkeypatch, retry_success):
    from tests.test_hold_decoder_1052d import configure, segment
    worker, session, pcm = configure(make_worker, monkeypatch, 1.2)
    session.app.config['diagnostics'] = {'keep_incident_holds': True}
    session.app._incident_first_1052a = True
    evidence.begin(session.app)
    recovery.begin_incident(session)
    owner._incident_store_1052a = session.app._incident_store_1052a
    attempts = iter([[], [segment('Please move this.')] if retry_success else []])
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter(next(attempts)), NS(language='en'))
    worker._final_pass()
    assert session.app.model.transcribe.call_count == 2
    recovery.complete_incident(session, session.on_final.call_args.args[0])
    row, = metadata(owner)
    assert 'no_speech_retry' in row['reasons']
    assert row['no_speech_retries'] == 1
    assert row['decoded_words'] == (3 if retry_success else 0)


@pytest.mark.parametrize('point', ['complete', 'refresh'])
def test_clear_barriers_never_restore_slot(owner, monkeypatch, point):
    session = hold(owner)
    session._hold_final_audit.drop('segment_quality')
    store = owner._incident_store_1052a
    paused, resume = threading.Event(), threading.Event()
    if point == 'refresh':
        recovery.complete_incident(session, 'fixture')
        sidecars(owner)
        store.worker.submit(lambda: (paused.set(), resume.wait(5)))
        assert paused.wait(5)
        # clear() invalidates generation before its queued deletion; a late
        # delivery update is behind that deletion in the serialized writer.
        cleared = store.clear()
        evidence.receipt(session, 'fixture', True, True)
        resume.set()
        cleared.result(timeout=5)
    else:
        original = recovery.np.array
        def copying(*args, **kwargs):
            if threading.current_thread().name.startswith('join-barrier'):
                paused.set()
                assert resume.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(recovery.np, 'array', copying)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='join-barrier') as pool:
            future = pool.submit(recovery.complete_incident, session, 'fixture')
            try:
                assert paused.wait(5)
                store.clear().result(timeout=5)
            finally:
                resume.set()
            future.result(timeout=5)
    assert sidecars(owner) == []
    assert store.previous is None


def test_actual_cleanup_pipeline_has_individual_stage_deltas(make_worker, monkeypatch, caplog):
    from samsara import streaming
    from samsara.cleanup import clean_text
    from samsara.formatting_tokens import apply_formatting_tokens
    from tests.test_hold_tail_words_1026 import segment, audio
    raw = 'um marker new line keep alpha'
    worker, session = make_worker([segment(raw)])
    evidence.begin(session.app)
    recovery.begin_incident(session)
    session._stop_capture.return_value = audio()
    monkeypatch.setattr(streaming, 'clean_text', clean_text)
    # Two distinct correction passes change counts; aggregate both, while
    # real cleanup and formatting each remove their own known words.
    session.app.voice_training_window.apply_corrections.side_effect = lambda text: text.replace('marker', 'marker one')
    session.app.process_transcription.side_effect = lambda text: text
    session.app.config['smart_corrections'] = {'modes': {'streaming': True}}
    monkeypatch.setattr(streaming, 'smart_correct', lambda text, app: text.replace('alpha', 'alpha beta'))
    session.app._apply_formatting_tokens.side_effect = apply_formatting_tokens
    with caplog.at_level(logging.DEBUG):
        worker._final_pass()
    final = session.on_final.call_args.args[0]
    fields = evidence.get(session).snapshot()
    assert fields['decoded_words'] == 6
    assert fields['cleanup_word_delta'] == -1
    assert fields['formatting_word_delta'] == -2
    assert fields['corrections_word_delta'] == 2
    assert fields['request_words'] == len(final.split()) == 5
    assert fields['decoded_words'] + sum(fields[k + '_word_delta'] for k in ('cleanup', 'formatting', 'corrections')) == fields['request_words']
    assert raw not in caplog.text and final not in caplog.text


@pytest.mark.parametrize('acknowledged,ok', [(True, True), (False, True), (False, False)])
def test_real_paste_seam_receipt_counts(rig, acknowledged, ok, caplog):
    editor, app, _manager = rig
    session = NS(app=app)
    evidence.bind(session)
    text = 'Private synthetic delivery.'
    evidence.decoded(session, text)
    effect = None if acknowledged else lambda *a: None
    with caplog.at_level(logging.INFO):
        result = evidence.paste_call(session, text, lambda value: deliver(rig, value, effect=effect, ok=ok))
    assert bool(result) is ok
    fields = evidence.get(session).snapshot()
    assert fields['request_words'] == 3
    assert fields['delivered_words'] == (3 if acknowledged else -1 if ok else 0)
    assert fields['delivery_abort_reason'] == ('none' if acknowledged else 'unverified' if ok else 'paste_failed')
    assert fields['paste_abort_word_delta'] == (0 if ok else -3)
    assert text not in caplog.text
    assert evidence.delivery_context.get() is None


def test_cue_duck_intervals_union_clipped_at_capture_stop(owner, monkeypatch):
    clock = NS(now=10.)
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: clock.now)
    joined = evidence.begin(owner)
    evidence.cue(owner, .2)
    @evidence.observe_duck
    def operation():
        clock.now += .15
        return 42
    assert operation() == 42
    joined.stopped = 10.1
    fields = joined.snapshot()
    assert fields['cue_or_duck_overlap_ms'] == 100  # Union, not double counting.
    assert fields['cue_events'] == fields['duck_events'] == 1
    owner.recording = False
    evidence.cue(owner, 1.)
    operation()
    assert joined.snapshot() == fields


def test_vad_end_and_unknown(owner):
    session = hold(owner)
    evidence.presence(session, Presence(True, probabilities=(.2, 0., .6, 0.), samples=2048))
    assert evidence.get(session).snapshot()['last_speech_end_ms'] == 96
    assert evidence.get(session).snapshot()['last_speech_source'] == 'vad_p01'
    evidence.presence(session, Presence(False))
    assert evidence.get(session).snapshot()['speech_present'] == -1


def test_sequence_and_cross_hold_delivery_context_are_isolated(owner):
    first = hold(owner)
    second = hold(owner)
    a, b = evidence.get(first), evidence.get(second)
    assert a.hold_seq_id != b.hold_seq_id
    assert int(b.hold_seq_id.split('-')[0]) > int(a.hold_seq_id.split('-')[0])
    evidence.paste_call(first, 'fixture', lambda text: True)
    assert a.values['delivery_abort_reason'] == 'unverified'
    assert b.values['delivery_abort_reason'] == 'pending'


def test_late_refresh_cannot_recreate_rotated_slot(owner):
    first = hold(owner)
    first._hold_final_audit.drop('segment_quality')
    recovery.complete_incident(first, 'fixture')
    sidecars(owner)
    for _ in range(5):
        session = hold(owner)
        session._hold_final_audit.drop('segment_quality')
        recovery.complete_incident(session, 'fixture')
        sidecars(owner)
    evidence.receipt(first, 'fixture', True, True)
    rows = metadata(owner)
    assert len(rows) == 5
    assert evidence.get(first).hold_seq_id not in {row['hold_seq_id'] for row in rows}


@pytest.mark.parametrize('reason', ['paste_failed', 'paste_exception', 'elevated_target',
                                   'target_changed', 'privacy_refused', 'typing_failed'])
def test_failed_request_counts_and_incident_trigger(owner, reason):
    session = hold(owner)
    recovery.complete_incident(session, 'Three synthetic words')
    evidence.receipt(session, 'Three synthetic words', False, False, reason)
    assert evidence.get(session).values['delivery_abort_reason'] == reason
    assert evidence.get(session).values['delivered_words'] == 0
    assert evidence.get(session).values['paste_abort_word_delta'] == -3
    row, = metadata(owner)
    assert 'delivery_failed' in row['reasons']


@pytest.mark.parametrize('cancelled', [False, True])
def test_finished_without_paste_has_numeric_abort(owner, cancelled, caplog):
    session = hold(owner)
    evidence.decoded(session, 'Three synthetic words')
    if cancelled:
        session.cancel_event.set()
    recovery.complete_incident(session, 'Three synthetic words')
    with caplog.at_level(logging.INFO):
        evidence.finished(session)
    fields = evidence.get(session).snapshot()
    assert fields['delivery_abort_reason'] == ('cancelled' if cancelled else 'not_requested')
    assert fields['request_words'] == 3 and fields['delivered_words'] == 0
    assert fields['paste_abort_word_delta'] == -3
    assert 'Three synthetic words' not in caplog.text


def test_observation_failure_cannot_turn_success_into_duplicate_paste(owner, monkeypatch):
    session = hold(owner)
    calls = []
    def failed_refresh(*args):
        raise OSError('synthetic observation failure')
    monkeypatch.setattr(recovery, 'update_incident', failed_refresh)
    def operation(text):
        calls.append(len(text))
        evidence.paste_result(text, True, True)
        return True
    assert evidence.paste_call(session, 'fixture', operation) is True
    assert len(calls) == 1
    assert evidence.delivery_context.get() is None
