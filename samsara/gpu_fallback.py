"""One session-local CPU recovery for the shared speech model (917).

Saved speech choices are untouched. An internal lock serializes recovery;
callers already hold the app's non-reentrant model_lock. Lazy segments are
consumed before text escapes, so a CPU retry never duplicates partial output.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# PLACEHOLDER COPY (917)
FALLBACK_NOTICE = (
    "Graphics acceleration did not work, so Samsara switched to the processor. "
    "Dictation still works, a little slower."
)


def is_gpu_runtime_error(error: Exception) -> bool:
    if not isinstance(error, (RuntimeError, OSError)):
        return False
    message = str(error).casefold()
    gpu = any(token in message for token in ("cuda", "cublas", "cudnn", "cudart"))
    failure = any(token in message for token in (
        "not found", "cannot be loaded", "could not load", "failed to load",
        "could not locate", "cannot load", "dll load failed", "cuda failed",
        "cuda error", "cublas_status_", "cudnn_status_",
    ))
    return gpu and failure


class ResilientSpeechModel:
    def __init__(self, app, model, cpu_factory, *, device, notify=None, recovered=False):
        self._app = app
        self._model = model
        self._cpu_factory = cpu_factory
        self._device = device
        self._lock = threading.RLock()
        self._fallback_attempted = recovered
        self._notice_pending = recovered
        self._load_error = None
        self._notify = notify or self._post_notice

    def __getattr__(self, name):
        return getattr(self._model, name)

    def _post_notice(self):
        from samsara.ui import qt_runtime
        if not qt_runtime.is_alive():
            return False
        from samsara.ui.speech_hardware_qt import show_fallback_notice
        qt_runtime.post(show_fallback_notice)
        return True

    def _report_recovery(self):
        if self._notice_pending:
            try:
                if self._notify() is not False:
                    self._notice_pending = False
            except Exception:
                logger.warning('[GPU-FALLBACK] Notice delivery deferred')

    def _run(self, operation):
        with self._lock:
            if self._load_error is not None:
                raise RuntimeError('CPU speech model could not load') from self._load_error
            try:
                result = operation(self._model)
            except Exception as error:
                if (self._device != 'cuda' or self._fallback_attempted
                        or not is_gpu_runtime_error(error)):
                    raise
                self._fallback_attempted = True
                logger.warning('[GPU-FALLBACK] CUDA runtime failed; reloading CPU int8')
                self._model = None
                try:
                    self._model = self._cpu_factory()
                except Exception as load_error:
                    self._load_error = load_error
                    raise
                self._device = self._app.device_type = 'cpu'
                self._app.compute_type = 'int8'
                self._notice_pending = True
                result = operation(self._model)
            self._report_recovery()
            return result

    def transcribe(self, *args, **kwargs):
        def decode(model):
            segments, info = model.transcribe(*args, **kwargs)
            return iter(list(segments)), info
        return self._run(decode)

    def encode(self, *args, **kwargs):
        return self._run(lambda model: model.encode(*args, **kwargs))


def load_resilient_model(app, update_splash, model_factory, model_name, device, compute_type):
    from samsara.cuda_detect import prepare_cuda_dll_search
    from samsara.model_load_feedback import load_speech_model
    prepare_cuda_dll_search()
    recovered = False
    try:
        model, elapsed = load_speech_model(
            update_splash, model_factory, model_name, device, compute_type)
    except Exception as error:
        if device != 'cuda' or not is_gpu_runtime_error(error):
            raise
        logger.warning('[GPU-FALLBACK] CUDA model load failed; reloading CPU int8')
        device, compute_type, recovered = 'cpu', 'int8', True
        model, elapsed = load_speech_model(
            update_splash, model_factory, model_name, device, compute_type)
    wrapper = ResilientSpeechModel(
        app, model, lambda: model_factory(model_name, device='cpu', compute_type='int8',
                                        cpu_threads=4, num_workers=2, local_files_only=True),
        device=device, recovered=recovered,
    )
    return wrapper, elapsed, device, compute_type
