"""Speech files through the Hub cache, with received-byte progress (1007)."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import logging
from pathlib import Path
import threading
import time


@dataclass(frozen=True)
class Progress:
    downloaded: int = 0
    total: int = 0
    eta: float | None = None


class _HubAdvice(logging.Filter):
    def filter(self, record):
        message = record.getMessage().removeprefix('Warning: ')
        if (message.startswith('You are sending unauthenticated requests to the HF Hub')
                or (message.startswith('Xet Storage is enabled')
                    and "'hf_xet' package is not installed" in message)):
            record.levelno, record.levelname = logging.DEBUG, 'DEBUG'
        return True


_advice = _HubAdvice()


def quiet_hub_advice():
    # Logger filters run before handlers; do not lower unrelated Hub warnings.
    for name in ('huggingface_hub.file_download', 'huggingface_hub.utils._http',
                 'huggingface_hub.utils._headers'):
        logging.getLogger(name).addFilter(_advice)


def fetch_model(model_name, *, local_files_only=False, progress=None):
    """Use the same files/cache as faster-whisper; never inspect blob lengths.

    A metadata pass fixes the denominator before any transfer starts. Passing
    tqdm_class to each file works with Hub versions that only count files in
    snapshot_download. Hub owns locking, partial files and HTTP Range resumes.
    """
    quiet_hub_advice()
    from faster_whisper.utils import _MODELS, download_model
    if local_files_only:
        path = download_model(model_name, local_files_only=True)
        # Hub's local snapshot lookup alone also accepts an unfinished snapshot.
        if not all((Path(path) / name).is_file()
                   for name in ('model.bin', 'config.json', 'tokenizer.json')):
            raise FileNotFoundError('Speech model snapshot is incomplete')
        return path

    from huggingface_hub import HfApi, hf_hub_download
    from tqdm.auto import tqdm
    repo_id = model_name if '/' in model_name else _MODELS[model_name]
    info = HfApi().model_info(repo_id, files_metadata=True)
    patterns = ('config.json', 'preprocessor_config.json', 'model.bin',
                'tokenizer.json', 'vocabulary.*')
    files = [f for f in info.siblings if any(fnmatch(f.rfilename, p) for p in patterns)]
    if not {'model.bin', 'config.json', 'tokenizer.json'}.issubset(f.rfilename for f in files):
        raise FileNotFoundError('Speech model repository is incomplete')
    sizes = {f.rfilename: int(f.size or 0) for f in files}
    total = sum(sizes.values())
    counts, cached = {}, {}
    # Credit cached files before publishing, so percentage never drops.
    for name in sizes:
        try:
            cached[name] = hf_hub_download(repo_id, name, revision=info.sha, local_files_only=True)
            counts[name] = sizes[name]
        except Exception:
            counts[name] = 0
    lock = threading.Lock()
    started, received = time.monotonic(), 0

    def publish(name=None, count=0, *, initial=False):
        nonlocal received
        with lock:
            if name is not None:
                count = min(sizes[name], max(0, int(count)))
                delta = max(0, count - counts[name])
                counts[name] = max(counts[name], count)
                if not initial:
                    received += delta
            downloaded = sum(counts.values())
            elapsed = time.monotonic() - started
            eta = (total - downloaded) * elapsed / received if received and elapsed >= .5 else None
            if progress:
                progress(Progress(downloaded, total, eta))

    publish()
    result = None
    for name in sizes:
        if name in cached:
            result = cached[name]
            continue

        class Bytes(tqdm):
            def __init__(self, *args, **kwargs):
                kwargs.pop('name', None)
                initial = kwargs.get('initial', 0)
                super().__init__(*args, **dict(kwargs, disable=True))
                self.n = initial
                publish(name, self.n, initial=True)

            def update(self, n=1):
                self.n += n or 0
                publish(name, self.n)

        result = hf_hub_download(repo_id, name, revision=info.sha, tqdm_class=Bytes)
        publish(name, sizes[name], initial=True)
    return str(Path(result).parent)


def downloaded_whisper_model(factory, args, kwargs, on_download):
    """Use the completed local snapshot; plain factory callbacks stay compatible."""
    resolve = getattr(on_download, 'resolve', None)
    if not callable(resolve):
        return factory(*args, **kwargs)
    path = resolve()
    options = dict(kwargs, local_files_only=True)
    if args:
        return factory(path, *args[1:], **options)
    options['model_size_or_path'] = path
    return factory(**options)
