"""Whole-take WAV evidence using synthetic PCM, never a live profile/model."""
import json
import threading
from types import SimpleNamespace as NS
import wave

import numpy as np
import pytest

from samsara import hold_recording_recovery as recovery, kept_dictations as kept
from samsara.hold_final_policy import FinalAudit, current_audit, final_decode
from samsara.session_modes import UtteranceSignals
from tests.test_kept_dictations_1161 import manager_for


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    app = NS(config={'diagnostics': {'keep_incident_holds': True},
                     'kept_dictations': {'enabled': False}}, model_rate=16000,
             model_lock=threading.Lock(), device_type='cpu')
    yield app
    for name in ('_incident_store_1052a', '_kept_dictations'):
        store = getattr(app, name, None)
        if store:
            timer = getattr(store, 'expiry', None)
            if timer:
                timer.cancel()
            if store.worker:
                store.worker.shutdown(wait=True)


def read_wav(path):
    with wave.open(str(path), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        return np.frombuffer(wav.readframes(wav.getnframes()), '<i2')


def incident(app):
    store = app._incident_store_1052a
    store.worker.submit(lambda: None).result(timeout=5)
    path, = store.folder.glob('*.json')
    return json.loads(path.read_text()), read_wav(path.with_suffix('.wav'))


def kept_take(app):
    store = kept.store_for(app)
    store.flush()
    row, = store.list()
    return row, read_wav(store.audio_path(row['id']))


@pytest.mark.parametrize('incident_on', [False, True])
def test_real_chunked_final_saves_full_timeline_in_both_stores(owner, monkeypatch, incident_on):
    from samsara import speech_presence
    owner.config['diagnostics']['keep_incident_holds'] = incident_on
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: speech_presence.Presence(False))
    audio = np.full(43 * 16000 + 8000, .025, np.float32)
    audio[18 * 16000:28 * 16000] = 0  # Long pause must stay on the original timeline.
    audio[-16000:] = 0  # Native exact-silence trimming must not trim saved PCM.
    received = []
    def transcribe(pcm, **params):
        received.append(pcm)
        segment = NS(text=f' Synthetic part {len(received)}.', start=.1, end=.5,
                     avg_logprob=-.2, compression_ratio=1., no_speech_prob=.01,
                     temperature=0., words=None)
        return iter([segment]), NS(language='en')
    owner.model = NS(transcribe=transcribe)
    session = NS(app=owner, cancel_event=threading.Event())
    recovery.begin_incident(session)
    recovery.capture_stopped(session, audio)
    worker = NS(_session=session, _decoder_guard=lambda *a, **kw: None)
    _, _, text = final_decode(worker, audio, {})
    assert len(received) == 2 and text == 'Synthetic part 1. Synthetic part 2.'
    recovery.prepare_final(session, text, text, len(audio) / 16000)
    kept.request_keep(owner, voice=True)
    row, pcm = kept_take(owner)
    assert row['capture_sample_count'] == row['sample_count'] == len(audio)
    assert not row['truncated'] and row['truncation_reason'] is None
    np.testing.assert_array_equal(pcm, kept._to_int16_pcm(audio))
    expected = [(part.ctypes.data - audio.ctypes.data) // audio.itemsize for part in received]
    boundaries = row['chunk_boundaries']
    assert [b['start_sample'] for b in boundaries] == expected
    assert [b['end_sample'] - b['start_sample'] for b in boundaries] == [len(p) for p in received]
    assert all(b['source'] == 'capture_samples' for b in boundaries)
    assert sum(len(part) for part in received) != len(audio)
    assert not hasattr(session._hold_final_audit, 'incident_audio')
    if incident_on:
        meta, pcm = incident(owner)
        np.testing.assert_array_equal(pcm, np.rint(audio * 32768).astype('<i2'))
        assert meta['sample_count'] == len(audio)
        assert meta['chunk_boundaries'] == boundaries
        assert meta['wav_source'] == 'complete_decode_take'
    else:
        assert not hasattr(owner, '_incident_store_1052a')
        assert not hasattr(session._hold_final_audit, 'incident_attempts')


def test_overlapping_native_chunks_and_retries_are_not_concatenated(owner):
    audio = np.linspace(-.1, .1, 64000, dtype=np.float32)
    owner.model = NS(transcribe=lambda *a, **k: (iter([]), None))
    session = NS(app=owner, _decode_audio=audio, _hold_final_audit=FinalAudit())
    recovery.begin_incident(session)
    token = current_audit.set(session._hold_final_audit)
    try:
        model = recovery.incident_model(owner)
        for part in (audio[:40000], audio[:40000], audio[32000:]):
            list(model.transcribe(part)[0])
    finally:
        current_audit.reset(token)
    recovery.prepare_final(session, 'Synthetic final.', 'Synthetic final.', 4)
    kept.request_keep(owner, voice=True)
    for row, pcm in (incident(owner), kept_take(owner)):
        assert row['sample_count'] == 64000
        assert [(b['start_sample'], b['end_sample']) for b in row['chunk_boundaries']] == [(0, 40000), (32000, 64000)]
        # The overlap is saved exactly once, while boundary metadata keeps it.
        assert len(pcm) == len(audio)


@pytest.mark.parametrize('kind', ['incident', 'keep'])
@pytest.mark.parametrize('extra', [0, 1, 16000])
def test_duration_cap_is_exact_and_explicit(owner, kind, extra):
    cap = round(recovery.MAX_SAVED_TAKE_S * owner.model_rate)
    audio = np.full(cap + extra, .02, np.float32)
    session = NS(app=owner, _decode_audio=audio, _hold_final_audit=FinalAudit())
    recovery.begin_incident(session)
    if kind == 'incident':
        recovery.complete_incident(session, 'Synthetic final.')
        row, pcm = incident(owner)
    else:
        kept.capture_held(session, 'Synthetic final.', 'Synthetic raw.')
        kept.request_keep(owner, voice=True)
        row, pcm = kept_take(owner)
    assert len(pcm) == row['sample_count'] == cap
    assert row['capture_sample_count'] == cap + extra
    assert row['duration_s'] == row['max_duration_s'] == 120
    assert row['capture_duration_s'] == (cap + extra) / 16000
    assert row['truncated'] is bool(extra)
    assert row['truncation_reason'] == ('duration_cap' if extra else None)


@pytest.mark.parametrize('fallback', [False, True])
def test_stopped_or_failed_take_uses_full_pcm_without_native_decode(owner, fallback):
    audio = np.full(32000, .03, np.float32)
    session = NS(app=owner, _hold_final_audit=FinalAudit(), cancel_event=threading.Event())
    recovery.begin_incident(session)
    recovery.capture_stopped(session, audio)
    if not fallback:
        session._decode_audio = audio
    recovery.prepare_final(session, '', '', 2)
    kept.request_keep(owner, voice=True)
    for row, pcm in (incident(owner), kept_take(owner)):
        assert row['sample_count'] == len(pcm) == len(audio)
        assert row['chunk_boundaries'] == []  # No guessed decoder boundaries.


def test_staged_handsfree_replaces_first_wav_with_all_parts(owner):
    owner.config['kept_dictations']['enabled'] = True
    app, manager = manager_for(owner)
    owner._kept_dictations = kept.store_for(app)
    parts = [np.full(1600, .02, np.float32), np.full(3200, -.03, np.float32)]
    take_id = None
    for index, audio in enumerate(parts):
        result = manager.dispatch_utterance(f'Synthetic sentence {index}.', UtteranceSignals(True, audio_ref=audio))
        assert result.kind == 'dictate_staged'
        row, pcm = kept_take(app)  # First WAV exists before the second stage arrives.
        take_id = take_id or row['id']
        assert row['id'] == take_id
        np.testing.assert_array_equal(pcm, kept._to_int16_pcm(np.concatenate(parts[:index + 1])))
    assert [(b['start_sample'], b['end_sample']) for b in row['chunk_boundaries']] == [(0, 1600), (1600, 4800)]
    result = manager.dispatch_utterance('send', UtteranceSignals(True, (1.,)))
    assert result.kind == 'dictate_committed'
    row, pcm = kept_take(app)
    assert row['id'] == take_id and row['sample_count'] == len(pcm) == 4800
    assert len(kept.store_for(app).list()) == 1


def test_wake_parts_have_boundaries_and_preserve_internal_silence(owner):
    owner.config['kept_dictations']['enabled'] = True
    owner.wake_dictation_buffer = []
    parts = [np.r_[np.full(800, .02, np.float32), np.zeros(800, np.float32)],
             np.full(800, -.03, np.float32)]
    for index, audio in enumerate(parts):
        token = kept.begin_wake(owner, [audio], 16000)
        if index == 0:
            owner.wake_dictation_buffer.append('Synthetic pending.')
        else:
            kept.wake_history(owner, 'Synthetic raw.', 'Synthetic final.', 'wake', 'dictation')
            owner.wake_dictation_buffer.clear()
        kept.end_wake(owner, token)
    row, pcm = kept_take(owner)
    assert [(b['start_sample'], b['end_sample']) for b in row['chunk_boundaries']] == [(0, 1600), (1600, 2400)]
    np.testing.assert_array_equal(pcm, kept._to_int16_pcm(np.concatenate(parts)))


def test_copied_native_input_is_explicitly_unmapped(owner):
    audio = np.zeros(1600, np.float32)
    session = NS(_hold_final_audit=NS(take_inputs=[audio.copy()]))
    boundary, = recovery.take_boundaries(session, audio)
    assert boundary == dict(start_sample=None, end_sample=None, sample_count=1600,
                            source='unmapped_native_input')
