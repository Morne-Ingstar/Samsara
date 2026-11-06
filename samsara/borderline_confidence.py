"""1162: recover an acoustically supported, borderline final at temperature zero."""
from math import isfinite


LOGPROB_MARGIN = .10


def _number(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def supported(segments, threshold=-1., no_speech_threshold=.6, *,
              audio=None, rate=16000, speech_level=None, zero_temperature=False):
    """A narrow exception needs positive evidence; missing telemetry cannot qualify.

    Use each segment's PCM span so speech elsewhere cannot excuse a silent
    tail. Without PCM, a caller's measured speech level is the corroborator.
    This never changes the stricter partial policy or decoder telemetry.
    """
    from samsara.hallucination_filter import (
        DECODER_COMPRESSION_RATIO_THRESHOLD, DECODER_NO_SPEECH_THRESHOLD,
        OUTRO_NOISE_CEILING,
    )
    from samsara.hold_final_policy import segment_level

    threshold = _number(threshold)
    silence = _number(no_speech_threshold)
    silence = min(DECODER_NO_SPEECH_THRESHOLD,
                  DECODER_NO_SPEECH_THRESHOLD if silence is None else silence)
    if not segments or threshold is None:
        return False
    for segment in segments:
        logprob = _number(getattr(segment, 'avg_logprob', None))
        compression = _number(getattr(segment, 'compression_ratio', None))
        no_speech = _number(getattr(segment, 'no_speech_prob', None))
        temperature = _number(getattr(segment, 'temperature', None))
        if (logprob is None or not threshold - LOGPROB_MARGIN <= logprob < threshold
                or compression is None or not 0 <= compression <= DECODER_COMPRESSION_RATIO_THRESHOLD
                or no_speech is None or not 0 <= no_speech <= silence
                or (zero_temperature and temperature != 0.)):
            return False
        level = segment_level(segment, audio, rate) if audio is not None else _number(speech_level)
        if level is None or level <= OUTRO_NOISE_CEILING:
            return False
    return True


def recover(app, audio, params, segments, info, check):
    """One deterministic replay, inside the caller's existing lock and deadline.

    faster-whisper tags an exhausted result with the last temperature even
    when it chose an earlier candidate. Replay zero rather than guessing
    which text won. Every replay still passes the normal outro/output gates.
    """
    if not any((_number(getattr(s, 'temperature', None)) or 0.) > 0 for s in segments):
        return segments, info, params
    from samsara.decode_recovery import _clip_timing
    # Whisper can timestamp into its padded input; use the same real-PCM
    # bounds as the downstream guard without changing decoder telemetry.
    aligned = _clip_timing(segments, len(audio) / app.model_rate)
    if not supported(aligned, params.get('log_prob_threshold', -1.),
                     params.get('no_speech_threshold', .6), audio=audio, rate=app.model_rate):
        return segments, info, params
    from samsara.vocabulary_decode import transcribe_with_vocabulary
    from samsara.log import get_logger

    check()
    deterministic = dict(params, temperature=0.)
    iterator, replay_info = transcribe_with_vocabulary(app, audio, path='hold_final', **deterministic)
    replay = []
    for segment in iterator:
        check()
        replay.append(segment)
    check()
    if not replay:
        return segments, info, params
    get_logger(__name__).info('[QUALITY] borderline final replay temperature=0 segments=%d', len(replay))
    return replay, replay_info, deterministic


def low_confidence(segments, params):
    """Reuse the existing diagnostics outcome without inventing a display marker."""
    threshold = _number(params.get('log_prob_threshold', -1.))
    scores = [_number(getattr(s, 'avg_logprob', None)) for s in segments]
    return bool(scores and threshold is not None
                and all(score is not None and score < threshold for score in scores))
