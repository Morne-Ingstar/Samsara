"""1100: real release/decoder coverage and text-free cutoff evidence."""
import importlib.util
import logging
import os
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara.audio_engine import dictation_consumer as capture
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.audio_engine.ring import FrameBus


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bench_1100', ROOT / 'tools' / 'hold_e2e_bench.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


@pytest.fixture
def _speech_presence_unknown_for_decoder_fixtures():
    # These public fixtures exercise the real detector, not the synthetic
    # PCM substitution in conftest.
    yield


def frame(level):
    return np.full(FRAME_SIZE, level, dtype=np.float32)


@pytest.fixture
def consumer():
    bus = FrameBus()
    engine = NS(register_consumer=bus.new_reader, _overflow_count=0)
    return capture.DictationSessionConsumer(engine, NS(config={})), bus


def fake_clock(monkeypatch, consumer, levels):
    clock = NS(now=100.)
    levels = iter(levels)

    def sleep(_seconds):
        clock.now += .1
        try:
            level = next(levels)
        except StopIteration:
            return
        consumer._streaming_frames.append(frame(level))

    monkeypatch.setattr(capture, 'time', NS(monotonic=lambda: clock.now,
                                          perf_counter=lambda: clock.now, sleep=sleep))
    return clock


def tail_record(caplog):
    records = [r for r in caplog.records if r.getMessage().startswith('[TAIL_CAPTURE]')]
    assert len(records) == 1
    return dict(item.split('=', 1) for item in records[0].getMessage().split()[1:])


def test_final_pcm_and_soft_energy_are_audited_without_changing_capture(consumer, monkeypatch, caplog):
    c, _ = consumer
    c._streaming_frames = [frame(.05) for _ in range(3)]
    fake_clock(monkeypatch, c, [.002, 0., 0.])
    caplog.set_level(logging.DEBUG, logger=capture.logger.name)
    result = c.wait_for_streaming_release_tail()
    c.finish_capture(force=True)
    expected = np.concatenate(c._streaming_frames).copy()
    np.testing.assert_array_equal(c.stop_streaming(), expected)
    assert result['reason'] == 'silence' and result['frames'] == 3
    fields = tail_record(caplog)
    assert fields['release_audio_ms'] == '300'
    assert fields['captured_audio_ms'] == '600'
    assert fields['admitted_tail_ms'] == '300'
    assert fields['last_active_end_ms'] == '300'
    assert fields['last_weak_end_ms'] == '400'
    assert fields['active_end_gap_ms'] == '300'
    assert fields['weak_end_gap_ms'] == '200'
    assert fields['cutoff_set'] == '1'
    assert float(fields['weak_floor']) < .002 < float(fields['threshold'])
    assert c.stop_streaming() is None
    tail_record(caplog)  # No duplicate receipt after the generation was drained.


def test_final_backlog_obeys_cutoff_and_is_in_the_receipt(consumer, caplog):
    c, bus = consumer
    from samsara.audio_engine import hold_capture
    hold_capture.begin(c)
    c._streaming_capturing = True
    c._streaming_frames = [frame(.05)]
    # Abort the wait without discarding already captured or queued PCM.
    result = c.wait_for_streaming_release_tail(should_abort=lambda: True)
    assert result['reason'] == 'abort'
    c._capture_not_after = 10.
    bus.write(np.full(FRAME_SIZE, 300, np.int16), 9.9, 0)
    bus.write(np.full(FRAME_SIZE, 900, np.int16), 10.1, 0)
    caplog.set_level(logging.DEBUG, logger=capture.logger.name)
    audio = c.stop_streaming()
    assert len(audio) == 2*FRAME_SIZE
    np.testing.assert_array_equal(audio[FRAME_SIZE:], np.full(FRAME_SIZE, 300/32767., np.float32))
    fields = tail_record(caplog)
    assert fields['observed_tail_frames'] == '0'
    assert fields['admitted_tail_ms'] == '100'
    assert fields['last_active_end_ms'] == '200'
    assert fields['cutoff_set'] == '1'
    assert not c.capture_loss


