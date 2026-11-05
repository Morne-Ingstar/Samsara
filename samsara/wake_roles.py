"""1042b: per-utterance Ava capture and direct conversation dispatch.

The audio poller never waits for inference or a provider. Local partials use
the existing model lock opportunistically; final decoding remains in the
ordered wake FIFO. No partial can execute an action or send a provider request.
"""
import threading
import time

import numpy as np

from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)


class AdmittedWakeBuffer(list):
    """Audio and its OWW hits travel together through the asynchronous decode FIFO."""

    def __init__(self, frames, *, primary_hit=False, profile_hits=(),
                 primary_phrase=None, primary_role=None):
        super().__init__(frames)
        self.primary_hit = bool(primary_hit)
        self.profile_hits = frozenset(profile_hits)
        self.primary_phrase = primary_phrase
        self.primary_role = primary_role


def clear_oww_hits(app):
    clear_capture(app)
    app._oww_wake_detected = False
    app._oww_primary_hit = False
    app._oww_profile_hits = set()
    app._oww_primary_hit_ns = None
    app._oww_profile_hit_ns = {}


def phrase_role(wake, phrase):
    for row in wake.get('phrases') or ():
        if row.get('enabled', True) and row.get('phrase') == phrase:
            return 'ava' if row.get('role') == 'ava' else 'samsara'
    return 'samsara'


class AvaWakeStream:
    """One bounded local partial worker for the currently captured utterance."""

    def __init__(self, app, frames):
        self.app = app
        self.frames = frames
        self.stopped = threading.Event()
        self.partial_text = ''

    def start(self):
        thread_registry.spawn('ava-wake-partial', self._run, daemon=True)

    def close(self):
        self.stopped.set()  # Never join an inference worker on the audio poller.

    def _run(self):
        from samsara.audio_engine.frame import SAMPLE_RATE
        from samsara.audio_tools import resample_audio
        from samsara.vocabulary_decode import transcribe_with_vocabulary
        deadline = time.monotonic() + 8.0  # Also bounded if VAD never admits an onset.
        while not self.stopped.is_set() and time.monotonic() < deadline:
            frames = self.frames()
            if frames and self.app.model_lock.acquire(blocking=False):
                try:
                    audio = np.concatenate(frames)[-SAMPLE_RATE * 7:]
                    audio = resample_audio(audio, SAMPLE_RATE, self.app.model_rate)
                    params = self.app.get_transcription_params(include_vocabulary=False)
                    params.update(beam_size=1, best_of=1, vad_filter=False)
                    if not self.stopped.is_set():
                        segments, _ = transcribe_with_vocabulary(self.app, audio, path='ava', **params)
                        text = ''.join(segment.text for segment in segments).strip()
                        if not self.stopped.is_set():
                            self.partial_text = text
                except Exception as exc:
                    logger.debug('[WAKE] Ava partial unavailable: %s', type(exc).__name__)
                    return  # Final decoding still owns the complete captured audio.
                finally:
                    self.app.model_lock.release()
            if self.stopped.wait(.75):
                return
        self.close()


def begin_primary_capture(consumer, phrase, chunk):
    """Called on an OWW hit, before endpointing; only local work is scheduled."""
    app = consumer._app
    if getattr(app, '_oww_primary_phrase', None):
        return  # Keep the first hit's identity for this utterance.
    wake = app.config.get('wake_word_config', {})
    if not isinstance(phrase, str):  # Older detector/test-double truthy API.
        phrase = wake.get('phrase', 'jarvis')
    app._oww_primary_phrase = phrase
    app._oww_primary_role = phrase_role(wake, phrase)
    if app._oww_primary_role == 'ava':
        stream = AvaWakeStream(app, lambda: list(consumer._utterance_frames) or [chunk])
        app._ava_wake_stream = stream
        stream.start()


def clear_capture(app):
    stream = getattr(app, '_ava_wake_stream', None)
    if isinstance(stream, AvaWakeStream):
        stream.close()
    app._ava_wake_stream = None
    app._oww_primary_phrase = None
    app._oww_primary_role = None


def route_transcript(app, text, buffer):
    """Consume Ava-role text before the Samsara command/dictation parser.

    Identity travels with audio, never through a mutable last-hit global.
    The slow fallback uses the same transcript matcher and the same decoder.
    """
    from samsara.wake_profiles import match_primary_wake
    from samsara.wake_word_matcher import match_wake_phrase
    from samsara.wake_corrections import apply_corrections

    wake = app.config.get('wake_word_config', {})
    corrected = apply_corrections(text.lower())
    detector = getattr(app, '_wake_detector', None)
    phrase = getattr(buffer, 'primary_phrase', None)
    role = getattr(buffer, 'primary_role', None)
    pending = getattr(app, '_ava_wake_pending_until', 0.0)
    followup = app.wake_word_triggered and time.monotonic() < pending
    if phrase:
        matched, _, index = match_wake_phrase(corrected, phrase)
    else:
        phrase, matched, _, index = match_primary_wake(corrected, wake, detector)
        role = phrase_role(wake, phrase) if matched else None
        heads = getattr(detector, '_heads', ())
        if (role == 'ava' and isinstance(heads, list)
                and phrase in {p for p, _, _ in heads}
                and not getattr(buffer, 'primary_hit', False)):
            return False  # A different profile's hit cannot authorize this head.
    if role != 'ava' and not (followup and not matched):
        return False
    # Confirm the actual wake phrase locally. A false head hit must never
    # send unrelated room speech to a provider.
    if not matched and not followup:
        return True
    content = text if not matched else corrected[index + len(phrase):].lstrip(' ,.!?:;-')
    if content.strip():
        app._ava_wake_pending_until = 0.0
        app.wake_word_triggered = False
        timer = getattr(app, 'wake_word_timer', None)
        if timer is not None:
            timer.cancel()
        def dispatch():
            from plugins.commands.ask_ollama import handle_ask_ava
            handle_ask_ava(app, remainder=content.strip())
        thread_registry.spawn('ava-wake-turn', dispatch, daemon=True)
    else:
        # A bare wake arms capture immediately; no greeting/provider round-trip.
        app.wake_word_triggered = True
        app._ava_wake_pending_until = time.monotonic() + 5.0
        app._start_wake_timeout(timeout_s=5.0)
    return True
