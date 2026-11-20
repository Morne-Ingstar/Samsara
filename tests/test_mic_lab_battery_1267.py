from dataclasses import replace
import json
from pathlib import Path
import threading

import numpy as np
import pytest

from samsara import mic_lab_battery as battery
from samsara.mic_lab_store import MicLabStore, load_run, storage_root


def take(text='testing', *, slices=True):
    raw = np.random.default_rng(2).normal(0, .025, 32000).astype(np.float32)
    return dict(raw=raw, sample_rate=16000, text=text, gain_before=.5, gain_after=.5,
        raw_coordinates=dict(capture_start=0, capture_end=len(raw)), capture_receipt=None,
        slices=[dict(audio=raw[200:30000].copy(), capture_start=200, capture_end=30000,
                     params=dict(temperature=(0., .2), vad_filter=False))] if slices else [])


def test_declarative_protocol_matches_revised_battery():
    assert len(battery.BATTERY) == 11
    assert [s.id for s in battery.BATTERY if s.takes == 2] == ['baseline', 'cold_onset', 'fricatives_hf', 'plosives_norm']
    assert all(s.certification for s in battery.BATTERY[:9])
    assert not any(s.certification for s in battery.BATTERY[9:])
    assert battery.STEPS['dyn_contrast'].seconds == 15.


@pytest.mark.parametrize('votes, expected, count', [
    (['Good', 'Good'], 'Good', 2), (['Bad', 'Bad'], 'Bad', 2),
    (['Good', 'Bad', 'Good'], 'Good', 3), (['Bad', 'Good', 'Bad'], 'Bad', 3),
    (['Good', 'Bad', 'Watch'], 'Watch', 3)])
def test_runner_requests_only_needed_takes_and_votes(monkeypatch, votes, expected, count):
    observed, states = [], []
    runner = battery.BatteryRunner()
    runner.steps = (battery.STEPS['baseline'],)
    def capture(step, number):
        observed.append(number)
        states.append(runner.state)
        return take(step.target)
    def judge(step, data, context):
        return dict(verdict=votes[data['number'] - 1], cause='none', metrics={})
    monkeypatch.setattr(battery, 'judge_take', judge)
    saved = []
    runner.run(capture, lambda audio, params: 'reference', save=lambda *a: saved.append((a[1], runner.state)))
    assert observed == list(range(1, count + 1)) and states == ['capture'] * count
    assert saved == [(i, 'save') for i in range(1, count + 1)]
    assert runner.results[0]['verdict'] == expected
    assert runner.state == 'done'
    with pytest.raises(RuntimeError):
        runner.run(capture, lambda *a: '')


def test_single_take_step_never_requests_tie_breaker(monkeypatch):
    runner = battery.BatteryRunner()
    runner.steps = (battery.STEPS['quiet_edges'],)
    monkeypatch.setattr(battery, 'judge_take', lambda *a: dict(verdict='Bad', cause='pipeline'))
    calls = []
    runner.run(lambda s, n: (calls.append(n) or take()), lambda *a: '')
    assert calls == [1]


def test_failed_capture_is_not_reported_as_pass():
    runner = battery.BatteryRunner()
    with pytest.raises(OSError):
        runner.run(lambda *a: (_ for _ in ()).throw(OSError('device gone')), lambda *a: '')
    assert runner.state == 'failed' and not runner.results


def test_reference_decodes_copies_at_zero_no_vad_and_deduplicates_attempts():
    data = take()
    data['slices'].append(dict(data['slices'][0], audio=data['slices'][0]['audio'].copy()))
    expected = data['slices'][0]['audio'].copy()
    calls = []
    def decode(audio, params):
        assert params['temperature'] == 0. and params['vad_filter'] is False
        assert np.array_equal(audio, expected)
        audio[:] = 0
        calls.append(params)
        return f'attempt {len(calls)}'
    reference = battery.offline_reference(data, decode)
    assert reference['text'] == 'attempt 2' and len(reference['slices']) == 2
    assert np.array_equal(data['slices'][0]['audio'], expected)


