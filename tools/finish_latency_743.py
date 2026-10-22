"""Replay copied timing evidence or isolated PCM control fixtures; never open a live profile.

--log accepts stage-timing JSONL (exact marks) or ordinary logs (missing marks
stay unknown). --fixtures uses the real endpoint/commit code with a stub decoder
and paste sink. Its numbers are control-path measurements, NOT Whisper latency.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time


def stage_rows(lines, start=None, end=None):
    """Only utterance-correlated marks can establish exact per-commit timing."""
    rows = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        created = record.get('created_at', '')
        if start and created < start or end and created > end:
            continue
        if record.get('outcome') not in ('typed', 'dictate_committed'):
            continue
        if record.get('route') != 'handsfree' or record.get('mode') != 'dictate':
            continue
        marks = record.get('timestamps_ns', {})
        voice, close, paste = (marks.get(key) for key in ('last_voice', 'speech_end', 'action_done'))
        delta = lambda a, b: (b - a) / 1e9 if a is not None and b is not None else None
        rows.append(dict(commit=len(rows) + 1, endpoint_s=delta(voice, close),
                         post_close_s=delta(close, paste), total_s=delta(voice, paste)))
    return rows


def log_rows(lines, start=None, end=None):
    """Keep exact endpoints unknown; expose separately labelled legacy proxies.

    Gate entry happens after capture closes and may include queue delay. The
    longest detected speech run need not be the last speech in the buffer.
    Neither mark can replace a flight-recorder endpoint timestamp.
    """
    rows = []
    gate_at = run_tail = redecode_at = None
    for line in lines:
        stamp = re.match(r'(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?)', line)
        if not stamp:
            continue
        at = stamp[1].replace(',', '.').replace(' ', 'T')
        if start and at < start or end and at > end:
            continue
        timestamp = datetime.fromisoformat(at)
        if 'speech_presence path=handsfree ' in line:
            gate_at, run_tail, redecode_at = timestamp, None, None
        run = re.search(r'max contiguous speech (\d+)ms at ([\d.]+)s \(buffer ([\d.]+)s\)', line)
        if run and gate_at is not None:
            run_tail = float(run[3]) - float(run[2]) - int(run[1]) / 1000
        if 'speech_presence path=commit_redecode ' in line:
            redecode_at = timestamp
        if 'dictate_committed' in line:
            if re.search(r"['\"]empty['\"]:\s*True", line):
                gate_at = run_tail = redecode_at = None
                continue
            delta = lambda earlier: (timestamp - earlier).total_seconds() if earlier is not None else None
            rows.append(dict(commit=len(rows) + 1, endpoint_s=None, post_close_s=None,
                             total_s=None, committed_at=at, gate_to_commit_s=delta(gate_at),
                             longest_run_tail_s=run_tail, redecode_to_commit_s=delta(redecode_at)))
            gate_at = run_tail = redecode_at = None
    return rows


def fixture_rows():
    """Five 60-second staged PCM thoughts per pace; no mic, output, app or model."""
    if not os.environ.get('SAMSARA_HOME_DIR'):
        raise ValueError('--fixtures requires an isolated SAMSARA_HOME_DIR')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from types import SimpleNamespace
    from unittest.mock import patch
    from tests.test_finish_latency_743 import Decoder, make_manager, stage, preview_fixture, SIGNALS, _loud_frame
    from samsara import session_modes as sm
    from samsara.audio_engine import wake_consumer as wake

    old_word = sm.DICTATE_COMMIT_PHRASE
    sm.set_commit_phrase('finish')
    rows = []
    try:
        for pace in ('standard', 'unhurried'):
            for number in range(1, 6):
                decoder, pasted = Decoder(), []
                manager = make_manager(decoder, pasted)
                for _ in range(20):
                    stage(manager)
                expected = manager.dictate_pending_buffer
                consumer, _, _ = preview_fixture(pace)
                now, result = [100.0], {}

                def flush(*args, **kwargs):
                    result['endpoint_s'] = (kwargs['speech_end_ns'] - kwargs['last_voice_ns']) / 1e9
                    began = time.perf_counter()
                    outcome = manager.dispatch_utterance('finish', SIGNALS)
                    result['post_close_s'] = time.perf_counter() - began
                    assert outcome.kind == 'dictate_committed' and pasted == [expected]

                consumer._flush = flush
                frame = _loud_frame()
                frame.pcm[:] = 0
                clock = SimpleNamespace(time=lambda: now[0], perf_counter_ns=lambda: int(now[0] * 1e9),
                                        monotonic=lambda: now[0])
                with patch.object(wake, 'time', clock):
                    for tick in range(1, 52):
                        now[0] = 100.0 + tick * 0.1
                        consumer._process_frame(frame)
                        if result:
                            break
                if not result:
                    raise RuntimeError('fixture failed to close within 5.1s')
                result.update(pace=pace, commit=number, redecode_calls=len(decoder.calls))
                result['total_s'] = result['endpoint_s'] + result['post_close_s']
                rows.append(result)
    finally:
        sm.set_commit_phrase(old_word)
    return rows


def table(rows):
    fmt = lambda value: 'unknown' if value is None else f'{value:.4f}'
    lines = ['| Pace | Commit | Close - last speech (s) | Paste - close (s) | Total (s) |',
             '|---|---:|---:|---:|---:|']
    for row in rows:
        lines.append(f"| {row.get('pace', 'recorded')} | {row['commit']} | "
                     f"{fmt(row['endpoint_s'])} | {fmt(row['post_close_s'])} | {fmt(row['total_s'])} |")
    return '\n'.join(lines)


def legacy_table(rows):
    """Supplement missing exact marks without presenting proxies as endpoints."""
    fmt = lambda value: 'unknown' if value is None else f'{value:.4f}'
    lines = ['| Commit time | Buffer tail after longest run (s) | Gate entry to commit (s) | Re-decode entry to commit (s) |',
             '|---|---:|---:|---:|']
    for row in rows:
        lines.append(f"| {row['committed_at']} | {fmt(row['longest_run_tail_s'])} | "
                     f"{fmt(row['gate_to_commit_s'])} | {fmt(row['redecode_to_commit_s'])} |")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--log', type=Path, help='copied evidence only; live profile paths refused')
    source.add_argument('--fixtures', action='store_true')
    parser.add_argument('--start', help='inclusive ISO local timestamp, e.g. 2026-10-21T20:17:00')
    parser.add_argument('--end', help='inclusive ISO local timestamp, e.g. 2026-10-21T20:25:00')
    parser.add_argument('--json-output', type=Path)
    args = parser.parse_args(argv)
    for value in (args.start, args.end):
        if value:
            datetime.fromisoformat(value)
    if args.fixtures:
        rows = fixture_rows()
        print('Synthetic PCM control-path replay; decoder and paste stubbed; 100ms frame clock.')
    else:
        path = args.log.resolve()
        if any(part.lower() in ('.samsara', '.samsara-daily') for part in path.parts):
            parser.error('live profile access is prohibited; supply an already copied evidence file')
        lines = path.read_text(encoding='utf-8').splitlines()
        rows = stage_rows(lines, args.start, args.end) or log_rows(lines, args.start, args.end)
        print('Copied evidence replay; missing endpoint marks stay unknown.')
    print(table(rows))
    if any('gate_to_commit_s' in row for row in rows):
        print('\nLegacy proxies only: gate entry follows capture close; longest run may precede last speech.')
        print(legacy_table(rows))
    if args.json_output:
        args.json_output.write_text(json.dumps(rows, indent=2) + '\n', encoding='utf-8')
    return 0 if rows else 2


if __name__ == '__main__':
    raise SystemExit(main())
