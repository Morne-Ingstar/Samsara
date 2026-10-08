"""One speech runtime lifecycle for startup, Settings and the feedback setter."""
from contextlib import contextmanager, nullcontext
import logging

from .coordinator import AudioCoordinator

logger = logging.getLogger(__name__)


@contextmanager
def tts_runtime_lock(app):
    """Serialize config/runtime changes: dispatch lock, then the app config lock.

    The config lock is non-reentrant. Callers hold this context once, including
    their config write/save, and call rebuild_tts_runtime without locking again.
    Retry if another caller replaced the router while we waited for its lock.
    """
    while True:
        old = getattr(app, 'audio_coordinator', None)
        dispatch_lock = getattr(old, '_dispatch_lock', None)
        with dispatch_lock or nullcontext():
            with app._config_lock:
                if old is not getattr(app, 'audio_coordinator', None):
                    continue
                yield
                return


def rebuild_tts_runtime(app, requested: dict) -> None:
    """Replace the engine, retaining a router even when disabled or unavailable.

    Caller must hold tts_runtime_lock(app). Retire the old engine completely
    before constructing or exposing its replacement. A retained old router
    loses its engine too, so re-enabling the same provider cannot revive it.
    """
    old = getattr(app, 'audio_coordinator', None)
    old_engine = getattr(app, 'tts_engine', None)
    if old is not None:
        old.shutdown()
        old.engine = None
    elif old_engine is not None:
        old_engine.cancel_all()
    app.tts_engine = None
    # Install the caption/policy path before constructing any engine. Direct
    # engine fallbacks never see an engine published without a router.
    app.audio_coordinator = AudioCoordinator(
        app, None, config=app.config.get('audio_coordinator', {}) or {})
    if old_engine is not None:
        shutdown = getattr(old_engine, 'shutdown', None)
        if callable(shutdown):
            shutdown()
    if not requested.get('enabled', False):
        return

    engine = None
    try:
        from samsara.tts import EdgeTTSEngine, WinRTEngine
        provider = str(requested.get('engine', 'winrt')).lower()
        if provider not in ('winrt', 'edge'):
            raise ValueError(f'Unsupported TTS provider: {provider}')
        factory = EdgeTTSEngine if provider == 'edge' else WinRTEngine
        engine = factory(output_device_resolver=getattr(app, '_resolve_playback_output', None))
        coordinator = AudioCoordinator(
            app, engine, config=app.config.get('audio_coordinator', {}) or {})
        app.tts_engine = engine
        app.audio_coordinator = coordinator
    except Exception:
        if engine is not None:
            try:
                engine.shutdown()
            except Exception:
                pass
        logger.exception('[TTS] Could not start selected engine; caption router remains available')
