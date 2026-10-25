"""Capture failure copy and small producer seams; no detection policy changes."""
from __future__ import annotations

import logging


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
    if kind == 'clipping' and context.get('peak', 0) >= .99:
        return 'Mic is too loud - turn the mic level down', ''
    return '', ''


def capture_peak(audio):
    """PCM amplitude, unlike speech-presence's similarly named probability."""
    import numpy as np
    if audio is None or not len(audio):
        return 0.0
    return float(np.max(np.abs(audio)))


def post_reason(app, kind, *, handsfree=False, peak=0.0, headline=None):
    """Use the existing message lifetime, report action and speech queue."""
    from samsara.live_surface.feedback import schedule_feedback
    from samsara.message_durations import message_duration_ms
    config = getattr(app, 'config', {}) or {}
    text, hint = reason_for(kind, dict(config=config, handsfree=handsfree, peak=peak))
    text = headline or text
    if not text:
        return
    quiet = kind == 'clipping'
    label = 'Captured\n' + text if quiet else text
    severity = 'accent' if quiet else 'error'
    try:
        schedule_feedback(app, label, severity, 4000, source='capture_' + kind)
        duration = message_duration_ms(label, severity, 4000)
        app._schedule_ui(_home_message, app, text, duration)
        if config.get('tts', {}).get('feedback_level') == 'full':
            app._speak_session_notice(text)
    except Exception:
        logging.getLogger(__name__).debug('Capture feedback unavailable', exc_info=True)


def _home_message(app, text, duration):
    window = getattr(getattr(app, 'main_window', None), '_window', None)
    if window is not None and window.isVisible():
        window.statusBar().showMessage(text, duration)


def post_clipping(app, audio):
    post_reason(app, 'clipping', peak=capture_peak(audio))


def post_delivery_clipping(app, audio, delivered):
    if delivered is not False:
        post_clipping(app, audio)


def preserve_clipping(label, kind, owner, protected):
    from samsara.message_durations import routine_receipt
    return owner == 'capture_clipping' and protected and routine_receipt(label, kind)


def skip_decode(pcm, app, **kwargs):
    """Hands-free call seam: keep the detector and its return value intact."""
    from samsara.speech_presence import skip_decode as detect
    rejected = detect(pcm, app, **kwargs)
    duration = len(pcm) / app.model_rate
    if duration < .3 or rejected:
        post_reason(app, 'short_tap' if duration < .3 else 'near_silent', handsfree=True)
    return rejected


def show_empty_capture(app, duration_s, mode='hold'):
    """Preserve the existing failed-history record, replace its vague notice."""
    post_reason(app, 'near_silent', handsfree=mode == 'handsfree')
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
    session._capture_peak_891 = capture_peak(audio)
    duration = len(audio) / session.app.model_rate if audio is not None else 0
    session._empty_capture_kind_891 = 'short_tap' if duration < .51 else 'near_silent'
    finished = finish(worker, audio)
    if not finished:
        session._empty_capture_kind_891 = None
    return finished


def stream_decode_failed(session):
    session._empty_capture_kind_891 = 'long_failure'
    session.on_final(None)


def report_retained_failure(app):
    from samsara.decode_recovery import UNCLEAR
    post_reason(app, 'long_failure', headline=UNCLEAR)


def deliver_stream_final(session, text, raw_text, duration_s, elapsed_ms):
    """Let delivery retain its capture ownership, then publish its receipt."""
    from samsara import tutorial_capture
    live = not session.cancel_event.is_set() and tutorial_capture.final_current(session)
    session._deliver_final(text, raw_text, duration_s, elapsed_ms)
    if not live:
        return
    if not text or not text.strip():
        existing = getattr(session, '_decode_failure', None)
        kind = getattr(session, '_empty_capture_kind_891', None) or (
            'long_failure' if existing or duration_s >= .51 else
            'short_tap' if duration_s < .3 else 'near_silent')
        # Audio-lost/retry guidance already explains its own detection; retain it.
        post_reason(session.app, kind, headline=existing if isinstance(existing, str) else None)
    else:
        post_reason(session.app, 'clipping', peak=getattr(session, '_capture_peak_891', 0))
