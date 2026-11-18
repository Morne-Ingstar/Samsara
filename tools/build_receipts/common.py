"""Receipt bookkeeping and isolation checks. No product or desktop imports."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time

TICKETS = ('T10', 'T29', 'T67', 'T68', 'T70', 'T73', 'T80', 'T81', 'T87')
ROOT = Path('D:/samsara_tmp1241')
# Set only by the unattended entry point; reserve shutdown time outside this.
DEADLINE = None
# Latest relevant landing named by the 1241 acceptance brief, not HEAD's date.
LANDINGS = {
    'T10': ('3b8435e3bca95325bd952ec1aa0fff41b03de3a3', '2026-11-01T03:06:22-08:00'),
    'T29': ('f03a1be162cc5bfe863ef8c2c4b96e14733f6c00', '2026-11-05T11:28:12-08:00'),
    'T67': ('f9b0263c14364f4c885c6445a1b59fe9fd6a9e48', '2026-11-06T03:59:00-08:00'),
    'T68': ('e296bb4e657c227217d923aa07c035054bd194a1', '2026-11-09T06:16:51-08:00'),
    'T70': ('e6ca0f58c1fce355c0d1e18b7407ac1e2e59a9ce', '2026-11-05T02:35:57-08:00'),
    'T73': ('62b916951ea4891a865b3dfa9a32e225a2999442', '2026-11-03T18:25:51-08:00'),
    'T80': ('f8b27dde4fd076dc83b554f6e153ff3dcd675932', '2026-11-04T03:21:03-08:00'),
    'T81': ('83ba56ce67b2965a7c2d7363a53b03d4c0e13c69', '2026-11-06T07:41:55-08:00'),
    'T87': ('ffc01b496e4050d50c8bc0f1d2b97a951e49bbf2', '2026-11-07T11:09:46-08:00'),
}


def reject_links(path):
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and
                                 getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Isolation path contains a link or reparse point')


def validate_paths(app, home, *, root=ROOT):
    """Require the explicitly named scratch tree, with no owner-profile fallback."""
    app, home, root = (Path(p).absolute() for p in (app, home, root))
    for path in (app, home, root):
        reject_links(path)
    if app.resolve() != (root / 'app').resolve() or home.resolve() != (root / 'home').resolve():
        raise ValueError('Use D:/samsara_tmp1241/app and D:/samsara_tmp1241/home')
    if not (app / 'Samsara.exe').is_file():
        raise ValueError('Copy the installed Daily build into the scratch app directory first')
    if home.exists() and any(home.iterdir()):
        raise ValueError('Home must be empty; retain the previous run and use a fresh scratch tree')
    return app, home


def build_identity(app):
    """Timestamp comes from build metadata, never from copy/file modification time."""
    path = Path(app) / 'BUILD_INFO.txt'
    raw = path.read_text(encoding='utf-8-sig')
    # Daily's build script writes key=value; accept older colon/JSON envelopes too.
    try:
        fields = json.loads(raw)
    except ValueError:
        fields = dict(re.findall(r'^\s*(commit|date|built|build_time|build_ts|built_at|build_timestamp|timestamp)'
                                 r'(?:\s*[:=]\s*|\s+)(.*?)\s*$', raw, re.M | re.I))
    fields = {k.lower().replace(' ', '_').replace('-', '_'): v for k, v in fields.items()}
    stamp = next((fields[k] for k in ('build_time', 'build_ts', 'built_at', 'build_timestamp',
                                      'built', 'timestamp') if fields.get(k)), None)
    if not isinstance(stamp, str):
        raise ValueError('BUILD_INFO has no build timestamp')
    # The installed Daily format is 'built YYYY-MM-DD HH:MM:SS from ...'.
    stamp = stamp.split(' from ', 1)[0]
    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    timezone_source = 'build timestamp'
    if parsed.tzinfo is None:
        # Legacy Daily metadata records local build time but includes the builder's
        # offset in 'date'. Retain the inference explicitly; never use copy mtime.
        dated = datetime.fromisoformat(fields.get('date', ''))
        if dated.tzinfo is None or dated.date() != parsed.date():
            raise ValueError('Build timezone unavailable; cannot fabricate a receipt')
        parsed = parsed.replace(tzinfo=dated.tzinfo)
        timezone_source = 'inferred from same-day date offset in BUILD_INFO; runner must confirm'
    return {'build_ts': parsed.isoformat(), 'commit': fields.get('commit', ''),
            'timezone_source': timezone_source,
            'metadata_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'executable_sha256': file_hash(Path(app) / 'Samsara.exe')}


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + '.tmp')
    pending.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    pending.replace(path)


def wait_for(probe, *, timeout=30, interval=.1):
    end = time.monotonic() + timeout
    if DEADLINE is not None:
        end = min(end, DEADLINE)
    while True:
        if time.monotonic() >= end:
            raise TimeoutError('Receipt observation budget exhausted')
        value = probe()
        if value:
            return value
        if time.monotonic() >= end:
            raise TimeoutError('Expected observation did not arrive')
        time.sleep(interval)


def receipt(ticket, identity, trigger, evidence, observations, *, limitation='', eligible=False):
    if ticket not in TICKETS or not trigger or not observations:
        raise ValueError('A receipt needs a known ticket, exact trigger and observations')
    # Observation values must be actual booleans. Truthy strings must not create passes.
    commit, landed_at = LANDINGS[ticket]
    try:
        built = datetime.fromisoformat(identity['build_ts'])
        newer = built.tzinfo is not None and built > datetime.fromisoformat(landed_at)
    except (ValueError, TypeError):
        newer = False
    observations = dict(observations, build_newer_than_landing=newer)
    passed = all(value is True for value in observations.values())
    return dict(ticket=ticket, build_ts=identity['build_ts'], trigger=trigger,
                result='PASS' if passed else 'FAIL', evidence=str(Path(evidence).absolute()),
                observations=observations, limitation=limitation,
                acceptance_eligible=passed and eligible and identity.get('timezone_source') == 'build timestamp',
                landed_commit=commit, landed_commit_ts=landed_at,
                build=identity)
