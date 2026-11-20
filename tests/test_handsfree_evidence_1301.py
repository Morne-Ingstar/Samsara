import json
from datetime import datetime, timezone
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from tools.diag import handsfree_evidence_1301 as evidence


BASE = datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()


def _detector(pcm, rate):
    assert rate == evidence.RATE
    padded = np.pad(pcm, (0, (-len(pcm)) % evidence.VAD_STEP))
    scores = np.max(np.abs(padded.reshape(-1, evidence.VAD_STEP)), axis=1)
    return SimpleNamespace(known=True, probabilities=tuple(scores))


@pytest.fixture
def sources(tmp_path):
    logs, flight, sessions = [tmp_path / name for name in ('logs', 'flight', 'sessions')]
    for path in (logs, flight, sessions):
        path.mkdir()
    return logs, flight, sessions


def _write_journal(directory, frames, extra=(), *, wav_width=2, complete=True):
    stem = directory / ('a' * 32)
    rows = []
    for seq, (at, level) in enumerate(frames):
        rows.append(dict(event='frame', sample_offset=seq * 1600, samples=1600,
                         seq=seq, device_epoch=1, capture_t=at,
                         observed_ns=round((at + .003) * 1e9)))
    rows.extend(extra)
    stem.with_suffix('.jsonl').write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    stem.with_suffix('.json').write_text(json.dumps(dict(schema=1, kind='REAL_SESSION',
        samples=len(frames) * 1600, complete=complete,
        incomplete_reason=None if complete else 'frame_discontinuity')), encoding='utf-8')
    with wave.open(str(stem.with_suffix('.wav')), 'wb') as wav:
        wav.setparams((1, wav_width, evidence.RATE, 0, 'NONE', 'not compressed'))
        audio = np.concatenate([np.full(1600, level, dtype='<i2') for _, level in frames])
        wav.writeframes(audio.tobytes())


def _write_flight(directory, *events):
    rows = [dict(event=event, t_wall=BASE + seconds, t_mono=100 + seconds - .03,
                 thread='PRIVATE_THREAD', **extra) for seconds, event, extra in events]
    (directory / 'events-20261120.jsonl').write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')


def _run(sources, measure=_detector):
    return evidence.analyse(*sources, BASE, BASE + 4, timezone.utc, measure=measure)


def test_aligns_and_measures_saved_pcm_without_copying_words(sources):
    logs, flight, sessions = sources
    (logs / 'samsara.log').write_text('\n'.join([
        '2026-11-20 00:00:01,000 [DUCK] capture duck engaged',
        '2026-11-20 00:00:01,700 [HANDSFREE-SILENCE] vad_used=1 threshold=0.500000 '
        'vad_peak=0.005 rms=0.009 rms_threshold=0.007 silence_s=0.65 since_release_s=-1',
        '2026-11-20 00:00:01,704 [GATE] speech_presence path=handsfree duration_s=0.300 '
        'peak=0.6 total_p05_s=0.192 verdict=pass PRIVATE_LOG',
        '2026-11-20 00:00:02,000 [DICTATE-PREVIEW] parked 13-char draft at session exit',
        '2026-11-20 00:00:02,100 [AVA] PRIVATE_TTS',
    ]), encoding='utf-8')
    _write_flight(flight, (1, 'wake.session_open', {}),
        (1.7, 'wake.session_close', dict(reason='silence_flush', speech_s=.3)),
        (1.675, 'hold_recording.start', dict(key_combo='PRIVATE_KEY')),
        (2.1, 'hold_recording.stop', {}))
    _write_journal(sessions, [(101.0, 24000), (101.6, 24000), (101.69, 300),
                              (101.8, 24000), (101.9, 24000)], [
        dict(event='chunk', samples=4800, sample_offset=4800, speech_start_ns=101000000000,
             speech_end_ns=101700000000, last_voice_ns=101600000000),
        dict(event='draft', observed_ns=101990000000, text='PRIVATE_DRAFT'),
        dict(event='dispatch', delivered='PRIVATE_DELIVERY'),
    ], complete=False)
    result = _run(sources)
    assert 'PRIVATE_' not in json.dumps(result, allow_nan=False)
    row, = result['closes']
    assert row['opened_at'] == BASE + 1
    assert row['silence']['rms_threshold'] == .007
    assert row['gate']['total_p05_s'] == .192
    audio, = row['audio']
    assert audio['queued_s'] == .3
    assert audio['closing_pcm_rms'] == pytest.approx(300 / 32767, abs=1e-6)
    assert audio['closing_offline_peak'] == audio['closing_pcm_rms']
    assert audio['clip_estimate']['timing_gaps'] == 1
    assert audio['ring_near_close']['pcm_s'] == .5
    assert audio['ring_near_close']['total_p05_s'] >= .25
    assert audio['ring_near_close']['run_p05_s'] < .29
    saved, = result['sessions']
    assert saved['gap_s'] == .5
    assert saved['nonconsecutive_frames'] == 0
    assert saved['delivered_events'] == 1
    assert saved['draft_counts'] == [dict(at=BASE + 1.99, chars=13, words=1)]
    assert saved['speech_spans'][0]['hold_s'] > 0
    assert {'duck_engage', 'tts_ava_log', 'hold_recording.start', 'park'} <= {
        row['event'] for row in result['chronology']}


def test_leaves_unmatched_gate_and_missing_pcm_explicit(sources):
    logs, flight, sessions = sources
    (logs / 'samsara.log').write_text('\n'.join([
        '2026-11-20 00:00:01,500 [HANDSFREE-SILENCE] vad_used=0 threshold=0.02 rms=0.019',
        '2026-11-20 00:00:01,700 [GATE] speech_presence path=handsfree duration_s=1.1 '
        'peak=-1 verdict=PRIVATE_UNKNOWN',
    ]), encoding='utf-8')
    _write_flight(flight, (1.5, 'wake.session_close', dict(reason='silence_flush', speech_s=1.4)))
    _write_journal(sessions, [(101., 0)], wav_width=1)
    result = _run(sources)
    assert result['unreadable_sessions'] == 1
    assert result['sessions'] == []
    row, = result['closes']
    assert row['opened_at'] is None
    assert row['gate'] is None
    assert row['audio'] == []
    assert result['chronology'][-1]['verdict'] == 'other'


def test_rejects_live_profile_before_any_read(tmp_path):
    with pytest.raises(ValueError, match='Live profiles'):
        _run((tmp_path / '.samsara', tmp_path, tmp_path))


def test_missing_vad_is_distinct_from_zero_speech():
    weights = evidence._sample_weights(1600)
    times = np.arange(len(weights)) * .032
    assert evidence._window_audio([(None, weights, times)], 0, 1) == {
        'pcm_s': .1, 'vad_pcm_s': 0}
    result = evidence._window_audio([(np.zeros(len(weights)), weights, times)], 0, 1)
    assert result['vad_pcm_s'] == .1
    assert result['total_p05_s'] == 0


def test_wall_anchor_can_join_a_delayed_flight_close():
    close = dict(at=BASE + 1.215, buffer_s=1.4)
    flight = [(101.19, close)]
    chunk = dict(speech_end_ns=101000000000, samples=22400)
    assert evidence._pair_chunk(chunk, flight, [101.19]) is None
    assert evidence._pair_chunk(chunk, flight, [101.19], BASE - 100) is close
    silence = dict(at=BASE + 1.)
    assert evidence._silence_for([silence], close['at'], BASE) is silence
