"""Audit copied recognition metadata without exporting speech, prompts or secrets.

No implicit home/profile discovery, History DB access, model loads or network.
Pass the runner's fixture directory explicitly. Missing evidence stays unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
import json
from pathlib import Path
import re


MODELS = {s + suffix for s in ('tiny', 'base', 'small', 'medium')
          for suffix in ('', '.en')} | {'large-v1', 'large-v2', 'large-v3', 'turbo'}
ENUMS = {
    'model_size': MODELS,
    'compute_type': {'default', 'auto', 'float16', 'float32', 'int8', 'int8_float16', 'int8_float32'},
    'device': {'auto', 'cpu', 'cuda'},
    'language': {'auto', 'en'},
    'performance_mode': {'fast', 'balanced', 'accurate'},
}
STAMP = re.compile(r'^config-(\d{8})-(\d{6})(?:-\d+)?\.json$')
DAY = re.compile(r'^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d{3} ')
MODEL_LOAD = re.compile(r'\[CONFIG\] Model: ([^\s,]+), Device: ([^\s,]+), Compute: ([^\s,]+)\s*$')
PEAK = re.compile(r'(?<!\w)peak=(-?\d+(?:\.\d+)?)(?=\s|$)')


def safe_config(data):
    """Fixed projection: never return arbitrary config strings or nested values."""
    result = {k: data.get(k) if isinstance(data.get(k), str) and data[k] in choices
              else 'unknown' for k, choices in ENUMS.items()}
    result['explicit_prompt_present'] = bool(data.get('initial_prompt'))
    result['microphone_pinned'] = data.get('microphone') is not None
    result['microphone_named'] = bool(data.get('microphone_name'))
    value = data.get('builtin_vocabulary_enabled')
    result['builtin_vocabulary_enabled'] = value if type(value) is bool else 'unknown'
    return result


def snapshots(root):
    """Backup timestamps date PRE-write disk states, not the change itself."""
    result = []
    for profile in ('samsara-daily', 'samsara'):
        folder = root / profile
        paths = sorted((folder / 'config_backups').glob('config-*.json'))
        paths += [folder / 'config.json', folder / 'config.json.bak']
        for path in paths:
            if not path.is_file():
                continue
            match = STAMP.fullmatch(path.name)
            if path.name not in ('config.json', 'config.json.bak') and not match:
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
            except (ValueError, OSError):
                result.append({'profile': profile, 'snapshot': path.name, 'invalid': True})
                continue
            if not isinstance(data, dict):
                result.append({'profile': profile, 'snapshot': path.name, 'invalid': True})
                continue
            result.append({'profile': profile, 'snapshot': path.name, **safe_config(data)})
    return result


def audit(root, *, end, days=10):
    """Count observable events only; a delivery marker is not an error metric."""
    if not 1 <= days <= 366:
        raise ValueError('days must be between 1 and 366')
    daily = {}
    for offset in reversed(range(days)):
        day = (end - timedelta(days=offset)).isoformat()
        daily[day] = dict(log_lines=0, delivered=0, peaks=[], loads=Counter(),
                          preview_corrections=0)
    # Only the dictation log has the documented event grammar. Never read Ava
    # conversations, crash dumps or a History DB to approximate this information.
    path = root / 'logs' / 'daily_samsara.log'
    if path.is_file():
        with path.open(encoding='utf-8-sig', errors='replace') as source:
            for line in source:
                stamp = DAY.match(line)
                if not stamp or stamp[1] not in daily:
                    continue
                row = daily[stamp[1]]
                row['log_lines'] += 1
                if '[TRANSCRIBED] Held dictation ready for delivery' in line:
                    row['delivered'] += 1
                if '[DICTATE-PREVIEW] correction choice ' in line:
                    row['preview_corrections'] += 1
                if '[GATE] speech_presence ' in line:
                    peak = PEAK.search(line)
                    if peak and 0 <= float(peak[1]) <= 1:
                        row['peaks'].append(float(peak[1]))
                load = MODEL_LOAD.search(line)
                if load:
                    values = (load[1], load[2], load[3])
                    allowed = (MODELS, ENUMS['device'], ENUMS['compute_type'])
                    safe = tuple(v if v in choices else 'unknown'
                                 for v, choices in zip(values, allowed))
                    row['loads'][' / '.join(safe)] += 1
    return {'daily': daily, 'snapshots': snapshots(root)}


def render(result):
    lines = [
        '# Recognition metadata audit', '',
        '## PREMISES',
        '- Backup names timestamp PRE-write snapshots; they do not identify the writer or exact change time.',
        '- Missing dates mean unavailable evidence, not zero utterances or zero errors.',
        '- Held-delivery markers and speech-presence captures count different pipeline events.',
        '- Peaks cannot establish clipped-sample fraction. No audio or reference transcript was supplied.',
        '- Prompt contents, dictated text, correction strings, private paths and secrets are never exported.', '',
        '| Day | Log lines | Held delivery markers | Model / device / precision (load count) | Captures with peak | Mean / max peak | Captures peak >= .99 | Preview correction choices |',
        '|---|---:|---:|---|---:|---|---:|---:|',
    ]
    for day, row in result['daily'].items():
        if not row['log_lines']:
            lines.append(f'| {day} | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable |')
            continue
        peaks = row['peaks']
        stats = f'{sum(peaks)/len(peaks):.6f} / {max(peaks):.6f}' if peaks else 'unavailable'
        loads = '; '.join(f'{key} ({count})' for key, count in sorted(row['loads'].items())) or 'unavailable'
        lines.append(f"| {day} | {row['log_lines']} | {row['delivered']} | {loads} | {len(peaks)} | {stats} | {sum(p >= .99 for p in peaks)} | {row['preview_corrections']} |")
    lines += ['', 'Immediate scratch/retype/correction rate, correction-rule hits, per-utterance model/prompt, and clipped-sample fraction: unavailable. Preview choices alone do not measure overall correction rate.', '',
              '## Config timeline (allow-listed metadata)', '',
              '| Profile | Snapshot | Model | Performance | Device | Precision | Language | Explicit prompt present | Mic pinned / named | Built-in vocabulary |',
              '|---|---|---|---|---|---|---|---|---|---|']
    for row in result['snapshots']:
        if row.get('invalid'):
            lines.append(f"| {row['profile']} | {row['snapshot']} | unreadable | | | | | | | |")
            continue
        values = [row[k] for k in ('profile', 'snapshot', 'model_size', 'performance_mode', 'device', 'compute_type', 'language', 'explicit_prompt_present')]
        values += [f"{row['microphone_pinned']} / {row['microphone_named']}", row['builtin_vocabulary_enabled']]
        lines.append('| ' + ' | '.join(map(str, values)) + ' |')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    parser.add_argument('--days', type=int, default=10)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = render(audit(args.fixtures, end=args.end, days=args.days))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
