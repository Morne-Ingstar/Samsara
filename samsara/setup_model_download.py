"""Prepare first-run speech files while setup is open, without booting audio."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading

from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)


@dataclass
class Download:
    phase: str = 'checking'
    done: threading.Event = field(default_factory=threading.Event)


class SetupModelDownload:
    """One transfer per model; workers never retain or mutate Qt widgets."""

    def __init__(self, *, downloader=None, spawn=None):
        self.jobs = {}
        self._lock = threading.Lock()
        self._downloader = downloader
        self._spawn = spawn or thread_registry.spawn

    def request(self, model_name):
        with self._lock:
            if model_name in self.jobs:
                return self.jobs[model_name]
            job = self.jobs[model_name] = Download()
        try:
            self._spawn('setup.speech-download', self._download,
                        args=(model_name, job), daemon=True)
        except Exception:
            job.phase = 'failed'
            job.done.set()
            logger.warning('Could not start setup speech download', exc_info=True)
        return job

    def _download(self, model_name, job):
        try:
            downloader = self._downloader
            if downloader is None:
                from faster_whisper.utils import download_model
                downloader = download_model
            try:
                downloader(model_name, local_files_only=True)
            except Exception:
                job.phase = 'downloading'
                downloader(model_name)
            job.phase = 'downloaded'
        except Exception:
            job.phase = 'failed'
            logger.warning('Setup speech download failed; startup can retry', exc_info=True)
        finally:
            job.done.set()

    def snapshot(self, model_name):
        with self._lock:
            job = self.jobs.get(model_name)
            return job.phase if job is not None else 'checking'

    def wait(self, model_name):
        with self._lock:
            job = self.jobs.get(model_name)
        if job is not None:
            job.done.wait()


def wait_for_setup_download(app, model_name):
    """Reuse the selected transfer before startup's normal cache/load path.

    A failed prefetch is deliberately not raised: the normal loader retains its
    existing retry, cache-repair and CPU/GPU recovery behavior.
    """
    downloads = getattr(app, '_setup_model_download', None)
    if downloads is not None:
        if downloads.snapshot(model_name) in ('checking', 'downloading'):
            app._speech_model_phase = 'downloading'
        downloads.wait(model_name)


# PLACEHOLDER COPY (935i): phases, never an estimate or a model-loaded claim.
def progress_text(model_name, phase):
    return {
        'checking': f'Checking speech model ({model_name})…',
        'downloading': f'Downloading speech model ({model_name}) in the background…',
        'downloaded': f'Speech model ({model_name}) downloaded. It loads when Samsara starts.',
        'failed': 'Speech model download paused. Check your connection; Samsara will retry after setup.',
    }[phase]
