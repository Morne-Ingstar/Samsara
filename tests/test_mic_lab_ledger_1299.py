import json

import numpy as np
import pytest

from samsara.mic_lab_battery import BATTERY
from samsara.mic_lab_ledger import build_devices
from samsara.mic_lab_store import MicLabStore
from tools import mic_lab_cli as cli


RATE = 16000


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def finish_run(root, run_id, *, label='Desk mic', name='USB microphone', gain=50,
               replugged=False, gpu=False, narrow=False, gated=False):
    rng = np.random.default_rng(1299)
    quiet = rng.normal(0, 0.0005, RATE * 3).astype(np.float32)
    speech = rng.normal(0, 0.045, RATE * 3).astype(np.float32)
    t = np.arange(RATE * 3) / RATE
    narrow_speech = (0.06 * np.sin(2 * np.pi * 1100 * t)).astype(np.float32)
    pin = dict(label=label, mount='boom', distance_cm=22, pop_filter='yes',
               windows_gain_percent=gain, replugged=replugged,
               capture={'value': {'name': name, 'sample_rate': 48000}})
    with MicLabStore(root) as store:
        store.start_run(pin, run_id=run_id, metadata=dict(sample_rate=RATE, commit='abcdef0',
                                                         backend='cuda' if gpu else 'cpu'))
        for step in BATTERY:
            if not step.certification:
                continue
            raw = speech
            extra = {}
            if step.id == 'native_probe':
                raw = np.zeros_like(quiet) if gated else quiet
                extra['native_idle'] = dict(audio=quiet, sample_rate=RATE)
                if gpu:
                    extra['native_load'] = dict(audio=quiet, sample_rate=RATE)
            elif step.id == 'fricatives_hf' and narrow:
                raw = narrow_speech
            for number in range(1, step.takes + 1):
                take = dict(sample_rate=RATE, raw=raw, step_id=step.id, number=number,
                            text=step.target, offline=dict(status='ok', text=step.target),
                            gain_before=.5, gain_after=.5, **extra)
                store.save_take(run_id, f'{step.id}-{number}', take).result()
        # The ledger must use current grading in place of this saved verdict.
        store.finish(run_id, dict(verdict='Bad')).result()


@pytest.mark.parametrize(('first_mark', 'second_mark', 'gpu', 'gated', 'status', 'reason'), [
    (False, True, True, False, 'Confirmed working', None),
    (True, False, True, False, 'Needs another run', 'mark the later run'),
    (False, True, False, False, 'Needs another run', 'CUDA backend'),
    (False, True, True, True, 'Needs another run', 'signal-to-noise ratio'),
])
def test_confirmation_uses_current_grade_reset_and_measured_evidence(
        tmp_path, first_mark, second_mark, gpu, gated, status, reason):
    finish_run(tmp_path, 'first', gain=48, replugged=first_mark, gpu=gpu)
    finish_run(tmp_path, 'second', gain=55, replugged=second_mark, gated=gated)
    ledger = read_json(tmp_path / 'devices.json')
    assert [row['grade'] for row in ledger['runs']] == ['Good', 'Good']
    assert read_json(tmp_path / 'second' / 'report.json')['verdict'] == 'Bad'
    device, = ledger['devices']
    assert device['status'] == status
    table = (tmp_path / 'DEVICES.md').read_text(encoding='utf-8')
    if reason:
        assert reason in device['confirmation_reason']
        assert reason in table
        assert device['passed_windows_gain_percent_range'] is None
    else:
        assert device['confirmed_runs'] == ['first', 'second']
        assert device['passed_windows_gain_percent_range'] == [48, 55]
        assert '48%–55%' in table


def test_ledger_metadata_narrowband_skips_and_offline_cli(tmp_path):
    finish_run(tmp_path, 'bad', label='Headset', name='Bluetooth hands-free', gain=None, narrow=True, gated=True)
    finish_run(tmp_path, 'good', label='Desk | mic')
    finish_run(tmp_path, 'same-label', label='Desk | mic', name='Other USB microphone')
    partial = tmp_path / 'partial'
    partial.mkdir()
    (partial / 'run.json').write_text('{}', encoding='utf-8')
    corrupt = tmp_path / 'corrupt'
    corrupt.mkdir()
    (corrupt / 'report.json').write_text('{broken', encoding='utf-8')
    corrupt_take = tmp_path / 'same-label' / 'cold_onset-1' / 'raw.wav'
    corrupt_take.write_bytes(b'RIFF')

    def forbidden(*args, **kwargs):
        pytest.fail('Ledger modes must not open a model or microphone')

    opts = dict(model_loader=forbidden, capture_factory=forbidden)
    base_args = ['--output-dir', str(tmp_path)]
    rebuilt = []
    assert cli.main(['--rebuild-devices', *base_args], output=rebuilt.append, **opts) == 0
    json_file, table_file = tmp_path / 'devices.json', tmp_path / 'DEVICES.md'
    previous = json_file.read_bytes(), table_file.read_bytes()
    assert cli.main(['--rebuild-devices', *base_args], output=rebuilt.append, **opts) == 0
    assert previous == (json_file.read_bytes(), table_file.read_bytes())
    shown = []
    assert cli.main(['--devices', *base_args], output=shown.append, **opts) == 0
    assert shown == rebuilt[:1] == rebuilt[1:]
    assert 'Check whether the microphone is using a narrowband mode.' in shown[0]
    assert r'Desk \| mic' in shown[0]

    ledger = read_json(json_file)
    assert [device['best_grade'] for device in ledger['devices']] == ['Good', 'Bad']
    assert len(ledger['devices'][0]['runs']) == 1
    bad = ledger['runs'][0]
    assert bad['grade'] == 'Bad' and 'narrowband' in bad['findings'][0]
    assert bad['interface_kind'] == 'Bluetooth hands-free'
    assert bad['windows_gain_percent'] is None and bad['noise_floor_dbfs'] == 'gated'
    assert 'Windows input level was not recorded' in bad['unknown']
    assert bad['sample_rate'] == 48000 and bad['samsara_commit'] == 'abcdef0'
    assert (bad['mount'], bad['distance_cm'], bad['pop_filter']) == ('boom', 22, 'yes')
    assert bad['accuracy']['wer'] == 0
    assert [row['run_id'] for row in ledger['skipped']] == ['corrupt', 'partial', 'same-label']
    assert all(row['reason'] in shown[0] for row in ledger['skipped'])


def test_grouping_latest_and_delete_refresh(tmp_path):
    finish_run(tmp_path, 'good')
    finish_run(tmp_path, 'bad', narrow=True)
    finish_run(tmp_path, 'different-endpoint', name='Other USB microphone')
    finish_run(tmp_path, 'different-label', label='Second mic')
    ledger = build_devices(tmp_path)
    assert len(ledger['devices']) == 3
    group = next(device for device in ledger['devices'] if len(device['runs']) == 2)
    assert group['best_grade'] == 'Good' and group['latest_grade'] == 'Bad'
    with MicLabStore(tmp_path) as store:
        store.delete_run('bad').result()
    assert 'bad' not in {row['run_id'] for row in read_json(tmp_path / 'devices.json')['runs']}
