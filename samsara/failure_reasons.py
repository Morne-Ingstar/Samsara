"""Capture failure classification, feedback and small producer seams."""
from __future__ import annotations

import logging
import threading
from time import monotonic


CLIP_AMPLITUDE = .99
CLIP_FRAME_S = .020
CLIP_WINDOW_S = .250
CLIP_VOICED_RMS = .01
CLIP_MIN_WINDOWS = 3
CLIP_MIN_VOICED_FRACTION = .30
CLIP_MIN_SPREAD_S = 2.0
CLIP_NOTICE_INTERVAL_S = 600.0
_clip_notice_lock = threading.Lock()


def reason_for(kind, context):
    """Return a headline and optional hint from capture facts, never words."""
    # PLACEHOLDER COPY (868) -- prompt renumbered to 891.
    config = context.get('config', {})
    if kind == 'short_tap':
        if context.get('handsfree'):
            return 'Say more - that was too short to hear', ''
        from samsara.config_defaults import DEFAULTS
        from samsara.ui.home_qt import hotkey_label
        key = hotkey_label(config.get('hotkey') or DEFAULTS['hotkey'])
        return f'Too short - hold {key} while you speak, release when done', ''
    if kind == 'near_silent':
        return 'Nothing heard - check the mic, or speak louder', ''
    if kind == 'long_failure':
        return "Couldn't transcribe that - try a shorter take", 'Report a problem'
    if kind == 'clipping' and context.get('sustained_clipping', False):
        return 'Mic is too loud - turn the mic level down', ''
    return '', ''


def capture_peak(audio):
    """PCM amplitude, unlike speech-presence's similarly named probability."""
    import numpy as np
    if audio is None or not len(audio):
        return 0.0
    return float(np.max(np.abs(audio)))


def sustained_clipping(audio, sample_rate):
    """Require clipping across voiced windows, beyond a short cough cluster.

    RMS is an amplitude-only proxy for voiced activity; silence must not dilute
    a genuinely hot take. No transcript, mic access or audio retention is needed.
    """
    import numpy as np
    if audio is None or not len(audio) or sample_rate <= 0:
        return False
    samples = np.asarray(audio).reshape(-1)
    frame_size = max(1, round(sample_rate * CLIP_FRAME_S))
    starts = np.arange(0, samples.size, frame_size)
    sizes = np.minimum(frame_size, samples.size - starts)
    magnitudes = np.abs(samples.astype(np.float64))
    peaks = np.maximum.reduceat(magnitudes, starts)
    rms = np.sqrt(np.add.reduceat(magnitudes ** 2, starts) / sizes)
    windows = np.floor(starts / sample_rate / CLIP_WINDOW_S).astype(int)
    voiced = np.unique(windows[rms >= CLIP_VOICED_RMS])
    clipped = np.unique(windows[peaks >= CLIP_AMPLITUDE])
    # Include clipped windows even when a sparse clipped sample has low RMS.
    active = np.union1d(voiced, clipped)
    return bool(len(clipped) >= CLIP_MIN_WINDOWS
                and len(clipped) >= CLIP_MIN_VOICED_FRACTION * len(active)
                and (clipped[-1] - clipped[0]) * CLIP_WINDOW_S >= CLIP_MIN_SPREAD_S)


def post_reason(app, kind, *, handsfree=False, sustained_clipping=False, headline=None):
    """Use the existing message lifetime, report action and speech queue."""
    from samsara.live_surface.feedback import schedule_feedback
    from samsara.message_durations import message_duration_ms
    config = getattr(app, 'config', {}) or {}
    text, hint = reason_for(kind, dict(config=config, handsfree=handsfree,
                                     sustained_clipping=sustained_clipping))
    text = headline or text
    if not text:
        return
    quiet = kind == 'clipping'
    speak = config.get('tts', {}).get('feedback_level') == 'full'
    if quiet:
        with _clip_notice_lock:
            now = monotonic()
            last = getattr(app, '_clipping_notice_at_972', None)
            if last is not None and now - last < CLIP_NOTICE_INTERVAL_S:
                return
            app._clipping_notice_at_972 = now
            speak = speak and not getattr(app, '_clipping_spoken_972', False)
            if speak:
                app._clipping_spoken_972 = True
    label = 'Captured\n' + text if quiet else text
    severity = 'accent' if quiet else 'error'
    try:
        duration = message_duration_ms(label, severity)
        if getattr(app, 'listening_indicator', None) is not None:
            schedule_feedback(app, label, severity, duration, source='capture_' + kind)
        else:
            app._show_outcome_chip(label, severity, duration)
        if callable(getattr(app, '_schedule_ui', None)):
            app._schedule_ui(_home_message, app, text, duration, severity)
        if speak:
            app._speak_session_notice(text)
    except Exception:
        logging.getLogger(__name__).debug('Capture feedback unavailable', exc_info=True)


