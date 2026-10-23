"""Restrict sparse held dictation to existing Silero speech evidence.

Unknown evidence, short captures and ordinary speech keep the established
recovery path. Sparse holds decode padded regions and corroborate each word.
"""
from copy import copy
from dataclasses import dataclass
import logging
from math import ceil

import numpy as np

from samsara import command_clip_trim, decode_recovery, speech_presence
from samsara.decode_recovery import LOST  # Streaming's existing capture-loss seam.


RATE = command_clip_trim.RATE
FRAME = command_clip_trim.FRAME
PAD_S = .25
LONG_HOLD_S = 10.0
SPARSE_RATIO = .15
MIN_OVERLAP = .60
logger = logging.getLogger('Samsara')


@dataclass(frozen=True)
class Regions:
    # Half-open original-sample intervals, unpadded and padded respectively.
    speech: tuple
    padded: tuple
    samples: int

    @property
    def sparse(self):
        return sum(end - start for start, end in self.speech) < self.samples * SPARSE_RATIO

    def concatenate(self, audio):
        """Return compact PCM and padded-region intervals in compact seconds.

        Padding belongs to the speech region: VAD frame edges are not phoneme
        boundaries, especially for quiet first/last consonants.
        """
        pieces, evidence, offset = [], [], 0
        for start, end in self.padded:
            pieces.append(audio[start:end])
            evidence.append((offset / RATE, (offset + end - start) / RATE))
            offset += end - start
        return (np.concatenate(pieces) if pieces else np.empty(0, np.float32),
                tuple(evidence))


def locate(audio, result, *, capture=None):
    """Reuse 780 onset preparation, retaining 534's shorter quiet evidence.

    Each positive interval gets its own bounded onset probe so a later, longer
    word cannot replace an earlier short one. Command gain is never applied.
    """
    pcm, scores = np.asarray(audio), np.asarray(result.probabilities)
    if (not result.known or pcm.ndim != 1 or scores.ndim != 1 or not np.isfinite(pcm).all()
            or result.samples != len(pcm) or len(scores) != ceil(len(pcm) / FRAME)
            or not np.isfinite(scores).all() or np.any(scores < 0) or np.any(scores > 1)):
        return None
    boundary = 0
    if capture is not None:
        try:
            earcon_ms = capture.offsets()[2]
            if not np.isfinite(earcon_ms):
                return None
            boundary = max(0, ceil(earcon_ms * RATE / 1000))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None
    changes = np.diff(np.r_[False, scores > speech_presence.PROBABILITY, False].astype(np.int8))
    speech = []
    for first, last in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)):
        start, end = max(boundary, int(first) * FRAME), min(len(pcm), int(last) * FRAME)
        if end <= start or (end - start < speech_presence.MIN_RUN_S * RATE
                            and not np.any(scores[first:last] > .5)):
            continue
        head = max(boundary, (int(first) - 8) * FRAME, 0)
        tail = min(end, head + int(command_clip_trim.MAX_CLIP_S * RATE))
        tail = head + (tail - head) // FRAME * FRAME
        prepared = command_clip_trim.prepare(
            pcm[head:tail], RATE, scores[head // FRAME:head // FRAME + (tail-head)//FRAME],
        ) if head % FRAME == 0 else None
        if prepared is not None and prepared.longest_s:
            start = max(start, head + prepared.speech_sample)
        speech.append((start, end))
    padded = []
    pad = round(PAD_S * RATE)
    for start, end in speech:
        start, end = max(boundary, start - pad), min(len(pcm), end + pad)
        if padded and start <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(end, padded[-1][1]))
        else:
            padded.append((start, end))
    return Regions(tuple(speech), tuple(padded), len(pcm))


def filter_words(segments, regions):
    """Keep >=60% word overlap; missing alignment cannot authorize sparse words."""
    kept, dropped = [], 0
    for segment in segments:
        words = getattr(segment, 'words', None)
        if not words:
            dropped += len(segment.text.split())
            continue
        accepted = []
        for word in words:
            try:
                start, end = float(word.start), float(word.end)
                overlap = sum(max(0., min(end, b) - max(start, a)) for a, b in regions)
                valid = (np.isfinite(start) and np.isfinite(end) and start >= 0
                         and end > start and overlap + 1e-9 >= MIN_OVERLAP * (end-start))
            except (AttributeError, TypeError, ValueError, OverflowError):
                valid = False
            if valid:
                accepted.append(word)
            else:
                dropped += 1
        if accepted:
            item = copy(segment)
            item.words = accepted
            item.text = ''.join(word.word for word in accepted)
            item.start, item.end = accepted[0].start, accepted[-1].end
            kept.append(item)
    if dropped:
        logger.info('[GUARD] dropped words outside speech regions (%d words)', dropped)
    return kept, ''.join(segment.text for segment in kept).strip()


def _ordinary_final(worker, audio, params):
    """Keep explicit retry semantics and guard any retained compact timeline."""
    session = worker._session
    retained = getattr(session.app, '_hold_retry_audio', None)
    segments, info, text = decode_recovery.stream_final(worker, audio, params)
    bounds = getattr(retained, 'speech_regions', None)
    if bounds is not None and getattr(session, '_decode_audio', None) is retained.audio:
        segments, text = filter_words(segments, bounds)
    return segments, info, text


def stream_final(worker, audio, params):
    """Streaming final seam, keeping the existing bounded model-lock owner."""
    app, session = worker._session.app, worker._session
    if (app.model_rate != RATE or len(audio) <= LONG_HOLD_S * RATE
            or app.config.get('dictation', {}).get('speech_presence_gate', True) is False):
        return _ordinary_final(worker, audio, params)
    try:
        result = speech_presence._presence.measure(audio, RATE)
        regions = locate(audio, result, capture=getattr(worker, '_speech_presence_capture', None))
    except Exception as exc:
        logger.info('[GATE] speech_regions_unavailable exception_type=%s', type(exc).__name__)
        regions = None
    if regions is None or not regions.sparse:
        return _ordinary_final(worker, audio, params)
    compact, evidence = regions.concatenate(audio)
    logger.info('[GATE] speech_regions source_s=%.3f speech_s=%.3f decode_s=%.3f regions=%d',
                len(audio)/RATE, sum(b-a for a,b in regions.speech)/RATE,
                len(compact)/RATE, len(regions.padded))
    if not len(compact):
        return [], None, ''
    # Both bounded attempts keep the same timeline. Unaligned partials and
    # chunk-relative recovery timestamps must never replace rejected words.
    params = dict(params, word_timestamps=True, without_timestamps=False)
    session._decode_audio, session._decode_params = compact, dict(params)
    deadline = decode_recovery._deadline(app)
    try:
        for retry in (False, True):
            effective = decode_recovery._params(app, params, len(compact)/RATE, retry=retry)
            segments, info = decode_recovery._transcribe(
                app, compact, effective, deadline, session.cancel_event)
            segments, text = filter_words(segments, evidence)
            if not text:
                return [], info, ''
            if not worker._decoder_guard(segments, text, effective, duration_s=len(compact)/RATE):
                if decode_recovery._retry_request(app, text) is not None:
                    return _ordinary_final(worker, compact, params)
                return segments, info, text
        raise ValueError('sparse hold recovery rejected')
    except Exception as exc:
        reason = 'timeout' if isinstance(exc, TimeoutError) else 'decode_rejected'
        decode_recovery.retain(app, compact, params, reason)
        app._hold_retry_audio.speech_regions = evidence
        session._decode_failure = decode_recovery.UNCLEAR
        logger.info('[DECODE] sparse hold recovery failed reason=%s', reason)
        return [], None, ''
