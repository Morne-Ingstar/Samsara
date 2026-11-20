"""Build the private device ledger from finished Mic Lab runs."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
import json
import math
from pathlib import Path
from struct import error as StructError
from threading import Lock

from samsara.mic_lab_battery import rejudge_run
from samsara.mic_lab_endpoint import interface_kind
from samsara.mic_lab_store import _inside, _json, _write_text, load_run


_GRADE_ORDER = {'Good': 0, 'Watch': 1, 'Bad': 2, 'Unknown': 3}
_REBUILD_LOCK = Lock()
_SIDECARS = ('run.json', 'device_pin.json', 'report.json')


def _object(value):
    if not isinstance(value, dict):
        raise ValueError('Expected a Mic Lab JSON object')
    return value


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def _values(measurement):
    value = measurement['value']
    return value if isinstance(value, list) else [value]


def _minimum(measurements):
    values = [_number(value) for m in measurements for value in _values(m)]
    return min(values) if values and None not in values else None


def _event_count(measurements, *keys):
    values = [value for m in measurements for value in _values(m)]
    if not values or any(not isinstance(value, dict) for value in values):
        return None
    return sum(value[key] for value in values for key in keys)


def _run_row(folder):
    run = load_run(folder)
    if run['run_id'] != folder.name:
        raise ValueError('Saved run identifier disagrees with its folder')
    created = datetime.fromisoformat(run['created_at'])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    metadata = _object(run.get('metadata', {}))
    pin = _object(run['device_pin'])
    capture = _object(pin.get('capture', {})).get('value')
    capture = capture if isinstance(capture, dict) else {}
    endpoint = _object(pin.get('endpoint', {})).get('value')
    endpoint = endpoint if isinstance(endpoint, dict) else {}
    name = _text(endpoint.get('name')) or _text(capture.get('name'))
    interface = endpoint.get('interface_kind')
    if isinstance(interface, dict):
        interface = interface.get('value')
    if not interface or interface == 'unknown':
        interface = interface_kind(name or '').value
    gain = _number(pin.get('windows_gain_percent'))
    if gain is not None and not 0 <= gain <= 100:
        gain = None

    measures = defaultdict(list)
    native_load = False

    def collect(step, takes):
        nonlocal native_load
        if not step.certification:
            return
        for take in takes:
            metrics = take['judgment']['metrics']
            for key in ('noise', 'clipping', 'native_clipping', 'dropouts'):
                if key in metrics:
                    measures[key].append(metrics[key])
            if step.id == 'native_probe':
                native_load = bool(take.get('native_load'))
            if step.id == 'baseline':
                measures['snr'].append(metrics['snr'])
            if step.id == 'fricatives_hf':
                measures['bandwidth'].append(metrics['bandwidth'])

    report = rejudge_run(run, on_step=collect)
    if not report['complete']:
        return None

    noise = measures['noise'][0]
    floor = 'gated' if any(noise['details'].get('gated', [])) else noise['value']
    missing = list(report['unknown'])
    # PLACEHOLDER COPY (1299)
    if gain is None:
        missing.append('Windows input level was not recorded')
    return {
        'run_id': folder.name, 'date': created.date().isoformat(),
        'created_at': created.astimezone(timezone.utc).isoformat(),
        'label': _text(pin.get('label')), 'endpoint_name': name,
        'interface_kind': None if interface == 'unknown' else interface,
        'sample_rate': _number(capture.get('sample_rate')) or _number(metadata.get('sample_rate')),
        'windows_gain_percent': gain, 'mount': _text(pin.get('mount')),
        'distance_cm': _number(pin.get('distance_cm')), 'pop_filter': _text(pin.get('pop_filter')),
        'samsara_commit': _text(metadata.get('samsara_commit')) or _text(metadata.get('commit')),
        'grade': report['verdict'], 'findings': report['findings'][:2],
        'accuracy': report['accuracy'], 'all_scripted_accuracy': report['all_scripted_accuracy'],
        'noise_floor_dbfs': floor, 'unknown': missing, 'replugged': pin.get('replugged') is True,
        'gpu_load_probe': native_load and metadata.get('backend') == 'cuda',
        'minimum_snr_db': _minimum(measures['snr']),
        'minimum_bandwidth_db': _minimum(measures['bandwidth']),
        'flat_top_runs': _event_count(measures['clipping'] + measures['native_clipping'], 'flat_top_runs'),
        'dropouts': _event_count(measures['dropouts'], 'zero_runs', 'input_overflows'),
    }


# PLACEHOLDER COPY (1299)
def _confirmation(good):
    if len(good) < 2:
        return None, 'Complete two Good runs'

    # Search newest pairs first. The later run's mark must describe a reset
    # since an earlier run, so a mark on the first run cannot confirm a pair.
    best_progress, reason = -1, None
    for later, earlier in combinations(reversed(good), 2):
        pair = (earlier, later)
        words = sum(row['all_scripted_accuracy']['words'] for row in pair)
        errors = sum(row['all_scripted_accuracy']['errors'] for row in pair)
        checks = (
            (earlier['created_at'] < later['created_at'] and later['replugged'],
             'Unplug and reconnect or reboot between Good runs and mark the later run'),
            (any(row['gpu_load_probe'] for row in pair), 'Run the native load check with the CUDA backend'),
            (words >= 150 and errors * 100 <= words * 2,
             'Score at least 150 scripted words across the pair with at most 2% errors'),
            (all(row['minimum_snr_db'] is not None and row['minimum_snr_db'] >= 25 for row in pair),
             'Measure a signal-to-noise ratio of at least 25 dB in both runs'),
            (all(row['minimum_bandwidth_db'] is not None and row['minimum_bandwidth_db'] >= -20 for row in pair),
             'Measure an upper-band ratio of at least −20 dB in both runs'),
            (all(row['flat_top_runs'] == 0 and row['dropouts'] == 0 for row in pair),
             'Complete both runs with zero flat-top clips and zero capture gaps'),
        )
        for progress, (passed, message) in enumerate(checks):
            if not passed:
                if progress > best_progress:
                    best_progress, reason = progress, message
                break
        else:
            return pair, None
    return None, reason


def _device_rows(runs):
    groups = defaultdict(list)
    for row in runs:
        label, endpoint = row['label'], row['endpoint_name']
        groups[(label, endpoint, None if label and endpoint else row['run_id'])].append(row)

    devices = []
    for (label, endpoint, _), rows in groups.items():
        best = min((row['grade'] for row in rows), key=_GRADE_ORDER.__getitem__)
        latest = rows[-1]
        good = [row for row in rows if row['grade'] == 'Good']
        pair, reason = _confirmation(good)
        gains = [row['windows_gain_percent'] for row in pair or () if row['windows_gain_percent'] is not None]
        status = 'Confirmed working' if pair else 'Needs another run' if good else f'Tested: {best}'
        devices.append({
            'label': label, 'endpoint_name': endpoint, 'runs': [row['run_id'] for row in rows],
            'best_grade': best, 'latest_grade': latest['grade'], 'status': status,
            'confirmation_reason': reason if good else None,
            'confirmed_runs': [row['run_id'] for row in pair or ()],
            'passed_windows_gain_percent_range': [min(gains), max(gains)] if len(gains) == 2 else None,
            'latest_run': latest['run_id'],
        })
    return sorted(devices, key=lambda d: (not d['confirmed_runs'], _GRADE_ORDER[d['best_grade']],
                                          (d['label'] or '').casefold(), (d['endpoint_name'] or '').casefold()))


def _run_folders(root):
    try:
        return sorted(folder for folder in root.iterdir() if folder.is_dir()
                      and any((folder / name).is_file() for name in _SIDECARS))
    except FileNotFoundError:
        return []


def build_devices(root):
    """Read and re-grade saved runs. Produce no files or hardware activity."""
    root = Path(root).resolve()
    runs, skipped = [], []
    for folder in _run_folders(root):
        try:
            _inside(root, folder.name)
            for name in _SIDECARS:
                _inside(folder, name)
            report_path = folder / 'report.json'
            # A report is the finish marker. Its stored grade is never used.
            if report_path.is_file():
                _object(json.loads(report_path.read_text(encoding='utf-8')))
                row = _run_row(folder)
                if row is not None:
                    runs.append(row)
                    continue
            reason = 'Run is unfinished or is missing required takes'
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, OverflowError,
                EOFError, StructError):
            # Rejected metadata and measurements can contain transcripts in
            # their exception messages. Keep only a generic note in the index.
            reason = 'Saved run data could not be read or re-graded'
        skipped.append({'run_id': folder.name, 'reason': reason})

    runs.sort(key=lambda row: (row['created_at'], row['run_id']))
    return {'schema': 1, 'runs': runs, 'devices': _device_rows(runs), 'skipped': skipped}


def _cell(value):
    if value is None:
        return 'Unknown'
    text = ' '.join(str(value).split())
    for character in ('\\', '|', '*', '_', '[', ']', '`', '<', '>'):
        text = text.replace(character, '\\' + character)
    return text


# PLACEHOLDER COPY (1299)
def _action(device, latest):
    for finding in latest['findings']:
        if 'lower the microphone level' in finding:
            return 'Lower the microphone level in Windows and run the test again.'
        if 'input level low' in finding:
            return 'Move closer or raise the microphone level in Windows.'
        if 'narrowband device mode' in finding:
            return 'Check whether the microphone is using a narrowband mode.'
    if latest['grade'] in ('Watch', 'Bad') and latest['findings']:
        return '. '.join(latest['findings'][0].split('; '))
    return device['confirmation_reason'] or '—'


# PLACEHOLDER COPY (1299)
def render_devices(ledger):
    lines = ['# Tested microphones', '']
    runs = {run['run_id']: run for run in ledger['runs']}
    if ledger['devices']:
        lines += [
            '| Microphone | Windows input | Connection | Runs | Best | Latest | Status | Windows input level | What to change |',
            '| --- | --- | --- | ---: | --- | --- | --- | --- | --- |',
        ]
        for device in ledger['devices']:
            latest = runs[device['latest_run']]
            gain = latest['windows_gain_percent']
            gain_text = f'{gain:g}% (latest)' if gain is not None else 'Unknown'
            passed = device['passed_windows_gain_percent_range']
            if passed:
                low, high = passed
                gain_text = f'{low:g}% (passed)' if low == high else f'{low:g}%–{high:g}% (passed)'
            elif device['confirmed_runs']:
                gain_text = 'Passed range unknown'
            cells = (device['label'], device['endpoint_name'], latest['interface_kind'], len(device['runs']),
                     device['best_grade'], device['latest_grade'], device['status'], gain_text,
                     _action(device, latest))
            lines.append('| ' + ' | '.join(map(_cell, cells)) + ' |')
    else:
        lines.append('No completed microphone runs yet.')
    if ledger['skipped']:
        lines.extend(('', '## Skipped folders', ''))
        lines.extend(f"- {_cell(row['run_id'])} — {row['reason']}." for row in ledger['skipped'])
    return '\n'.join(lines) + '\n'


def rebuild_devices(root):
    """Refresh both derived files after a save or on demand."""
    root = Path(root).resolve()
    with _REBUILD_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        while True:
            before = _run_revision(root)
            ledger = build_devices(root)
            table = render_devices(ledger)
            if before != _run_revision(root):
                continue
            _json(root / 'devices.json', ledger)
            _write_text(root / 'DEVICES.md', table)
            # A concurrent CLI process can finish a run during publication.
            # Retry until the files reflect the same source revision.
            if before == _run_revision(root):
                return table


def _run_revision(root):
    revision = []
    for folder in _run_folders(root):
        revision.append(folder.name)
        for name in _SIDECARS:
            try:
                info = (folder / name).lstat()
                revision.append((info.st_mtime_ns, info.st_size, info.st_ino))
            except FileNotFoundError:
                revision.append(None)
    return revision


def show_devices(root):
    """Read the saved table or show an in-memory view if it has never been built."""
    try:
        return (Path(root) / 'DEVICES.md').read_text(encoding='utf-8')
    except FileNotFoundError:
        return render_devices(build_devices(root))