def _home_message(app, text, duration, kind='error'):
    window = getattr(getattr(app, 'main_window', None), '_window', None)
    if window is not None and window.isVisible():
        bar = window.statusBar()
        if callable(getattr(bar, 'show_feedback', None)):
            bar.show_feedback(text, kind, duration)
        else:
            window.statusBar().showMessage(text, duration or 0)


def post_clipping(app, audio):
    post_reason(app, 'clipping', sustained_clipping=sustained_clipping(audio, app.model_rate))


def post_delivery_clipping(app, audio, delivered):
    if delivered is not False:
        post_clipping(app, audio)
        if delivered is not None:
            from samsara.gpu_fallback import report_delivered
            report_delivered(app, delivered)


def preserve_clipping(label, kind, owner, protected):
    from samsara.message_durations import routine_receipt
    return owner == 'capture_clipping' and protected and routine_receipt(label, kind)


def skip_decode(pcm, app, **kwargs):
    """Pre-decode silence is an accidental capture, not a failed attempt (899)."""
    from samsara.speech_presence import skip_decode as detect
    return detect(pcm, app, **kwargs)


def _explain_empty_capture(app, duration_s, owner):
    # 921: tutorial practice is deliberate; ordinary empty holds keep 481/899 quiet.
    from samsara import tutorial_capture
    return duration_s > 0 and tutorial_capture.alive(app, owner)


def show_empty_capture(app, duration_s, mode='hold', *, tutorial_owner=None):
    """Explain tutorial attempts; preserve ordinary empty-capture silence/history."""
    if _explain_empty_capture(app, duration_s, tutorial_owner):
        post_reason(app, 'short_tap' if duration_s < .51 else 'near_silent')
    if duration_s > .5:
        try:
            app._log_history(raw_text='', display_text='(no speech detected)',
                             duration_ms=int(duration_s * 1000), mode=mode,
                             status='empty', entry_type='failed')
        except Exception:
            pass


def finish_streaming_if_empty(worker, audio):
    from samsara.speech_presence import finish_streaming_if_empty as finish
    session = worker._session
    session._capture_clipping_972 = sustained_clipping(audio, session.app.model_rate)
    duration = len(audio) / session.app.model_rate if audio is not None else 0
    session._empty_capture_kind_891 = 'short_tap' if duration < .51 else 'near_silent'
    # The detector can synchronously deliver on_final; mark ownership first.
    session._quiet_empty_capture_899 = not _explain_empty_capture(
        session.app, duration, getattr(session, '_tutorial_owner', None))
    # 1125: even one measured speech frame gets the bounded relaxed decode.
    if audio is not None and len(audio):
        from samsara.speech_presence import measure
        from samsara.decode_recovery import Chunk, _can_retry_no_speech
        evidence = measure(audio, session.app.model_rate)
        capture = getattr(worker, '_speech_presence_capture', None)
        if _can_retry_no_speech(evidence, session.app.model_rate, len(audio),
                               Chunk(0, len(audio), 0, len(audio)),
                               {'no_speech_threshold': .6}, capture):
            session._empty_capture_kind_891 = None
            session._quiet_empty_capture_899 = False
            return False
    if duration < .51:
        from samsara.speech_presence import skip_decode
        skip_decode([] if audio is None else audio, session.app,
                    path='streaming', skip_reason='not_decoded')
        session.on_final(None, duration_s=duration)
        return True
    finished = finish(worker, audio)
    if not finished:
        session._empty_capture_kind_891 = None
        session._quiet_empty_capture_899 = False
    return finished


def stream_empty_message(session):
    """The streaming result and listening receipt share one classification."""
    if getattr(session, '_quiet_empty_capture_899', False):
        return ''
    kind = getattr(session, '_empty_capture_kind_891', None)
    return reason_for(kind, {'config': session.app.config})[0] if kind else 'No speech'


def stream_decode_failed(session):
    session._empty_capture_kind_891 = 'long_failure'
    session.on_final(None)


def report_retained_failure(app):
    from samsara.dictation_retry import show_note
    show_note(app, empty=True)


def deliver_stream_final(session, text, raw_text, duration_s, elapsed_ms):
    """Let delivery retain its capture ownership, then publish its receipt."""
    from samsara import tutorial_capture
    live = not session.cancel_event.is_set() and tutorial_capture.final_current(session)
    from samsara.mic_score import deliver_with_history
    deliver_with_history(session, session._deliver_final, text, raw_text, duration_s, elapsed_ms)
    if not live:
        return
    if getattr(session, '_retry_note_shown_1125', False):
        return
    if not text or not text.strip():
        if getattr(session, '_quiet_empty_capture_899', False):
            return
        existing = getattr(session, '_decode_failure', None)
        kind = getattr(session, '_empty_capture_kind_891', None) or (
            'long_failure' if existing or duration_s >= .51 else
            'short_tap' if duration_s < .3 else 'near_silent')
        # Audio-lost/retry guidance already explains its own detection; retain it.
        post_reason(session.app, kind, headline=existing if isinstance(existing, str) else None)
    else:
        post_reason(session.app, 'clipping',
                    sustained_clipping=getattr(session, '_capture_clipping_972', False))
