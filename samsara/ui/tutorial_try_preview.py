"""Disposable tutorial speech, tracked by the live router without replacing it."""
from contextlib import nullcontext

from samsara.tts.coordinator import IDLE, SPEAKING


def make_engine(app, provider):
    from samsara.tts import EdgeTTSEngine, WinRTEngine
    factory = EdgeTTSEngine if provider == 'edge' else WinRTEngine
    return factory(output_device_resolver=getattr(app, '_resolve_playback_output', None))


class SampleSpeech:
    """Own an engine, but use the existing router for capture/echo awareness.

    The marker protects newer application speech from this sample's completion.
    Local and idle Network engines are reused; an active cancelled Network
    engine is disposed before replay because its cancellation flag is shared.
    Disposal never runs inside an engine's on_done worker or the capture lock.
    """
    def __init__(self, page, provider):
        self.page, self.provider = page, provider
        self.router = getattr(page.app, 'audio_coordinator', None)
        if self.router is None:
            raise RuntimeError('No capture-aware speech router')
        self.engine = make_engine(page.app, provider)
        self.marker = object()
        self.active, self.closed = False, False
        checker = getattr(self.engine, 'set_authorization_checker', None)
        if callable(checker):
            try:
                checker(self.authorized)
            except Exception:
                self.closed = True
                self.engine.shutdown()
                raise

    def authorized(self):
        return (not self.closed and self.active
                and self.router is getattr(self.page.app, 'audio_coordinator', None)
                and getattr(self.router, '_tutorial_sample_marker', None) is self.marker
                and not getattr(self.router, '_speech_pending', 0)
                and not self.page.busy(include_speech=False)
                and (self.provider != 'edge' or self.page.network_allowed()))

    @property
    def is_speaking(self):
        return self.active

    def speak(self, text, cfg):
        with getattr(self.router, '_dispatch_lock', None) or nullcontext():
            if self.page.busy() or self.closed:
                return None
            self.active = True
            self.marker = marker = object()
            self.router._tutorial_sample_marker = marker
            self.router.transition_to(SPEAKING, context={'reason': 'tutorial_sample'})
            try:
                return self.engine.speak(text, voice_id=cfg.get('voice_id'), speed=cfg.get('speed', 1),
                                         volume=cfg.get('volume', .8), category='readback', on_done=lambda: self.done(marker))
            except Exception:
                self.done(marker)
                raise

    def done(self, marker=None):
        marker = self.marker if marker is None else marker
        with getattr(self.router, '_dispatch_lock', None) or nullcontext():
            if marker is not self.marker:
                return  # A callback from a cancelled trial cannot finish its replacement.
            self.active = False
            if getattr(self.router, '_tutorial_sample_marker', None) is marker:
                self.router._tutorial_sample_marker = None
                if not getattr(self.router, '_speech_pending', 0):
                    self.router.transition_to(IDLE, context={'reason': 'tutorial_sample_complete'})

    def cancel_speech(self):
        if self.closed:
            return
        self.active = False  # Revoke network authorization before cancelling.
        try:
            self.engine.cancel_all()
        finally:
            self.done()

    def close(self):
        if self.closed:
            return
        try:
            if self.active:
                self.cancel_speech()
        finally:
            self.closed = True
            self.engine.shutdown()
