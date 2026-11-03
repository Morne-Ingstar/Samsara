"""1052c: fixture/score integrity and one short, real-model hold replay."""
import importlib.util
import os
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('hold_bench_1052c', Path(__file__).parents[1] / 'tools' / 'hold_e2e_bench.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


@pytest.fixture
def _speech_presence_unknown_for_decoder_fixtures():
    # Override conftest's synthetic-PCM autouse fixture for this real corpus.
    # This file must exercise the real bundled speech-presence detector.
    yield


def test_alignment_penalizes_insertions_and_preserves_reference_positions():
    result = bench.align('Mr. Quilter has a quiet voice.', 'Mister Quilter a quiet voice voice voice')
    assert result['matched_indices'] == [0, 1, 3, 4, 5]
    assert result['deletions'] == 1
    assert result['insertions'] == 2
    assert result['recall'] == pytest.approx(5/6)
    assert result['wer'] == pytest.approx(3/6)


def test_loop_detection_catches_phrases_without_rejecting_normal_recurrence():
    assert bench.repeated_run('We can go. We can go. We can go. We can go.') == 'we can go'
    assert bench.repeated_run('We can go now, and later we can go home.') is None


def test_fixtures_have_required_acoustic_conditions(tmp_path):
    cases = bench.build_fixtures(tmp_path)
    by_id = {c['id']: c for c in cases}
    assert len(cases) == 8
    assert by_id['1026_peak0995']['peak'] == pytest.approx(.995, abs=1e-5)
    assert by_id['1026_peak0995']['quiet_phrase_rms_dbfs'] == pytest.approx(-26, abs=.1)
    assert by_id['1043_peak0995']['duration_s'] == 75.
    assert by_id['1043_peak0995']['hesitation_s'] == [29.2, 30.0]
    assert by_id['1042_peak0995']['peak'] == pytest.approx(.07, abs=1e-6)
    assert by_id['1051_peak0995']['cue_at_s'] == 1.6
    assert by_id['1051_peak_minus12']['peak'] == pytest.approx(10**(-12/20), abs=1e-5)
    assert sum(p.stat().st_size for p in tmp_path.glob('*.wav')) < 20_000_000


@pytest.mark.slow
def test_short_hold_uses_real_model_and_real_delivery(tmp_path, monkeypatch):
    # The local model is the sole external input. No model download or private
    # profile fallback; plain exports can run with this explicit input too.
    model_path = os.environ.get('SAMSARA_HOLD_BENCH_MODEL')
    if not model_path:
        pytest.skip('Set SAMSARA_HOLD_BENCH_MODEL to a local faster-whisper model directory')
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    from samsara import speech_presence
    assert getattr(speech_presence._presence.measure, '__self__', None) is speech_presence._presence
    model = bench.load_model(Path(model_path), device='cpu', compute_type='int8')
    case = next(c for c in bench.build_fixtures(tmp_path / 'wav') if c['id'] == '1026_peak0995')
    result = bench.run_case(case, model, tmp_path / 'replay', partials=True)
    assert result['native_calls'], 'A real native decode must execute'
    assert any(c['segments'] for c in result['native_calls'])
    assert all(c.get('elapsed_ms', 0) > 0 for c in result['native_calls'])
    assert result['partials'], 'Real partial decode must also execute'
    assert result['metrics']['reference_words'] > 20
    assert result['metrics']['recall'] > .5, result['decode_failure']
    assert result['history'] and all(r['display_text'].strip() for r in result['history'])
    assert result['final_state'] == 'done'
    assert result['checks']['no_capture_loss']
    # Acoustic pass/fail is a bench result, not an assertion that current
    # production already meets every gate (the baseline is allowed to fail).
