"""Line up hands-free logs, flight events and saved ring PCM without emitting text.

Use explicit offset-bearing ISO timestamps with --start and --end. WAV samples
are read in place. JSON output uses Unix seconds, fixed labels and numeric data.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from math import isfinite
from pathlib import Path
import re
from statistics import median
import wave

import numpy as np

from tools.diag.handsfree_deaf_1286 import STAMP
from tools.voice_corpus.handsfree_replay import safe_source


RATE = 16000
VAD_STEP = 512
SYNC_S = .15  # Clock/calibration and log-to-flight pairing tolerance.
GAP_S = .15   # Recorder's tolerance beyond the nominal frame width.
LOG_CLOSE_DELAY_S = .35  # The closing-frame rescore/log precedes the flight write.
CLOSE_FIELDS = 'vad_used threshold vad_peak rms rms_threshold silence_s since_release_s'.split()
GATE_FIELDS = ['duration_s', 'peak', 'frames', 'head_grace_ms', 'grace_peak'] + [
    f'{prefix}{metric}_p{threshold}_s' for prefix in ('', 'grace_')
    for metric in ('total', 'run') for threshold in ('01', '03', '05')
]
VERDICTS = {'pass', 'reject', 'unknown', 'skipped'}
DROPS = {'no_speech_in_audio', 'empty_transcription', 'signoff_hallucination', 'too_short', 'fragment'}
FLIGHT_EVENTS = {'wake.session_open', 'wake.session_close', 'hold_recording.start',
                 'hold_recording.stop', 'hold_capture_duck.engage'}
CLOSE_REASONS = {'silence_flush', 'too_short', 'stuck_buffer', 'hard_buffer_cap'}
LOG_CONTEXT = {
    '[DUCK] capture duck engaged': 'duck_engage',
    '[DUCK] capture duck restored': 'duck_restore',
    '[SEAM] Ava lane suppression ENGAGED': 'ava_suppression_on',
    '[SEAM] Ava lane suppression RELEASED': 'ava_suppression_off',
    '[CMD MODE] Entering command mode': 'mode_enter',
    '[CMD MODE] Exiting command mode': 'mode_exit',
}
INCOMPLETE = {'frame_discontinuity', 'duration_cap', 'queue_overflow', 'event_cap',
              'ended_with_pending_decode', 'no_frames'}


def _fields(line, allowed):
    return {key: float(value) for key, value in
            re.findall(r'\b([a-z0-9_]+)=(-?\d+(?:\.\d+)?)(?=s?(?:\s|$))', line)
            if key in allowed and isfinite(float(value))}


def _label(line, key, allowed):
    match = re.search(rf'\b{key}=([a-z_]+)\b', line)
    return match[1] if match and match[1] in allowed else 'other'


def _read_logs(root, start, end, zone):
    coverage, events = [], []
    for name in ('samsara.log', *(f'samsara.log.{n}' for n in range(1, 4))):
        path = safe_source(root / name)
        if not path.is_file():
            continue
        first = last = None
        with path.open(encoding='utf-8', errors='replace') as source:
            for line in source:
                stamp = STAMP.match(line)
                if not stamp:
                    continue
                first = first or stamp[1]
                last = stamp[1]
                at = datetime.fromisoformat(last.replace(',', '.')).replace(tzinfo=zone).timestamp()
                if not start <= at <= end:
                    continue
                event, fields = _log_event(line)
                if event:
                    events.append(dict(at=at, event=event, **fields))
        if first:
            coverage.append([datetime.fromisoformat(s.replace(',', '.')).replace(tzinfo=zone).timestamp()
                             for s in (first, last)])
    return _unique(events), coverage


def _log_event(line):
    if '[HANDSFREE-SILENCE]' in line:
        return 'silence', _fields(line, CLOSE_FIELDS)
    if '[GATE] speech_presence path=handsfree ' in line:
        return 'gate', dict(verdict=_label(line, 'verdict', VERDICTS), **_fields(line, GATE_FIELDS))
    if '[CMD-UTT] dropped reason=' in line:
        return 'drop', dict(reason=_label(line, 'reason', DROPS), **_fields(line, {'duration'}))
    parked = re.search(r'\[DICTATE-PREVIEW\] parked (\d+)-char draft at session exit', line)
    if parked:
        return 'park', {'chars': int(parked[1])}
    for marker, label in LOG_CONTEXT.items():
        if marker in line:
            return label, {}
    if '[HOTKEY] Main hotkey' in line and 'pressed' in line:
        return 'hold_press', {}
    if 'suppression RELEASED' in line:
        return 'wake_suppression_off', {}
    if '[TTS]' in line or 'AudioCoordinator:' in line or '[AVA]' in line:
        return 'tts_ava_log', {}
    return None, {}


def _unique(events):
    return sorted({tuple(row.items()): row for row in events}.values(), key=lambda row: row['at'])


def _read_flight(root, start, end, zone):
    opened, events, closes, malformed = {}, [], [], 0
    day = datetime.fromtimestamp(start, zone).date()
    while day <= datetime.fromtimestamp(end, zone).date():
        path = safe_source(root / f'events-{day:%Y%m%d}.jsonl')
        day += timedelta(days=1)
        if not path.is_file():
            continue
        with path.open(encoding='utf-8', errors='replace') as source:
            for line in source:
                try:
                    row = json.loads(line)
                    at = row['t_wall']
                    if not _number(at):
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    malformed += 1
                    continue
                event = row.get('event')
                if event not in FLIGHT_EVENTS or not start <= at <= end:
                    continue
                evidence = dict(at=at, event=event)
                thread = row.get('thread')  # Internal join key only.
                if event == 'wake.session_open':
                    opened[thread] = at
                if event == 'wake.session_close':
                    reason = row.get('reason')
                    evidence.update(reason=reason if reason in CLOSE_REASONS else 'other',
                                    opened_at=opened.pop(thread, None),
                                    buffer_s=row.get('speech_s') if _number(row.get('speech_s')) else None)
                    if reason == 'silence_flush' and _number(row.get('t_mono')):
                        closes.append((row['t_mono'], evidence))
                events.append(evidence)
    return events, sorted(closes, key=lambda pair: pair[0]), malformed


def _number(value):
    return type(value) in (int, float) and isfinite(value)


@dataclass(frozen=True)
class Frame:
    offset: int
    samples: int
    at: float             # Capture clock at the end of this frame.
    observed: float
    seq: int
    epoch: int

    @property
    def start(self):
        return self.at - self.samples / RATE


@dataclass
class Session:
    wav: Path
    frames: list[Frame] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)
    delivered: int = 0
    samples: int = 0
    complete: bool = False
    failure: str | None = None

    def gaps(self):
        return [(right.offset, left.at, right.start, right.seq - left.seq - 1,
                 int(right.epoch != left.epoch)) for left, right in zip(self.frames, self.frames[1:])
                if right.seq != left.seq + 1 or right.epoch != left.epoch
                or abs(left.at - right.start) > GAP_S]


def _sessions(root):
    sessions, unreadable = [], 0
    for path in sorted(root.glob('*.json')):
        if not re.fullmatch(r'[0-9a-f]{32}', path.stem):
            continue
        try:
            session = _read_session(safe_source(path))
        except (ValueError, KeyError, OSError, TypeError, EOFError, wave.Error):
            unreadable += 1
            continue
        if session.frames:
            sessions.append(session)
    return sorted(sessions, key=lambda session: session.frames[0].at), unreadable


def _read_session(path):
    meta = json.loads(path.read_text(encoding='utf-8'))
    if meta.get('kind') != 'REAL_SESSION' or meta.get('schema') != 1:
        raise ValueError
    reason = meta.get('incomplete_reason')
    session = Session(safe_source(path.with_suffix('.wav')), complete=meta.get('complete') is True,
                      failure=reason if reason in INCOMPLETE else None if reason is None else 'other')
    with wave.open(str(session.wav), 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (RATE, 1, 2):
            raise ValueError
        session.samples = wav.getnframes()
    offset = 0
    with safe_source(path.with_suffix('.jsonl')).open(encoding='utf-8') as source:
        for line in source:
            row = json.loads(line)
            event = row['event']
            if event == 'frame':
                observed = row['observed_ns'] / 1e9
                frame = Frame(row['sample_offset'], row['samples'], row['capture_t'], observed,
                              row['seq'], row['device_epoch'])
                if (type(frame.samples) is not int or frame.samples <= 0 or frame.offset != offset
                        or not _number(frame.at) or not _number(observed)):
                    raise ValueError
                session.frames.append(frame)
                offset += frame.samples
            elif event == 'chunk':
                session.chunks.append({key: row[key] for key in ('samples', 'sample_offset',
                    'speech_start_ns', 'speech_end_ns', 'last_voice_ns')})
            elif event == 'draft' and isinstance(row.get('text'), str):
                session.drafts.append(dict(at=row['observed_ns'] / 1e9,
                    chars=len(row['text']), words=len(row['text'].split())))
            elif event == 'dispatch' and isinstance(row.get('delivered'), str):
                session.delivered += bool(row['delivered'])
    if offset != session.samples or offset != meta.get('samples'):
        raise ValueError
    return session


def _pair_chunk(chunk, closes, mono, shift=None):
    if not _number(chunk['speech_end_ns']):
        return None
    at = chunk['speech_end_ns'] / 1e9
    left = bisect_left(mono, at - SYNC_S)
    right = bisect_left(mono, at + SYNC_S)
    candidates = closes[left:right] if shift is None else [
        (m, event) for m, event in closes if -.020 <= event['at'] - (at + shift) <= LOG_CLOSE_DELAY_S]
    matches = [event for _, event in candidates if event['buffer_s'] is not None
               and abs(chunk['samples'] / RATE - event['buffer_s']) < .001]
    return matches[0] if len(matches) == 1 else None


def _silence_for(rows, at, previous_at):
    matches = [row for row in rows if previous_at < row['at'] <= at + .002
               and at - row['at'] <= LOG_CLOSE_DELAY_S]
    return matches[0] if len(matches) == 1 else None


def _nearest_gate(close, gates, next_close):
    # The hands-free worker is FIFO. Where a later close interleaves, leave the
    # gate separate unless there is exactly one compatible row before it.
    matches = [gate for gate in gates if close['at'] - .002 <= gate['at'] < next_close
               and gate['at'] - close['at'] < 3 and close['buffer_s'] is not None
               and abs(gate.get('duration_s', -1) - close['buffer_s']) <= .002]
    return matches[0] if len(matches) == 1 else None


def _offline(audio, measure):
    result = measure(audio, RATE)
    if not result.known or not result.probabilities:
        return None
    return np.asarray(result.probabilities)


def _metrics(scores, weights):
    result = {}
    if scores is None:
        return result
    active = weights > 0
    if not np.any(active):
        return result
    result['peak'] = round(float(np.max(scores[active])), 6)
    for threshold, label in ((.1, '01'), (.5, '05')):
        positive = np.where(scores > threshold, weights, 0.)
        changes = np.flatnonzero(np.diff(np.r_[False, positive > 0, False]))
        longest = max((np.sum(positive[a:b]) for a, b in zip(changes[::2], changes[1::2])), default=0)
        result[f'total_p{label}_s'] = round(float(np.sum(positive)), 4)
        result[f'run_p{label}_s'] = round(float(longest), 4)
    return result


def _sample_weights(samples):
    return np.minimum(VAD_STEP, samples - np.arange(0, samples, VAD_STEP)) / RATE


def _rms(audio):
    return round(float(np.sqrt(np.mean(audio ** 2))), 6)


def _audio_blocks(session, audio, measure):
    # Never give the offline detector context across a recorder discontinuity.
    breaks = {offset for offset, *_ in session.gaps()}
    cuts = [0, *(i for i, frame in enumerate(session.frames) if frame.offset in breaks), len(session.frames)]
    for first, end in zip(cuts, cuts[1:]):
        frames = session.frames[first:end]
        size = frames[-1].offset + frames[-1].samples - frames[0].offset
        origin = frames[0].offset
        scores = _offline(audio[origin:origin + size], measure)
        weights = _sample_weights(size)
        # VAD windows live on the WAV clock. Project their centres to each
        # frame's capture clock for comparisons to log/flight intervals.
        centers = origin + np.arange(0, size, VAD_STEP) + weights * RATE / 2
        offsets = np.array([frame.offset for frame in frames])
        indices = np.searchsorted(offsets, centers, side='right') - 1
        times = np.array([frame.start for frame in frames])[indices] + (centers - offsets[indices]) / RATE
        yield scores, weights, times, centers


def _window_audio(blocks, start, end):
    result = dict(pcm_s=0., vad_pcm_s=0.)
    for scores, weights, times in (block[:3] for block in blocks):
        partial = weights * ((start <= times) & (times < end))
        result['pcm_s'] += float(partial.sum())
        stats = _metrics(scores, partial)
        if stats:
            result['vad_pcm_s'] += float(partial.sum())
        for key, value in stats.items():
            result[key] = (result.get(key, 0.) + value if key.startswith('total')
                           else max(result.get(key, 0.), value))
    return {key: round(value, 6) for key, value in result.items()}


def _hold_intervals(flight):
    started = None
    for row in sorted(flight, key=lambda row: row['at']):
        if row['event'] == 'hold_recording.start':
            started = row['at']
        elif row['event'] == 'hold_recording.stop' and started is not None:
            yield started, row['at']
            started = None


def _speech_spans(session, blocks, shift, start, end, holds):
    clip_bounds = [(chunk['sample_offset'] - chunk['samples'], chunk['sample_offset'])
                   for chunk in session.chunks if 0 < chunk['samples'] <= chunk['sample_offset'] <= session.samples]
    spans, pauses = [], 0
    for scores, weights, times, centers in blocks:
        if scores is None:
            continue
        wall = times + shift
        positives = (scores > .5) & (start <= wall) & (wall < end)
        positions = np.flatnonzero(np.diff(np.r_[False, positives, False]))
        last_end = None
        for left, right in zip(positions[::2], positions[1::2]):
            width = weights[left:right]
            length = float(width.sum())
            if length < .1599:
                continue
            t0, t1 = wall[left] - width[0] / 2, wall[right - 1] + width[-1] / 2
            if last_end is not None and .5 <= t0 - last_end <= 3:
                pauses += 1
            last_end = t1
            saved = centers[left:right]
            timed = wall[left:right]
            queued = np.zeros(len(width), dtype=bool)
            held = np.zeros(len(width), dtype=bool)
            for a, b in clip_bounds:
                queued |= (a <= saved) & (saved < b)
            for a, b in holds:
                held |= (a <= timed) & (timed < b)
            spans.append(dict(start=float(t0), end=float(t1), scored_s=round(length, 4),
                              queued_estimate_s=round(float(width[queued].sum()), 4),
                              hold_s=round(float(width[held].sum()), 4),
                              outside_hold_and_queue_s=round(float(width[~(held | queued)].sum()), 4)))
    return spans, pauses


def _measure_chunk(chunk, session, audio, blocks, measure):
    offset, samples = chunk['sample_offset'], chunk['samples']
    end = chunk['speech_end_ns'] / 1e9
    onset = chunk['speech_start_ns']
    voice = chunk['last_voice_ns']
    result = dict(queued_s=samples / RATE, close_perf_s=end,
                  onset_to_close_s=round(end - onset / 1e9, 4) if _number(onset) else None,
                  last_voice_to_close_s=round(end - voice / 1e9, 4) if _number(voice) else None)
    start = onset / 1e9 if _number(onset) else end - samples / RATE
    result['ring_near_close'] = _window_audio(blocks, start - 1, end + 2)
    if _number(voice):
        result['ring_after_last_live_voice'] = _window_audio(blocks, voice / 1e9, end)
    frame_index = bisect_left(session.frames, offset, key=lambda frame: frame.offset) - 1
    if frame_index >= 0:
        frame = session.frames[frame_index]
        if offset == frame.offset + frame.samples and 0 <= end - frame.observed <= SYNC_S:
            closing = audio[frame.offset:offset]
            exact = closing[:len(closing) // VAD_STEP * VAD_STEP]
            result['closing_pcm_rms'] = _rms(closing)
            scores = _offline(exact, measure) if len(exact) else None
            result['closing_offline_peak'] = round(float(np.max(scores)), 6) if scores is not None else None
            if 0 < samples <= offset:
                # The journal carries extent, not membership. Report this as
                # an estimate even where the observed closing frame matches.
                pcm = audio[offset - samples:offset]
                result['clip_estimate'] = _metrics(_offline(pcm, measure), _sample_weights(samples))
                result['clip_estimate']['rms'] = _rms(pcm)
                result['clip_estimate']['timing_gaps'] = sum(offset - samples < gap[0] < offset
                                                           for gap in session.gaps())
    return result


def _survey(session, identifier, pairs, shift, start, end, holds, measure):
    coverage = [session.frames[0].start + shift, session.frames[-1].at + shift] if shift is not None else None
    row = dict(session=identifier, coverage=coverage, saved_s=session.samples / RATE,
               complete=session.complete, failure=session.failure, delivered_events=session.delivered,
               timed_chunks=sum(close is not None for _, close in pairs), journal_chunks=len(pairs))
    gaps = session.gaps()
    row['timing_gaps'] = len(gaps)
    row['gap_s'] = round(sum(max(0., right - left) for _, left, right, _, _ in gaps), 4)
    row['nonconsecutive_frames'] = sum(skip != 0 for _, _, _, skip, _ in gaps)
    row['epoch_changes'] = sum(epoch for _, _, _, _, epoch in gaps)
    if coverage is None or coverage[1] < start or coverage[0] > end:
        return row, []
    with wave.open(str(session.wav), 'rb') as wav:
        audio = np.frombuffer(wav.readframes(session.samples), dtype='<i2').astype(np.float32) / 32767.
    if len(audio) != session.samples:
        row['wav_short_read'] = True
        return row, []
    blocks = list(_audio_blocks(session, audio, measure))
    row['window_audio'] = _window_audio(blocks, start - shift, end - shift)
    row['speech_spans'], row['acoustic_pauses_05_to_3_s'] = _speech_spans(
        session, blocks, shift, start, end, holds)
    row['draft_counts'] = [dict(draft, at=draft['at'] + shift) for draft in session.drafts
                           if start <= draft['at'] + shift <= end]
    row['gaps_in_window'] = [[left + shift, right + shift] for _, left, right, _, _ in gaps
                             if right + shift >= start and left + shift <= end]
    return row, [(close, dict(session=identifier, **_measure_chunk(chunk, session, audio, blocks, measure)))
                 for chunk, close in pairs if close and start <= close['at'] <= end]


def analyse(logs, flight, sessions, start, end, zone, *, measure=None):
    """All inputs are explicit directories. Missing joins stay visible in output."""
    if measure is None:
        from samsara.speech_presence import measure
    logs, flight, sessions = map(safe_source, (logs, flight, sessions))
    log_events, coverage = _read_logs(logs, start, end, zone)
    # Calibrate session clocks on the surrounding days, including sessions that
    # have no queued chunk inside the requested window.
    flight_events, flight_closes, malformed = _read_flight(flight, start - 3600, end + 3600, zone)
    captures, unreadable = _sessions(sessions)
    holds = list(_hold_intervals(flight_events))
    mono = [t for t, _ in flight_closes]
    all_pairs = [[(chunk, _pair_chunk(chunk, flight_closes, mono)) for chunk in session.chunks]
                 for session in captures]
    anchors = [(chunk['speech_end_ns'] / 1e9, close['at'] - chunk['speech_end_ns'] / 1e9)
               for pairs in all_pairs for chunk, close in pairs if close]
    notes, meshes = [], {}
    for identifier, (session, pairs) in enumerate(zip(captures, all_pairs), 1):
        local = [close['at'] - chunk['speech_end_ns'] / 1e9 for chunk, close in pairs if close]
        nearby = [offset for at, offset in anchors if abs(at - session.frames[0].at) <= 300]
        offsets = local or nearby
        shift = median(offsets) if offsets and max(offsets) - min(offsets) <= SYNC_S else None
        if shift is not None:
            pairs = [(chunk, close or _pair_chunk(chunk, flight_closes, mono, shift))
                     for chunk, close in pairs]
        row, chunks = _survey(session, identifier, pairs, shift, start, end, holds, measure)
        row['clock_anchors'] = len(offsets)
        notes.append(row)
        for close, evidence in chunks:
            meshes.setdefault(close['at'], []).append(evidence)

    close_rows = sorted((close for _, close in flight_closes), key=lambda row: row['at'])
    silences = [row for row in log_events if row['event'] == 'silence']
    gates = [row for row in log_events if row['event'] == 'gate']
    lines = []
    for index, close in enumerate(close_rows):
        at = close['at']
        if not start <= at <= end:
            continue
        next_at = close_rows[index + 1]['at'] if index + 1 < len(close_rows) else float('inf')
        previous_at = close_rows[index - 1]['at'] if index else float('-inf')
        silence = _silence_for(silences, at, previous_at)
        if silence is None and at not in meshes:
            continue
        lines.append(dict(at=at, opened_at=close['opened_at'], buffer_s=close['buffer_s'],
                          silence=silence, gate=_nearest_gate(close, gates, next_at),
                          audio=meshes.get(at, [])))
    chronology = sorted(log_events + [row for row in flight_events if start <= row['at'] <= end],
                        key=lambda row: row['at'])
    return dict(window=[start, end], log_coverage=coverage, flight_malformed=malformed,
                unreadable_sessions=unreadable, sessions=notes, closes=lines, chronology=chronology)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('logs', 'flight', 'sessions'):
        parser.add_argument(f'--{name}', required=True, type=Path)
    for name in ('start', 'end'):
        parser.add_argument(f'--{name}', required=True, type=datetime.fromisoformat)
    args = parser.parse_args(argv)
    if (args.start.utcoffset() is None or args.start.utcoffset() != args.end.utcoffset()
            or args.end <= args.start):
        parser.error('a forward window with the same explicit UTC offset at both ends is required')
    roots = (args.logs, args.flight, args.sessions)
    try:
        roots = tuple(map(safe_source, roots))
    except ValueError:
        parser.error('live profiles are forbidden')
    if not all(root.is_dir() for root in roots):
        parser.error('all three input directories must exist')
    print(json.dumps(analyse(*roots, args.start.timestamp(), args.end.timestamp(),
                             args.start.tzinfo), allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