def test_overlapping_different_cuts_do_not_claim_clean_reference():
    data = take()
    data['slices'].append(dict(data['slices'][0], capture_start=10000, capture_end=31000))
    assert battery.offline_reference(data, lambda *a: 'testing')['status'] == 'ambiguous_overlap'


def test_clean_offline_reference_attributes_bad_live_text_to_pipeline():
    step = replace(battery.STEPS['cold_onset'], judges=('first_word', 'wer'))
    data = take('right there and check the first word')
    data['offline'] = dict(status='ok', text=step.target)
    result = battery.judge_take(step, data, battery.JudgeContext())
    assert result['cause'] == 'pipeline' and result['live']['first_word'] is False
    assert result['offline']['wer'] == 0.
    data['offline']['text'] = data['text']
    # 1297: ambiguous attribution now carries the measured reason in plain words.
    assert 'missing' in battery.judge_take(step, data, battery.JudgeContext())['cause']


def test_boundaries_require_missing_word_and_acoustic_evidence():
    step = replace(battery.STEPS['quiet_edges'], judges=('boundary',))
    data = take('maybe we left the soft note past eight')
    context = battery.JudgeContext(np.random.default_rng(3).normal(0, .0001, 32000).astype(np.float32))
    result = battery.judge_take(step, data, context)
    assert result['metrics']['boundary']['verdict'] == 'Bad' and result['cause'] == 'pipeline'
    data['text'] = step.target
    assert battery.judge_take(step, data, context)['metrics']['boundary']['verdict'] == 'Good'


def test_gain_hijack_and_ring_loss_are_failures():
    data = take()
    data.update(gain_after=.53, capture_receipt={'losses': [dict(reason='sequence_gap')]})
    step = replace(battery.STEPS['baseline'], judges=('gain', 'dropouts'))
    result = battery.judge_take(step, data, battery.JudgeContext())
    assert result['verdict'] == 'Bad'
    assert all(result['metrics'][key]['verdict'] == 'Bad' for key in ('gain', 'dropouts'))


def test_sandbox_does_not_change_certification_grade():
    results = [dict(step_id=s.id, certification=s.certification, verdict='Good' if s.certification else 'Bad', takes=[])
               for s in battery.BATTERY]
    assert battery.report_results(results)['verdict'] == 'Good'


def test_reference_failure_preserves_capture_and_never_blames_mic():
    data = take()
    def fail(*args):
        raise RuntimeError('reference unavailable')
    data['offline'] = battery.offline_reference(data, fail)
    step = replace(battery.STEPS['baseline'], target='testing', judges=('wer',))
    result = battery.judge_take(step, data, battery.JudgeContext())
    assert data['offline']['status'] == 'reference_failed'
    # 1297: an unavailable reference is Unknown and never lowers the grade.
    assert result['verdict'] == 'Good' and result['cause'] != 'pipeline'
    assert result['metrics']['reference']['verdict'] == 'Unknown'


def test_native_silence_is_not_penalized_by_speech_peak_range():
    raw = np.random.default_rng(4).normal(0, .0001, 32000).astype(np.float32)
    data = dict(raw=raw, sample_rate=16000, native_idle=dict(audio=raw, sample_rate=16000))
    step = replace(battery.STEPS['native_probe'], judges=('native_clipping',))
    assert battery.judge_take(step, data, battery.JudgeContext())['verdict'] == 'Good'
    data['native_idle']['input_overflow'] = True
    assert battery.judge_take(step, data, battery.JudgeContext())['verdict'] == 'Bad'
    data['native_idle']['input_overflow'] = False
    clipped = np.tile(np.r_[np.full(5, .0002), np.full(5, -.0002)], 3200).astype(np.float32)
    data['native_idle']['audio'] = clipped
    assert battery.judge_take(step, data, battery.JudgeContext())['verdict'] == 'Bad'


def test_incomplete_good_steps_do_not_certify_a_run():
    result = battery.report_results([dict(step_id='baseline', certification=True, verdict='Good', takes=[])])
    assert result['complete'] is False and result['verdict'] == 'Watch'


