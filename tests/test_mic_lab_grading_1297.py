"""1297: metrics from the first real run; no owner PCM or transcript fixture."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
import ctypes as ct

import numpy as np
import pytest

from samsara import mic_lab_battery as battery
from samsara import mic_lab_dsp as dsp
from samsara import mic_lab_endpoint as endpoint


# Copied measurement values from 2026-11-20, 13 takes (107 scripted words).
SAVED_METRICS_20261120 = [{'step_id': 'baseline',
  'number': 1,
  'live': {'errors': 0, 'words': 9, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'speech': {'value': -27.372865781362698, 'verdict': 'Good', 'details': {}},
              'snr': {'value': None, 'verdict': 'Watch', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'baseline',
  'number': 2,
  'live': {'errors': 0, 'words': 9, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'speech': {'value': -26.674033019385174, 'verdict': 'Good', 'details': {}},
              'snr': {'value': None, 'verdict': 'Watch', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'cold_onset',
  'number': 1,
  'live': {'errors': 0, 'words': 8, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'first_word': {'value': True, 'verdict': 'Good', 'details': {}},
              'onset': {'value': 0.42, 'verdict': 'Bad', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'cold_onset',
  'number': 2,
  'live': {'errors': 0, 'words': 8, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'first_word': {'value': True, 'verdict': 'Good', 'details': {}},
              'onset': {'value': 0.63, 'verdict': 'Bad', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'dyn_contrast',
  'number': 1,
  'live': {'errors': 3, 'words': 10, 'wer': 0.3, 'first_word': True, 'last_word': True},
  'metrics': {'agc': {'value': None, 'verdict': 'Watch', 'details': {}},
              'clipping': {'value': {'full_scale_samples': 0,
                                     'flat_top_runs': 0,
                                     'peak_dbfs': -0.01937238409402277,
                                     'runs': []},
                           'verdict': 'Watch',
                           'details': {}},
              'drift': {'value': 0.0008993628279441168, 'verdict': 'Good', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'fricatives_hf',
  'number': 1,
  'live': {'errors': 0, 'words': 9, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'bandwidth': {'value': 20.83219963539554, 'verdict': 'Good', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'fricatives_hf',
  'number': 2,
  'live': {'errors': 0, 'words': 9, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'bandwidth': {'value': 19.31674036928117, 'verdict': 'Good', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'native_probe',
  'number': 1,
  'live': None,
  'metrics': {'noise': {'value': -300.0, 'verdict': 'Watch', 'details': {'gated': [True]}},
              'rumble_dc': {'value': {'rumble_db': 11.997142999208421,
                                      'dc_dbfs': -115.18137796963512},
                            'verdict': 'Watch',
                            'details': {}}}},
 {'step_id': 'off_axis_2x',
  'number': 1,
  'live': {'errors': 1, 'words': 8, 'wer': 0.125, 'first_word': True, 'last_word': True},
  'metrics': {'snr': {'value': None, 'verdict': 'Watch', 'details': {}},
              'bandwidth': {'value': 14.312489824031376, 'verdict': 'Good', 'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'plosives_norm',
  'number': 1,
  'live': {'errors': 1, 'words': 10, 'wer': 0.1, 'first_word': True, 'last_word': True},
  'metrics': {'clipping': {'value': {'full_scale_samples': 0,
                                     'flat_top_runs': 0,
                                     'peak_dbfs': -0.4022879849302283,
                                     'runs': []},
                           'verdict': 'Watch',
                           'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'plosives_norm',
  'number': 2,
  'live': {'errors': 0, 'words': 10, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'clipping': {'value': {'full_scale_samples': 0,
                                     'flat_top_runs': 0,
                                     'peak_dbfs': -4.555385832775306,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'proximity_pop',
  'number': 1,
  'live': {'errors': 0, 'words': 8, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'clipping': {'value': {'full_scale_samples': 0,
                                     'flat_top_runs': 0,
                                     'peak_dbfs': -5.48378290983106,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}},
 {'step_id': 'quiet_edges',
  'number': 1,
  'live': {'errors': 0, 'words': 9, 'wer': 0.0, 'first_word': True, 'last_word': True},
  'metrics': {'edges': {'value': True, 'verdict': 'Good', 'details': {}},
              'boundary': {'value': {'before': False, 'after': False},
                           'verdict': 'Good',
                           'details': {}},
              'dropouts': {'value': {'zero_runs': 0,
                                     'longest_s': 0.0,
                                     'input_overflows': 0,
                                     'runs': []},
                           'verdict': 'Good',
                           'details': {}},
              'gain': {'value': None, 'verdict': 'Watch', 'details': {}}}}]


def saved_results():
    results = []
    for step in battery.BATTERY:
        takes = []
        for row in SAVED_METRICS_20261120:
            if row['step_id'] != step.id:
                continue
            live = deepcopy(row['live'])
            if live:
                # The only repeat in this saved run was the scripted quiet cue.
                live.update(raw_errors=live['errors'], speaker_slips=0, speaker_slip_words=0)
                if step.id == 'dyn_contrast':
                    live.update(errors=0, wer=0., speaker_slips=1, speaker_slip_words=3)
            metrics = {k: dsp.Measurement(cause='Saved measurement', **deepcopy(v)) for k, v in row['metrics'].items()}
            if live:
                metrics['wer'] = dsp.Measurement(live, 'Good', '')
            judgment = battery.grade_measurements(metrics, live=live)
            judgment['cause'] = judgment['reasons'][0]
            takes.append(dict(number=row['number'], judgment=judgment))
        if takes:
            results.append(dict(step_id=step.id, certification=step.certification,
                                takes=takes, **battery.step_verdict(takes)))
    return results


def test_first_real_run_is_watch_for_hot_input_not_unknowns():
    report = battery.report_results(saved_results())
    assert report['verdict'] == 'Watch' and report['complete']
    assert 'input level hot' in report['findings'][0]
    assert '-0.02 dBFS' in report['findings'][0]
    assert report['all_scripted_accuracy'] == dict(words=107, errors=2, wer=2/107,
        raw_errors=5, speaker_slip_words=3, verdict='Good')
    assert report['accuracy']['words'] == 91 and report['accuracy']['errors'] == 1
    assert any('gain: Unknown' in v for v in report['unknown'])
    assert any('onset 0.63 s' in v for v in report['observations'])


@pytest.mark.parametrize('text,errors,slips', [
    ('Normal level now. LOUD PEAK ONE TWO! quiet voice returns. quiet voice returns.', 0, 3),
    ('Normal level now. LOUD PEAK ONE TWO! quiet voice returns.', 0, 0),
    ('Normal level now. LOUD PEAK ONE TWO! unexpected words here. quiet voice returns.', 3, 0),
])
def test_repeat_detection_is_limited_to_repeated_scripted_phrases(text, errors, slips):
    score = battery.word_score(battery.STEPS['dyn_contrast'].target, text)
    assert score['errors'] == errors and score['speaker_slip_words'] == slips


def test_intended_target_repeats_are_preserved():
    assert battery.word_score('quiet voice quiet voice', 'quiet voice quiet voice')['speaker_slip_words'] == 0
    assert battery.word_score('quiet voice returns', 'quiet quiet voice returns')['errors'] == 1


def audio_take(step, *, text=None):
    raw = np.random.default_rng(1297).normal(0, .03, 32000).astype(np.float32)
    return dict(raw=raw, sample_rate=16000, text=step.target if text is None else text,
                offline=dict(status='ok', text=step.target), slices=[], gain_before=None,
                gain_after=None, trigger_sample=0)


def test_one_short_sentence_error_is_observed_without_downgrade():
    step = replace(battery.STEPS['off_axis_2x'], judges=('wer',))
    data = audio_take(step, text=step.target.replace('frequencies', 'frequency'))
    judgment = battery.judge_take(step, data, battery.JudgeContext())
    assert judgment['verdict'] == 'Good' and judgment['live']['errors'] == 1
    assert any('1/8' in v for v in judgment['observations'])


def test_two_non_edge_errors_flag_step_watch():
    step = replace(battery.STEPS['baseline'], judges=('wer',))
    words = step.target.split()
    words[1:3] = ['unexpected', 'replacement']
    judgment = battery.judge_take(step, audio_take(step, text=' '.join(words)), battery.JudgeContext())
    assert judgment['verdict'] == 'Watch'


def test_missing_gain_snr_and_agc_are_unknown_not_watch():
    step = replace(battery.STEPS['dyn_contrast'], judges=('gain', 'snr', 'agc', 'wer'))
    data = audio_take(step)
    result = battery.judge_take(step, data, battery.JudgeContext(np.zeros(32000, np.float32)))
    assert result['verdict'] == 'Good'
    for key in ('gain', 'snr', 'agc'):
        assert result['metrics'][key]['verdict'] == 'Unknown'
    assert 'no defensible noise-only floor' in result['metrics']['snr']['cause']


def test_unknown_only_take_does_not_claim_good():
    step = replace(battery.STEPS['baseline'], judges=('gain',))
    result = battery.judge_take(step, audio_take(step), battery.JudgeContext())
    assert result['verdict'] == 'Unknown'


def test_slow_human_reaction_does_not_fail_cold_start():
    step = replace(battery.STEPS['cold_onset'], judges=('first_word', 'onset', 'wer'))
    data = audio_take(step)
    data['raw'][:16000] = 0
    silence = np.random.default_rng(0).normal(0, .0001, 32000).astype(np.float32)
    result = battery.judge_take(step, data, battery.JudgeContext(silence))
    assert result['verdict'] == 'Good'
    assert result['metrics']['onset']['value'] >= .9
    assert any('reaction time' in s or 'observation only' in s for s in result['observations'])


@pytest.mark.parametrize('fault', ['narrowband', 'dropout', 'first_word', 'flat_top'])
def test_synthetic_core_faults_still_make_run_bad_even_if_votes_hide_them(fault):
    results = saved_results()
    step_id = 'fricatives_hf' if fault == 'narrowband' else 'cold_onset' if fault == 'first_word' else 'plosives_norm'
    step = battery.STEPS[step_id]
    data = audio_take(step)
    if fault == 'narrowband':
        from scipy.signal import butter, sosfiltfilt
        data['raw'] = sosfiltfilt(butter(8, 3000, fs=16000, output='sos'), data['raw']).astype(np.float32)
        step = replace(step, judges=('bandwidth',))
    elif fault == 'dropout':
        data['raw'][16000:16320] = 0
        step = replace(step, judges=('dropouts',))
    elif fault == 'first_word':
        data['text'] = ' '.join(step.target.split()[1:])
        step = replace(step, judges=('first_word', 'wer'))
    else:
        data['raw'] = np.clip(data['raw'] * 10, -.2, .2)
        step = replace(step, judges=('clipping',))
    judgment = battery.judge_take(step, data, battery.JudgeContext())
    assert judgment['verdict'] == 'Bad'
    result = next(r for r in results if r['step_id'] == step_id)
    result['takes'][0]['judgment'] = judgment
    result['verdict'] = 'Good'  # Prove the overall rule inspects concrete faults.
    assert battery.report_results(results)['verdict'] == 'Bad'


@pytest.mark.parametrize('errors,words,expected', [(5,100,'Good'), (6,100,'Watch'), (10,100,'Bad')])
def test_run_accuracy_thresholds(errors, words, expected):
    results = [dict(step_id=s.id, certification=s.certification, verdict='Good', takes=[]) for s in battery.BATTERY]
    results[1]['takes'] = [dict(judgment=dict(live=dict(errors=errors, words=words,
        first_word=True, last_word=True), metrics={}))]
    report = battery.report_results(results)
    assert report['verdict'] == expected


def test_curveballs_cannot_decide_overall_even_with_bad_audio_and_words():
    results = saved_results()
    for r in results:
        if r['step_id'] in battery.CURVEBALLS:
            for t in r['takes']:
                t['judgment']['live'].update(errors=8, first_word=False)
                t['judgment']['metrics']['dropouts']['verdict'] = 'Bad'
    assert battery.report_results(results)['verdict'] == 'Watch'


def test_rejudging_existing_two_take_step_needs_no_new_tie_breaker(monkeypatch):
    step = battery.STEPS['plosives_norm']
    takes = [dict(audio_take(step), step_id=step.id, number=n) for n in (1, 2)]
    def judge(step, take, context):
        return dict(verdict='Watch' if take['number'] == 1 else 'Good', cause='measured', metrics={})
    monkeypatch.setattr(battery, 'judge_take', judge)
    report = battery.rejudge_run(dict(takes=takes))
    assert not report['steps'][0].get('incomplete')
    assert not report['complete']  # All the other steps are still absent.


@pytest.mark.parametrize('hr,uninitializes', [(0,1), (1,1), (-2147417850,0)])
def test_com_existing_sta_can_read_exact_endpoint_without_changing_apartment(monkeypatch, hr, uninitializes):
    # Entire COM boundary is fake: no hardware enumeration, no input stream.
    calls = []
    class Function:
        def __init__(self, fn):
            self.fn = fn
        def __call__(self, *args):
            return self.fn(*args)
    def create(clsid, unused, ctx, iid, out):
        out._obj.value = 10
        return 0
    ole = NS(CoInitializeEx=Function(lambda *a: hr), CoCreateInstance=Function(create),
        CoTaskMemFree=Function(lambda *a: None), PropVariantClear=Function(lambda *a: 0),
        CoUninitialize=Function(lambda: calls.append('uninitialize')))
    monkeypatch.setattr(endpoint.sys, 'platform', 'win32')
    monkeypatch.setattr(ct, 'WinDLL', lambda name: ole, raising=False)
    monkeypatch.setattr(ct, 'OleDLL', lambda name: pytest.fail('OleDLL auto-raises changed mode'), raising=False)
    released = []
    def call(pointer, slot, args=(), types=()):
        if pointer.value == 10 and slot == 5:
            assert args[0] == 'fixture-id'
            args[1]._obj.value = 20
        elif pointer.value == 20 and slot == 0:
            args[1]._obj.value = 30
        elif pointer.value == 30 and slot == 3:
            args[0]._obj.value = 1
        else:
            pytest.fail(f'unexpected COM call {pointer.value}/{slot}')
    monkeypatch.setattr(endpoint, '_call', call)
    monkeypatch.setattr(endpoint, '_release', lambda p: released.append(p.value))
    monkeypatch.setattr(endpoint._CoreAudio, '_id', staticmethod(lambda *a: 'fixture-id'))
    monkeypatch.setattr(endpoint._CoreAudio, '_property', staticmethod(lambda *a: 'Fixture USB microphone'))
    monkeypatch.setattr(endpoint._CoreAudio, '_format', lambda *a: dict(sample_rate=48000,channels=1))
    monkeypatch.setattr(endpoint._CoreAudio, '_volume', lambda *a: .5)
    assert endpoint.endpoint_volume(endpoint_id='fixture-id').value == .5
    assert calls.count('uninitialize') == uninitializes
    assert set(released) == {10,20,30}


def test_cli_summary_and_step_print_measured_reasons_and_unknowns():
    from tools import mic_lab_cli as cli
    report = battery.report_results(saved_results())
    lines = []
    for r in report['steps']:
        cli.print_step(r['step_id'], r, lines.append)
    cli.print_summary(report, lines.append)
    text = '\n'.join(lines)
    assert 'Overall: Watch.' in text and 'input level hot' in text
    assert 'Could not measure (excluded from grade):' in text
    assert 'undetermined' not in text and 'mic_or_environment' not in text
    assert 'speaker slip: 3' in text and 'onset 0.42 s' in text


def test_unknown_votes_do_not_outvote_a_measured_fault():
    takes = [dict(judgment=dict(verdict=v, cause='fixture')) for v in ('Unknown', 'Unknown', 'Bad')]
    assert battery.step_verdict(takes)['verdict'] == 'Bad'
    assert battery.step_verdict(takes[:2])['verdict'] == 'Unknown'
    assert battery._baseline_rank(takes[0]) > battery._baseline_rank(takes[-1])


def test_hot_input_is_one_summary_finding_with_worst_peak():
    report = battery.report_results(saved_results())
    assert len([s for s in report['findings'] if 'input level hot' in s]) == 1
    assert '-0.02 dBFS' in report['findings'][0]
    assert 'word accuracy' in report['findings'][1]


@pytest.mark.parametrize('json_only', [False, True])
def test_saved_cli_human_default_and_explicit_json(monkeypatch, json_only):
    import json
    from tools import mic_lab_cli as cli
    report = battery.report_results(saved_results())
    monkeypatch.setattr(cli, 'load_run', lambda *a: {})
    monkeypatch.setattr(cli, 'rejudge_run', lambda *a: report)
    lines = []
    args = ['--from-folder', 'fixture'] + (['--json'] if json_only else [])
    assert cli.main(args, output=lines.append) == 0
    if json_only:
        assert len(lines) == 1 and json.loads(lines[0]) == report
    else:
        assert 'Overall: Watch.' in lines
        assert any('input level hot' in s for s in lines)
        assert not lines[0].startswith('{')
