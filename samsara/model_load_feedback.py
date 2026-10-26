"""Keep startup splash feedback tied to the blocking speech-model load."""
from __future__ import annotations

import time
from samsara import speech_readiness
from typing import Callable


LOADING_STATUS = "Loading the speech model - first start can take a minute"  # PLACEHOLDER COPY (893)


def is_model_loading_status(status: str) -> bool:
    return str(status).startswith("Loading the speech model")


def load_model_with_feedback(
    update_splash: Callable,
    model_factory: Callable,
    *args,
    ready_detail: Callable[[float], str],
    **kwargs,
):
    """Show loading before construction, then replace it with ready feedback."""
    update_splash(LOADING_STATUS, 60, "Preparing the interface while speech recognition loads")
    speech_readiness.phase(update_splash, 'loading')
    started = time.perf_counter()
    try:
        model = model_factory(*args, **kwargs)
    except Exception:
        speech_readiness.phase(update_splash, 'failed')
        raise
    elapsed = time.perf_counter() - started
    update_splash("Speech model ready...", 70, ready_detail(elapsed))
    speech_readiness.phase(update_splash, 'ready')
    return model, elapsed


def load_speech_model(update_splash, model_factory, model_name, device, compute_type):
    """Apply production CPU/GPU settings and the splash state around construction."""
    def downloading():
        speech_readiness.phase(update_splash, 'downloading')
        update_splash(LOADING_STATUS, 68, "Downloading the speech model; a network connection is needed")

    return load_model_with_feedback(
        update_splash,
        model_factory,
        model_name,
        device=device,
        compute_type=compute_type,
        cpu_threads=4,
        num_workers=2,
        ready_detail=lambda elapsed: f"Loaded on {device.upper()} in {elapsed:.1f} seconds",
        _on_download=downloading,
    )