def test_cli_live_workflow_with_fake_capture_writes_complete_run(tmp_path, monkeypatch):
    from tools import mic_lab_cli as cli
    from types import SimpleNamespace as NS
    import sys
    from samsara.mic_lab_endpoint import Observation
    fake_sd = NS(query_devices=lambda *a, **k: dict(name='Fixture microphone', max_input_channels=1, default_samplerate=16000))
    monkeypatch.setitem(sys.modules, 'sounddevice', fake_sd)
    monkeypatch.setattr(cli, 'capture_configuration', lambda *a, **k: Observation(dict(host_api='fake')))
    monkeypatch.setattr(cli, 'inspect_endpoint', lambda **k: Observation(dict(name='Fixture microphone')))
    monkeypatch.setattr(cli, 'endpoint_volume', lambda **k: Observation(.5))
    for name in ('SAMSARA_HOME_DIR', 'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY'):
        monkeypatch.setenv(name, 'temporary')
    class FakeCapture:
        def __init__(self, *a, **k):
            self.target = ''
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def __call__(self, step, number):
            self.target = step.target
            return take(step.target, slices=bool(step.target))
        def _decode_reference(self, audio, params):
            return self.target
    output = []
    assert cli.main(['--device', '0', '--label', 'Fixture', '--profile', str(tmp_path),
                     '--output-dir', str(tmp_path / 'runs')], capture_factory=FakeCapture,
                    model_loader=lambda *a: (object(), 'cpu', 'int8'), output=output.append) == 0
    runs = list((tmp_path / 'runs').iterdir())
    assert len(runs) == 1
    saved = load_run(runs[0])
    assert len(saved['takes']) == 13
    assert json.loads((runs[0] / 'report.json').read_text())['complete']
    assert battery.rejudge_run(saved)['complete']


def test_store_round_trip_is_bit_exact_and_writes_on_worker(tmp_path, monkeypatch):
    from samsara import mic_lab_store as module
    threads = []
    original = module._write_wav
    def write(*args):
        threads.append(threading.current_thread().name)
        return original(*args)
    monkeypatch.setattr(module, '_write_wav', write)
    data = take('test fixture')
    data.update(step_id='baseline', number=1, offline=dict(status='ok', text='test fixture'))
    data['raw'][1:4] = [1.01, -.75, .000000123]
    with MicLabStore(tmp_path) as store:
        run_id = store.start_run(dict(label='fake', mount='boom', distance_cm=20., pop_filter='yes', windows_gain_percent=50.))
        store.save_take(run_id, 'baseline-1', data).result()
        store.finish(run_id, dict(verdict='Watch')).result()
        saved = load_run(tmp_path / run_id)
        assert np.array_equal(saved['takes'][0]['raw'], data['raw'])
        assert np.array_equal(saved['takes'][0]['slices'][0]['audio'], data['slices'][0]['audio'])
        assert saved['device_pin']['windows_gain_percent'] == 50.
        assert all(t.startswith('mic-lab-store') for t in threads)
        store.delete_run(run_id).result()
        assert not (tmp_path / run_id).exists()


@pytest.mark.parametrize('bad', ['../escape', '..', 'a/b', 'C:/escape', ''])
def test_store_rejects_run_traversal(tmp_path, bad):
    with MicLabStore(tmp_path) as store:
        with pytest.raises(ValueError):
            store.delete_run(bad).result()


def test_store_refuses_to_delete_non_run_folder(tmp_path):
    (tmp_path / 'unrelated').mkdir()
    with MicLabStore(tmp_path) as store:
        with pytest.raises(ValueError):
            store.delete_run('unrelated').result()
    assert (tmp_path / 'unrelated').exists()


def test_configurable_private_root_and_profile_default(tmp_path):
    assert storage_root(tmp_path) == tmp_path / 'mic_lab'
    assert storage_root(tmp_path, {'mic_lab': {'output_dir': str(tmp_path / 'private')}}) == tmp_path / 'private'


