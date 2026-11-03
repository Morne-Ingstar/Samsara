"""Keep stopped streaming captures recoverable without changing decode gates (1042).

Audio stays in the existing single in-memory retry slot, never a new disk store.
History uses its normal privacy policy; only accepted previews may become text.
"""
import logging

from samsara import decode_recovery

logger = logging.getLogger(__name__)
# PLACEHOLDER COPY (1042)
RETRY = 'Audio kept; say try again.'
EMPTY = 'Nothing heard. ' + RETRY
CANCELLED = 'Recording stopped. ' + RETRY


def _audio(session):
    # An explicit retry decodes the original take, not the new "try again" PCM.
    audio = getattr(session, '_decode_audio', None)
    return audio if audio is not None else getattr(session, '_stopped_audio_1042', None)


def retain_stopped(session, reason):
    if getattr(session, '_retry_retained_1042', False):
        return True
    audio = _audio(session)
    if audio is None or not getattr(audio, 'size', 0):
        return False
    try:
        params = getattr(session, '_decode_params', None)
        if params is None:
            params = session._worker._final_params()
        decode_recovery.retain(session.app, audio, params, reason)
        session._retry_retained_1042 = True
        return True
    except Exception:
        logger.exception('[STREAM] stopped capture could not be retained')
        return False


def capture_stopped(session, audio):
    session._stopped_audio_1042 = audio
    if session.cancel_event.is_set():
        cancelled(session)
    return audio


def prepare_final(session, text, raw_text, duration_s):
    """Preserve a preview or explain the retained PCM before empty delivery."""
    # Tutorial captures already own their empty-result display and Home feedback.
    if getattr(session, '_tutorial_owner', None) is not None or str(text or '').strip():
        return text, raw_text, duration_s
    audio = _audio(session)
    if audio is not None and getattr(audio, 'size', 0):
        duration_s = len(audio) / session.app.model_rate
    retained = retain_stopped(session, 'streaming_empty')
    preview = getattr(session, '_last_partial', '')
    if isinstance(preview, str) and preview.strip():
        session._fallback_notice_rejected = True
        session._decode_failure = None
        session._quiet_empty_capture_899 = False
        return preview, preview, duration_s
    if retained:
        existing = getattr(session, '_decode_failure', None)
        session._decode_failure = ('Audio capture stopped. ' + RETRY
                                   if existing == decode_recovery.LOST else
                                   'Words unclear. ' + RETRY if isinstance(existing, str) and existing else EMPTY)
        session._quiet_empty_capture_899 = False
    else:
        # No PCM can be recovered; never claim it was kept.
        session._decode_failure = 'No audio was captured. Check the microphone.'
        session._quiet_empty_capture_899 = False
    return text, raw_text, duration_s


def cancelled(session):
    """Cancellation still prevents injection, while retaining work once."""
    retained = retain_stopped(session, 'streaming_cancelled')
    preview = getattr(session, '_last_partial', '')
    preview = preview.strip() if isinstance(preview, str) else ''
    if not retained and not preview:
        return
    audio = _audio(session)
    duration_s = len(audio) / getattr(session.app, 'model_rate', 16000) if retained else 0
    # PLACEHOLDER COPY (1042)
    message = 'Recording stopped. Preview saved in History.' if preview else CANCELLED
    if not getattr(session, '_recovery_history_1042', False):
        session._recovery_history_1042 = True
        try:
            session.app._log_history(
                raw_text=preview, display_text=preview or message,
                duration_ms=int(duration_s * 1000), mode='streaming', status='failed')
        except Exception:
            logger.exception('[STREAM] stopped capture History write failed')
            session._recovery_history_1042 = False
    if not getattr(session, '_recovery_notice_1042', False):
        session._recovery_notice_1042 = True
        from samsara.failure_reasons import post_reason
        post_reason(session.app, 'long_failure', headline=message)
    logger.info('[STREAM] stopped capture retained audio=%s preview_chars=%d', retained, len(preview))
