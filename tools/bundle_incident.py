"""Incident bundler: snapshot flight-recorder events, log tail, freeze-stack
dumps, probe/guard logs, config, and the repo's git HEAD into one directory,
so a post-hoc read (human or a later Claude session) doesn't have to go
hunting across however many places evidence for one failure landed.

CLI:
    python tools/bundle_incident.py [<SAMSARA_HOME>]

<SAMSARA_HOME> defaults to samsara.paths.samsara_home_dir() (honors the
SAMSARA_HOME_DIR override the rest of the app uses). Writes
<SAMSARA_HOME>/incidents/incident_<timestamp>/ and prints its path.

freeze_stacks.txt's dump-boundary format is not defined anywhere in this
repo (no writer for that file lives here) -- _tail_freeze_dumps uses a
leading-timestamp heuristic to find dump boundaries and falls back to a
flat line-count tail if no boundaries are found. See the report for detail.
"""
from __future__ import annotations

import argparse
import collections
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from samsara.paths import samsara_home_dir

_FLIGHT_EVENT_LIMIT = 500
_LOG_LINE_LIMIT = 300
_FREEZE_DUMP_COUNT = 2
_FREEZE_FALLBACK_LINES = 200
# Heuristic dump-boundary marker: a line starting with non-whitespace
# followed shortly by an ISO-ish timestamp (e.g. "Dump #94 2026-08-02
# 14:03:11" or "--- 2026-08-02T14:03:11 ---").
_DUMP_BOUNDARY_RE = re.compile(r'^\S.{0,40}\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}')


def _tail_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0 or not path.exists():
        return []
    with path.open('r', encoding='utf-8', errors='replace') as handle:
        return list(collections.deque(handle, maxlen=limit))


def _tail_flight_events(flight_dir: Path, limit: int) -> list[str]:
    """Most recent `limit` JSONL lines across daily-rotated event files,
    oldest to newest, newest file's tail weighted first."""
    if not flight_dir.is_dir():
        return []
    files = sorted(flight_dir.glob('events-*.jsonl'))
    collected: list[str] = []
    for path in reversed(files):
        remaining = limit - len(collected)
        if remaining <= 0:
            break
        collected = _tail_lines(path, remaining) + collected
    return collected[-limit:]


def _tail_freeze_dumps(path: Path, dump_count: int, fallback_lines: int) -> str:
    if not path.exists():
        return ''
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines(keepends=True)
    boundaries = [i for i, line in enumerate(lines) if _DUMP_BOUNDARY_RE.match(line)]
    if len(boundaries) >= dump_count:
        return ''.join(lines[boundaries[-dump_count]:])
    if boundaries:
        return ''.join(lines[boundaries[0]:])
    return ''.join(lines[-fallback_lines:])


def _git_head(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_root), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return f'<git rev-parse failed: {result.stderr.strip()}>'
    except Exception as exc:
        return f'<git unavailable: {exc}>'


def bundle_incident(samsara_home: Path, repo_root: Path = ROOT) -> Path:
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    incident_dir = samsara_home / 'incidents' / f'incident_{timestamp}'
    incident_dir.mkdir(parents=True, exist_ok=True)

    flight_events = _tail_flight_events(samsara_home / 'flight', _FLIGHT_EVENT_LIMIT)
    (incident_dir / 'flight_events.jsonl').write_text(''.join(flight_events), encoding='utf-8')

    log_lines = _tail_lines(samsara_home / 'logs' / 'samsara.log', _LOG_LINE_LIMIT)
    (incident_dir / 'samsara_log_tail.txt').write_text(''.join(log_lines), encoding='utf-8')

    freeze_stacks = _tail_freeze_dumps(
        samsara_home / 'freeze_stacks.txt', _FREEZE_DUMP_COUNT, _FREEZE_FALLBACK_LINES,
    )
    if freeze_stacks:
        (incident_dir / 'freeze_stacks_tail.txt').write_text(freeze_stacks, encoding='utf-8')

    for name in ('probe.log', 'guard.log'):
        source = samsara_home / name
        if source.exists():
            shutil.copy2(source, incident_dir / name)

    config_source = samsara_home / 'config.json'
    if config_source.exists():
        shutil.copy2(config_source, incident_dir / 'config.json')

    (incident_dir / 'git_head.txt').write_text(_git_head(repo_root) + '\n', encoding='utf-8')

    return incident_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Bundle flight-recorder + log evidence for one incident.',
    )
    parser.add_argument(
        'samsara_home', nargs='?', default=None,
        help='Samsara home dir (default: resolved via SAMSARA_HOME_DIR / ~/.samsara)',
    )
    args = parser.parse_args()
    samsara_home = Path(args.samsara_home) if args.samsara_home else samsara_home_dir()
    incident_dir = bundle_incident(samsara_home)
    print(str(incident_dir))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