def test_saved_folder_cli_needs_no_device_or_model(tmp_path, monkeypatch):
    from tools import mic_lab_cli as cli
    data = take('fixture')
    data.update(step_id='baseline', number=1, offline=dict(status='ok', text='fixture'))
    with MicLabStore(tmp_path) as store:
        run_id = store.start_run(dict(label='fake'))
        store.save_take(run_id, 'baseline-1', data).result()
    monkeypatch.setattr(cli, '_load_model', lambda *a: pytest.fail('model loaded'))
    output = []
    # 1297: human reasons are the default; --json preserves machine-readable output.
    assert cli.main(['--from-folder', str(tmp_path / run_id), '--json'], output=output.append) == 2
    result = json.loads(output[0])
    assert result['complete'] is False and result['steps'][0]['incomplete']
    assert not (tmp_path / run_id / 'report.json').exists()


def test_store_rejects_sidecar_escape(tmp_path):
    data = take()
    data.update(step_id='baseline', number=1)
    with MicLabStore(tmp_path) as store:
        run_id = store.start_run(dict(label='fake'))
        folder = store.save_take(run_id, 'baseline-1', data).result()
    sidecar = folder / 'take.json'
    content = json.loads(sidecar.read_text())
    content['raw_wav'] = '../../outside.wav'
    sidecar.write_text(json.dumps(content))
    with pytest.raises(ValueError, match='escapes'):
        load_run(tmp_path / run_id)


def test_cli_parser_is_import_safe_and_requires_device_for_live():
    from tools import mic_lab_cli as cli
    args = cli.parser().parse_args(['--device', 'fixture', '--label', 'Test'])
    assert args.device == 'fixture' and args.distance_cm == 20.
    with pytest.raises(SystemExit) as error:
        cli.main([])
    assert error.value.code == 2


def test_real_capture_adapter_uses_ring_tail_and_observed_decoder(monkeypatch):
    """Exercise the CLI adapter with the real ring/consumer/decoder, fake device/model."""
    from queue import Queue
    from types import SimpleNamespace as NS
    import time
    from tools import mic_lab_cli as cli
    from samsara import diagnostics, speech_presence
    from samsara.audio_engine import FrameBus, DictationSessionConsumer
    from samsara.mic_lab_endpoint import Observation
    from samsara.config_defaults import DEFAULTS
    import copy
    bus = FrameBus()
    engine = NS(register_consumer=bus.new_reader, _overflow_count=0)
    received = []
    class Model:
        def transcribe(self, audio, **params):
            received.append(np.array(audio, copy=True))
            return iter([NS(text=battery.STEPS['baseline'].target, start=0., end=.8,
                temperature=0., avg_logprob=-.2, compression_ratio=1.1, no_speech_prob=.01)]), NS(language='en')
    config = copy.deepcopy(DEFAULTS)
    config.update(mode='hold', microphone_name='Fixture', model_size='fake', compute_type='int8',
                  diagnostics={'write_jsonl': False}, kept_dictations={'enabled': False})
    model = Model()
    adapter = cli.RealCapture.__new__(cli.RealCapture)
    adapter.config, adapter.model, adapter.bus, adapter.engine = config, model, bus, engine
    adapter.app = cli.headless_app(model, config)
    adapter.consumer = DictationSessionConsumer(engine, adapter.app)
    adapter.app._dictation_consumer = adapter.consumer
    adapter.events, adapter.output, adapter.started = Queue(), lambda *a: None, False
    rng = np.random.default_rng(0)
    def feed(seconds):
        for _ in range(max(1, round(seconds * 10))):
            bus.write(rng.integers(-3000, 3000, 1600, dtype=np.int16), time.perf_counter(), 0)
    adapter.sleep = feed
    monkeypatch.setattr(speech_presence, 'measure', lambda *a: None)
    monkeypatch.setattr(cli, 'endpoint_volume', lambda **k: Observation(.5))
    diagnostics.set_mic_lab_observer(adapter.events.put)
    try:
        event = adapter(battery.STEPS['baseline'], 1)
        assert event['text'] == battery.STEPS['baseline'].target
        assert event['raw_coordinates']['provenance'] == 'ring_snapshot'
        assert len(event['slices']) == len(received) == 1
        assert np.array_equal(event['slices'][0]['audio'], received[0])
        assert event['gain_before'] == event['gain_after'] == .5
    finally:
        adapter.consumer.cancel()
        diagnostics.flush_mic_lab_observer()
        diagnostics.set_mic_lab_observer(None)
