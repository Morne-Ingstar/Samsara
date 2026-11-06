"""1165: retry long rejected intervals as smaller, pause-bounded pieces.

Run inline in the existing native job: the same model lock, deadline,
cancellation and every decoder/output gate remain in force. No recursion
beyond one subdivision, gain changes, or discarded neighboring words.
"""
from samsara.dictation_retry import MissedRange

MIN_RESPLIT_S = 6.
RECOVERABLE = {None, 'fallback_exhausted', 'compression_ratio', 'repetition_loop',
               'segment_quality', 'no_speech', 'low_logprob_at_noise_floor'}


def recover_failed_ranges(app, audio, chunk, accepted, reason, result, first,
                          params, deadline, cancel, guard, abandoned):
    from samsara import decode_recovery as decoder
    if reason not in RECOVERABLE:
        return accepted, reason, False
    origin, limit = chunk.start / app.model_rate, chunk.end / app.model_rate
    pending = result.missed[first:]
    targets = []
    for part in sorted(set(pending), key=lambda p: (p.start, p.end)):
        start, end = max(origin, part.start), min(limit, part.end)
        if part.delivered or end - start < MIN_RESPLIT_S:
            continue
        # Duplicate whole-chunk/guard reports describe the same rejected PCM.
        if targets and start <= targets[-1][1]:
            targets[-1] = (targets[-1][0], max(end, targets[-1][1]))
        else:
            targets.append((start, end))
    if not targets:
        return accepted, reason, False
    remaining = [p for p in pending if not any(a <= p.start and p.end <= b for a, b in targets)]
    for start, end in targets:
        a, b = round(start * app.model_rate), round(end * app.model_rate)
        part = decoder._transcribe(app, audio[a:b], params, deadline, cancel, guard,
                                   recovery=True, inline=abandoned)
        accepted.extend(decoder._offset(s, start - origin) for s in part.segments)
        result.info = part.info or result.info
        remaining.extend(MissedRange(p.start + start, p.end + start, p.delivered) for p in part.missed)
        if not part.segments and not part.missed:
            # Confirmed silence is not missing speech.
            continue
        decoder.logger.info('[HOLD_FINAL] resplit start_s=%.3f end_s=%.3f recovered_segments=%d missed_ranges=%d',
                            start, end, len(part.segments), len(part.missed))
    result.missed[first:] = remaining
    accepted.sort(key=lambda s: getattr(s, 'start', 0.))
    return accepted, reason if remaining else None, True
