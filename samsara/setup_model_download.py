"""Prepare first-run speech files while setup is open, without booting audio."""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
import time

from samsara.speech_model_transfer import Progress, fetch_model

from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)


@dataclass
class Download:
    phase: str = 'checking'
    done: threading.Event = field(default_factory=threading.Event)
    progress: Progress = field(default_factory=Progress)
    path: str | None = None
    listeners: list = field(default_factory=list)
    published: float = 0


class SetupModelDownload:
    """One transfer per model; workers never retain or mutate Qt widgets."""

    def __init__(self, *, downloader=None, spawn=None):
        self.jobs = {}
        self._lock = threading.RLock()
        self._downloader = downloader
        self._spawn = spawn or thread_registry.spawn

    def request(self, model_name, *, retry=False):
        with self._lock:
            if model_name in self.jobs:
                previous = self.jobs[model_name]
                if not retry or previous.phase != 'failed' or not previous.done.is_set():
                    return previous
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
                downloader = lambda name, **kw: fetch_model(
                    name, progress=lambda value: self._progress(job, value), **kw)
            try:
                job.path = downloader(model_name, local_files_only=True)
            except Exception:
                job.phase = 'downloading'
                self._notify(job)
                job.path = downloader(model_name)
            job.phase = 'downloaded'
        except Exception:
            job.phase = 'failed'
            logger.warning('Setup speech download failed; startup can retry', exc_info=True)
        finally:
            self._notify(job)
            job.done.set()

    def _progress(self, job, value):
        with self._lock:
            job.progress = value
            now = time.monotonic()
            if now - job.published < .2 and value.downloaded != value.total:
                return
            job.published = now
        self._notify(job)

    def _notify(self, job):
        with self._lock:
            listeners = tuple(job.listeners)
        for listener in listeners:
            try:
                listener(job)
            except Exception:
                logger.debug('Speech progress observer failed', exc_info=True)

    def subscribe(self, model_name, listener):
        with self._lock:
            job = self.jobs[model_name]
            job.listeners.append(listener)
        listener(job)
        def unsubscribe():
            with self._lock:
                job.listeners.remove(listener)
        return unsubscribe

    def details(self, model_name):
        with self._lock:
            job = self.jobs.get(model_name)
            return (job.phase, job.progress) if job is not None else ('checking', Progress())

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
        if model_name in getattr(downloads, 'jobs', {}):
            unsubscribe = downloads.subscribe(model_name, lambda job: publish_to_app(app, model_name, job))
            try:
                downloads.wait(model_name)
            finally:
                unsubscribe()
        else:
            downloads.wait(model_name)


def publish_to_app(app, model_name, job):
    """Worker-safe publication; the display and splash marshal their own UI calls."""
    if app is None:
        return
    from samsara import speech_readiness
    app._speech_model_progress = job.progress
    app._speech_model_phase = {'downloaded': 'loading', 'checking': 'downloading'}.get(job.phase, job.phase)
    message = speech_readiness.status(app)
    update = getattr(app, 'update_splash', None)
    if callable(update):
        # The splash paints single lines: keep byte details on its support line.
        detail = byte_text(job.progress) if job.progress.total else progress_text(model_name, job.phase)
        update('Loading the speech model — downloading' if job.phase in ('checking', 'downloading')
               else message, 68, detail)
    speech_readiness.show(app)


def resolve_for_startup(app, model_name):
    """Join setup's transfer, or start the same resumable path for existing profiles."""
    downloads = getattr(app, '_setup_model_download', None)
    if downloads is None:
        downloads = SetupModelDownload()
        if app is not None:
            app._setup_model_download = downloads
    job = downloads.request(model_name, retry=True)
    unsubscribe = downloads.subscribe(model_name, lambda value: publish_to_app(app, model_name, value))
    try:
        job.done.wait()
        if job.phase == 'failed':
            raise OSError('Speech model download paused. Check your connection, then choose Retry.')
        return job.path
    finally:
        unsubscribe()


def retry_startup(app):
    """Guard repeated clicks before the startup worker has set loading_model."""
    if (getattr(app, 'model_loaded', False) or getattr(app, 'loading_model', False)
            or getattr(app, '_speech_model_phase', '') != 'failed'):
        return
    app.loading_model = True
    app._speech_model_phase = 'loading'
    app.load_model_async()


def keep_download_retry_available(app):
    """Do not quit the shell for a resumable speech transfer failure."""
    downloads = getattr(app, '_setup_model_download', None)
    model_name = (getattr(app, 'config', {}) or {}).get('model_size')
    if (getattr(app, '_speech_model_phase', '') != 'failed' or downloads is None
            or downloads.snapshot(model_name) != 'failed'):
        return False
    app._close_splash_post_load()
    app.show_main_window()
    return True


# PLACEHOLDER COPY (935i): phases, never an estimate or a model-loaded claim.
def progress_text(model_name, phase):
    value = Progress()
    if isinstance(phase, tuple):
        phase, value = phase
    if phase == 'downloading' and value.total:
        return f'Downloading speech model ({model_name}) — {byte_text(value)}'
    return {
        'checking': f'Checking speech model ({model_name})…',
        'downloading': f'Downloading speech model ({model_name}) in the background…',
        'downloaded': f'Speech model ({model_name}) downloaded. It loads when Samsara starts.',
        'failed': 'Speech model download paused. Check your connection, then choose Retry; Samsara can also retry after setup.',
    }[phase]


def byte_text(value):
    # PLACEHOLDER COPY (1007): decimal MB, deliberately approximate time remaining.
    percent = min(100, int(100 * value.downloaded / value.total))
    text = f'{value.downloaded / 1_000_000:.1f} / {value.total / 1_000_000:.1f} MB ({percent}%)'
    if value.eta is not None and value.downloaded < value.total:
        seconds = max(1, round(value.eta))
        text += f' · about {seconds}s left' if seconds < 60 else f' · about {(seconds + 59) // 60} min left'
    return text
