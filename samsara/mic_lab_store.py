"""Local, lossless Mic Lab runs. No profile/config reads or hardware on import."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

import numpy as np
from scipy.io import wavfile


def json_value(value):
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


def _json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(json_value(value), indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def storage_root(profile, config=None):
    """Private configuration: {"mic_lab": {"output_dir": "D:/SamsaraCorpus/mic_lab"}}.

    Explicit configuration wins. Public callers pass their resolved profile;
    this module never inspects the owner's live profile to determine a build.
    """
    selected = (config or {}).get('mic_lab', {}).get('output_dir')
    return Path(selected).expanduser() if selected else Path(profile) / 'mic_lab'


def _component(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value):
        raise ValueError('Invalid run/take identifier')
    return value


def _inside(root, relative):
    path = root / relative
    attributes = getattr(path.lstat(), 'st_file_attributes', 0) if path.exists() else 0
    if path.is_symlink() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
        raise ValueError('Links are not allowed in a Mic Lab run')
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise ValueError('Path escapes the Mic Lab run')
    return path


def _write_wav(path, rate, audio):
    pcm = np.array(audio, dtype=np.float32, copy=True)
    if pcm.ndim not in (1, 2) or not len(pcm) or not np.isfinite(pcm).all():
        raise ValueError('Expected nonempty finite mono/stereo PCM')
    if pcm.ndim == 2 and pcm.shape[1] not in (1, 2):
        raise ValueError('Expected at most two channels')
    if not isinstance(rate, int) or rate <= 0:
        raise ValueError('Invalid sample rate')
    # IEEE float32 WAV preserves every decoder sample, including overshoots.
    wavfile.write(path, rate, pcm)


def _read_wav(path):
    rate, pcm = wavfile.read(path)
    if pcm.dtype != np.float32 or pcm.ndim not in (1, 2) or not len(pcm) or not np.isfinite(pcm).all():
        raise ValueError('Mic Lab WAV must contain finite float32 PCM')
    return int(rate), pcm.copy()


class MicLabStore:
    """One background writer; callers may wait on futures off capture/decode.

    A partial run has run.json and the completed takes even if interrupted.
    report.json is written only when finish() is called. Deletion waits behind
    outstanding writes, validates the exact run root, then removes that run.
    """
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mic-lab-store')

    def start_run(self, device_pin, *, run_id=None, metadata=None):
        run_id = _component(run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
        folder = _inside(self.root, run_id)
        folder.mkdir(parents=True, exist_ok=False)
        try:
            _json(folder / 'device_pin.json', device_pin)
            _json(folder / 'run.json', dict(schema=1, run_id=run_id,
                created_at=datetime.now(timezone.utc).isoformat(), metadata=metadata or {}))
        except Exception:
            # Preserve partial evidence rather than deleting an unknown state.
            raise
        return run_id

    def save_take(self, run_id, take_id, take):
        """Copy at submission so later user edits cannot race persistence."""
        import copy
        frozen = copy.deepcopy(take)
        return self._writer.submit(self._save_take, _component(run_id), _component(take_id), frozen)

    def _save_take(self, run_id, take_id, take):
        run = _inside(self.root, run_id)
        if not (run / 'run.json').is_file():
            raise ValueError('Unknown Mic Lab run')
        folder = _inside(run, take_id)
        folder.mkdir(exist_ok=False)
        rate = int(take.get('sample_rate', 16000))
        _write_wav(folder / 'raw.wav', rate, take['raw'])
        (folder / 'decoded_slices').mkdir()
        slices = []
        for index, item in enumerate(take.get('slices', [])):
            name = f'decoded_slices/{index:03d}.wav'
            _write_wav(folder / name, rate, item['audio'])
            slices.append({k: v for k, v in item.items() if k != 'audio'} | dict(wav=name))
        extra = {}
        for key in ('native_idle', 'native_load'):
            if key in take:
                probe = take[key]
                _write_wav(folder / (key + '.wav'), int(probe['sample_rate']), probe['audio'])
                extra[key] = {k: v for k, v in probe.items() if k != 'audio'} | dict(wav=key + '.wav')
        metadata = {k: v for k, v in take.items() if k not in ('raw', 'slices', 'native_idle', 'native_load')}
        metadata.update(schema=1, take_id=take_id, raw_wav='raw.wav', slices=slices, **extra)
        _json(folder / 'take.json', metadata)  # Publication marker comes last.
        return folder

    def finish(self, run_id, report):
        return self._writer.submit(_json, _inside(self.root, _component(run_id)) / 'report.json', report)

    def delete_run(self, run_id):
        return self._writer.submit(self._delete_run, _component(run_id))

    def _delete_run(self, run_id):
        path = _inside(self.root, run_id)
        if not (path / 'run.json').is_file():
            raise ValueError('Not a Mic Lab run')
        # Reject links anywhere, including parent components of stored takes.
        for child in path.rglob('*'):
            _inside(path, child.relative_to(path))
        shutil.rmtree(path)

    def close(self):
        self._writer.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def load_run(folder):
    """Read only; re-judging never overwrites saved live text or reference text."""
    folder = Path(folder).resolve()
    run = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
    if run.get('schema') != 1:
        raise ValueError('Unsupported Mic Lab run schema')
    run['device_pin'] = json.loads((folder / 'device_pin.json').read_text(encoding='utf-8'))
    takes = []
    for sidecar in sorted(folder.glob('*/take.json')):
        _inside(folder, sidecar.relative_to(folder))
        data = json.loads(sidecar.read_text(encoding='utf-8'))
        if data.get('schema') != 1:
            raise ValueError('Unsupported take schema')
        rate, raw = _read_wav(_inside(sidecar.parent, data['raw_wav']))
        if rate != data['sample_rate']:
            raise ValueError('Raw sample rate disagrees with take metadata')
        data['raw'] = raw
        for item in data['slices']:
            sr, item['audio'] = _read_wav(_inside(sidecar.parent, item['wav']))
            if sr != rate:
                raise ValueError('Decoder slice sample rate mismatch')
        for key in ('native_idle', 'native_load'):
            if key in data:
                sr, data[key]['audio'] = _read_wav(_inside(sidecar.parent, data[key]['wav']))
                if sr != data[key]['sample_rate']:
                    raise ValueError('Native probe sample rate mismatch')
        takes.append(data)
    run['takes'] = takes
    return run