def test_hard_cap_still_retains_all_admitted_pcm(consumer, monkeypatch, caplog):
    c, _ = consumer
    c._streaming_frames = [frame(.05) for _ in range(3)]
    fake_clock(monkeypatch, c, [.03]*10)
    caplog.set_level(logging.DEBUG, logger=capture.logger.name)
    result = c.wait_for_streaming_release_tail(max_tail_ms=350)
    assert result['reason'] == 'max'
    expected = np.concatenate(c._streaming_frames).copy()
    np.testing.assert_array_equal(c.stop_streaming(), expected)
    fields = tail_record(caplog)
    assert fields['reason'] == 'max'
    assert fields['active_end_gap_ms'] == '0'
    assert float(fields['last_frame_rms']) == pytest.approx(.03)


def test_empty_aborted_capture_has_numeric_receipt(consumer, caplog):
    c, _ = consumer
    c.wait_for_streaming_release_tail(should_abort=lambda: True)
    caplog.set_level(logging.DEBUG, logger=capture.logger.name)
    assert c.stop_streaming() is None
    fields = tail_record(caplog)
    assert fields['reason'] == 'abort'
    assert fields['captured_audio_ms'] == '0'
    assert fields['last_frame_peak'] == '0.000000'
    # Every field except the enumerated stop reason is numeric; no PCM/text.
    for key, value in fields.items():
        if key not in ('reason', 'hold_seq_id'):  # 1121: shared identity is the additional nonnumeric field.
            assert np.isfinite(float(value))


def test_new_streaming_hold_resets_prior_tail_receipt(consumer, monkeypatch, caplog):
    c, _ = consumer
    c.wait_for_streaming_release_tail(should_abort=lambda: True)
    monkeypatch.setattr(capture.thread_registry, 'spawn', lambda *a, **kw: None)
    assert c.activate_streaming()
    assert c._streaming_tail_audit is None
    caplog.set_level(logging.DEBUG, logger=capture.logger.name)
    assert c.stop_streaming() is None
    assert not any(r.getMessage().startswith('[TAIL_CAPTURE]') for r in caplog.records)


def test_last_word_fixtures_have_real_release_positions_and_levels(tmp_path):
    cases = bench.build_last_word_fixtures(tmp_path)
    assert len(cases) == 4
    by_id = {c['id']: c for c in cases}
    soft = by_id['1100_soft_peak0995']
    mid = by_id['1100_midword_peak0995']
    assert 0 < soft['release_s'] - soft['last_word_span_s'][1] < .1
    assert mid['last_word_span_s'][0] < mid['release_s'] < mid['last_word_span_s'][1]
    assert soft['last_word_rms_dbfs'] == pytest.approx(-32.42, abs=.1)
    assert by_id['1100_soft_peak_minus12']['last_word_rms_dbfs'] < -44
    assert bench.words(soft['reference'])[-1] == soft['last_word']
    assert soft['sha256'] != mid['sha256']


@pytest.fixture(scope='module')
def native_model():
    model_path = os.environ.get('SAMSARA_HOLD_BENCH_MODEL')
    if not model_path:
        pytest.skip('Set SAMSARA_HOLD_BENCH_MODEL to explicit local offline weights')
    return bench.load_model(Path(model_path))


@pytest.mark.slow
@pytest.mark.parametrize('case_id', ['1100_soft_peak0995', '1100_soft_peak_minus12',
                                    '1100_midword_peak0995', '1100_midword_peak_minus12'])
def test_native_last_word_survives_release_and_partials(case_id, tmp_path, native_model, caplog):
    from samsara import speech_presence
    assert getattr(speech_presence._presence.measure, '__self__', None) is speech_presence._presence
    caplog.set_level(logging.DEBUG, logger=capture.logger.name)
    case = next(c for c in bench.build_last_word_fixtures(tmp_path / 'wav') if c['id'] == case_id)
    result = bench.run_case(case, native_model, tmp_path / 'replay', partials=True)
    assert result['passed'], result['checks']
    assert result['metrics']['recall'] == 1.
    assert result['metrics']['wer'] == 0.
    assert result['partials']
    assert result['native_calls'] and any(c['segments'] for c in result['native_calls'])
    assert result['release_tail']['reason'] == 'silence'
    fields = tail_record(caplog)
    assert int(fields['captured_audio_ms']) >= case['last_word_span_s'][1]*1000
    assert fields['cutoff_set'] == '1'
    (tmp_path / 'tail_capture.log').write_text('\n'.join(
        r.getMessage() for r in caplog.records if r.getMessage().startswith('[TAIL_CAPTURE]'))+'\n')
