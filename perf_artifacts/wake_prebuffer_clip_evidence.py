"""Queue 44 evidence: does prebuffer_policy=discard clip in-session one-word utterances?

Offline replay of the owner's real recorded voice through the wake-consumer onset rule.
Does not import dictation and does not change behaviour.

Live rule reproduced (samsara/audio_engine/wake_consumer.py, _process_frame):
  * ring frames are FRAME_MS = 100 ms (1600 samples at 16 kHz);
  * is_speech = any Silero probability > LIVE_VAD_PROB_THRESHOLD (0.5) over the frame's
    complete 512-sample windows (dictation._vad_probabilities / _vad_is_speech), with the
    same bundled silero_vad_v6.onnx the running build ships;
  * onset = first speech frame after a non-speech frame;
  * policy=discard with a post-wake admission: the onset frame is kept, the 15-frame
    (1.5 s) rewind is not -> retained buffer starts at the onset frame's first sample;
  * policy=keep / asleep: retained buffer starts 15 frames earlier.
Frame phase relative to speech is arbitrary live, so every clip is replayed at four
phase offsets (0, 25, 50, 75 ms) and the worst case is reported alongside phase 0.

Sources: ~/.samsara/debug/hotkey_*.wav (16 kHz mono, hotkey lane keeps its own 1.5 s
lead-in, so the true word start is inside the file). "Soft" rows are the same real
utterances attenuated by 12 dB (NOT re-spoken softly -- stated in the report).

    F:\\envs\\sami\\python.exe perf_artifacts\\wake_prebuffer_clip_evidence.py scan
    F:\\envs\\sami\\python.exe perf_artifacts\\wake_prebuffer_clip_evidence.py evidence
"""
import glob
import json
import os
import string
import sys
import wave

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_GLOB = os.path.expanduser('~/.samsara/debug/hotkey_*.wav')
SILERO = r'D:\Samsara-daily\_internal\faster_whisper\assets\silero_vad_v6.onnx'
SCAN_JSON = os.path.join(HERE, 'wake_prebuffer_scan.json')
OUT_JSON = os.path.join(HERE, 'wake_prebuffer_clip_evidence.json')
OUT_MD = os.path.join(HERE, 'wake_prebuffer_clip_evidence.md')
PLOTS = os.path.join(HERE, 'wake_prebuffer_clip_plots')
WAVS = os.path.join(HERE, 'wake_prebuffer_clip_wavs')

SR = 16000
FRAME = 1600            # FRAME_MS = 100
PREBUFFER_FRAMES = 15
LIVE_VAD_PROB_THRESHOLD = 0.5
PHASES_MS = (0, 25, 50, 75)
SOFT_DB = -12.0
CONTROL_WORDS = {'over', 'cancel', 'done', 'send', 'end', 'stop', 'submit', 'enter', 'cancel dictation'}
MAX_BYTES = 112044      # <= 3.5 s


def norm(text):
    return ' '.join(text.lower().split()).strip(string.punctuation + ' ')


def read_wav(path):
    with wave.open(path) as f:
        assert f.getframerate() == SR and f.getnchannels() == 1 and f.getsampwidth() == 2, path
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32767.0


def write_wav(path, audio):
    with wave.open(path, 'w') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SR)
        f.writeframes((audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes())


def whisper():
    from faster_whisper import WhisperModel
    return WhisperModel('medium', device='cuda', compute_type='float16')


def transcribe(model, audio):
    # performance_mode=balanced wake-lane params (get_transcription_params, include_vocabulary=False).
    segs, _info = model.transcribe(
        audio, language=None, beam_size=3, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        condition_on_previous_text=False, without_timestamps=True, word_timestamps=False,
        no_speech_threshold=0.6, log_prob_threshold=-1.0)
    return ''.join(s.text for s in segs).strip()


def scan():
    model = whisper()
    files = [p for p in sorted(glob.glob(SRC_GLOB)) if os.path.getsize(p) <= MAX_BYTES]
    hits = []
    for i, p in enumerate(files):
        text = transcribe(model, read_wav(p))
        n = norm(text)
        if n and len(n.split()) <= 2 and (n in CONTROL_WORDS or n.split()[-1] in CONTROL_WORDS):
            hits.append({'file': os.path.basename(p), 'text': n})
        if i % 100 == 0:
            print(f'  {i}/{len(files)} scanned, {len(hits)} control-word clips', flush=True)
    json.dump({'scanned': len(files), 'hits': hits}, open(SCAN_JSON, 'w'), indent=1)
    print(f'scanned {len(files)}, control-word clips {len(hits)}')


class Vad:
    def __init__(self):
        from faster_whisper.vad import SileroVADModel
        self.m = SileroVADModel(SILERO)

    def frame_is_speech(self, frame):
        usable = (frame.size // 512) * 512
        probs = np.asarray(self.m(np.ascontiguousarray(frame[:usable], dtype=np.float32)), dtype=np.float32).reshape(-1)
        return bool(np.any(probs > LIVE_VAD_PROB_THRESHOLD)), float(probs.max()) if probs.size else 0.0


def energy_onset(audio):
    """True word start from the waveform itself: 20 ms windows, 5 ms hop; noise floor is
    the 20th percentile of the first 400 ms; onset = first window > floor * 10 dB (and >
    0.0015) that stays above it for 30 ms."""
    win, hop = 320, 80
    rms = np.array([np.sqrt(np.mean(audio[i:i + win] ** 2)) for i in range(0, len(audio) - win, hop)])
    floor = float(np.percentile(rms[:int(0.4 * SR / hop)], 20))
    thr = max(floor * 3.162, 0.0015)
    for k in range(len(rms) - 6):
        if np.all(rms[k:k + 6] > thr):
            return k * hop, floor, thr
    return None, floor, thr


def onset_frame(vad, audio, phase_samples):
    """Replay 100 ms ring frames starting at phase_samples; return onset frame start sample."""
    was = False
    for start in range(phase_samples, len(audio) - FRAME + 1, FRAME):
        speech, _p = vad.frame_is_speech(audio[start:start + FRAME])
        if speech and not was:
            return start
        was = speech
    return None


def plot(path, audio, word_start, cut, title):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    a = max(0, word_start - int(0.25 * SR)); b = min(len(audio), word_start + int(0.6 * SR))
    t = (np.arange(a, b) - word_start) / SR * 1000
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 4.6), sharex=True)
    ax1.plot(t, audio[a:b], lw=0.6, color='#333')
    ax1.axvspan(t[0], (cut - word_start) / SR * 1000, color='#e66', alpha=0.25, label='discarded (policy=discard)')
    ax1.axvline(0, color='#16a', lw=1, label='energy word start')
    ax1.axvline((cut - word_start) / SR * 1000, color='#c00', lw=1.2, label='retained buffer starts')
    ax1.legend(loc='upper right', fontsize=7); ax1.set_title(title, fontsize=9)
    ax2.specgram(audio[a:b], NFFT=320, Fs=SR, noverlap=240, cmap='magma',
                 xextent=(t[0], t[-1]))
    ax2.axvline((cut - word_start) / SR * 1000, color='#0f0', lw=1.2)
    ax2.axvline(0, color='#0ff', lw=1)
    ax2.set_ylim(0, 6000); ax2.set_xlabel('ms relative to energy word start'); ax2.set_ylabel('Hz')
    fig.tight_layout(); fig.savefig(path, dpi=90); plt.close(fig)


def evidence():
    os.makedirs(PLOTS, exist_ok=True); os.makedirs(WAVS, exist_ok=True)
    hits = json.load(open(SCAN_JSON))['hits']
    # Most recent first, one-word end/cancel words preferred.
    hits = sorted(hits, key=lambda h: h['file'], reverse=True)
    vad, model = Vad(), whisper()
    rows = []
    for h in hits:
        base = read_wav(os.path.join(os.path.dirname(SRC_GLOB), h['file']))
        for variant, gain in (('normal', 1.0), ('soft -12 dB', 10 ** (SOFT_DB / 20))):
            audio = base * gain
            said = norm(transcribe(model, audio)) if variant == 'normal' else h['text']
            ws, floor, thr = energy_onset(audio)
            if ws is None or ws < int(0.3 * SR):
                continue          # no clean lead-in to measure against
            per_phase = []
            for ph in PHASES_MS:
                cut = onset_frame(vad, audio, int(ph * SR / 1000))
                if cut is None:
                    per_phase.append({'phase_ms': ph, 'onset': None}); continue
                per_phase.append({'phase_ms': ph, 'cut': cut, 'speech_lost_ms': (cut - ws) / SR * 1000,
                                  'discarded_ahead_ms': min(PREBUFFER_FRAMES * FRAME, cut) / SR * 1000})
            valid = [p for p in per_phase if p.get('cut') is not None]
            if not valid:
                rows.append({'file': h['file'], 'variant': variant, 'said': h['text'], 'onset': 'never'}); continue
            p0 = valid[0]
            worst = max(valid, key=lambda p: p['speech_lost_ms'])
            out = {'file': h['file'], 'variant': variant, 'said': h['text'], 'full_transcript': said,
                   'noise_floor_rms': floor, 'word_start_ms': ws / SR * 1000, 'phases': per_phase}
            for tag, p in (('p0', p0), ('worst', worst)):
                kept = audio[p['cut']:]
                first20 = float(np.sqrt(np.mean(kept[:320] ** 2)))
                out[tag] = {
                    'phase_ms': p['phase_ms'], 'discarded_ahead_ms': p['discarded_ahead_ms'],
                    'speech_lost_ms': p['speech_lost_ms'],
                    'first20ms_rms': first20, 'starts_in_speech_energy': first20 > thr,
                    'buffer_rms': float(np.sqrt(np.mean(kept ** 2))),
                    'transcript_discard': transcribe(model, kept),
                }
                keep_start = max(0, p['cut'] - PREBUFFER_FRAMES * FRAME)
                out[tag]['transcript_keep'] = transcribe(model, audio[keep_start:])
                stem = f"{h['file'][:-4]}_{'soft' if gain != 1 else 'normal'}_{tag}"
                write_wav(os.path.join(WAVS, stem + '_retained_discard.wav'), kept)
                plot(os.path.join(PLOTS, stem + '.png'), audio, ws, p['cut'],
                     f"{h['file']} [{variant}] said '{h['text']}', phase {p['phase_ms']} ms, "
                     f"lost {p['speech_lost_ms']:.0f} ms -> '{out[tag]['transcript_discard']}'")
            rows.append(out)
            print(f"{h['file']} [{variant}] '{h['text']}' p0 lost {p0['speech_lost_ms']:.0f}ms -> "
                  f"'{out['p0']['transcript_discard']}' | worst lost {worst['speech_lost_ms']:.0f}ms -> "
                  f"'{out['worst']['transcript_discard']}' | keep '{out['worst']['transcript_keep']}'", flush=True)
    json.dump(rows, open(OUT_JSON, 'w'), indent=1)
    write_md(rows)


def matches(said, text):
    t = norm(text)
    if said.startswith('cancel'):
        return 'cancel' in t                          # wake_abort_phrase: substring
    toks = text.strip().split()
    return bool(toks) and toks[-1].rstrip('.,!?').lower() == said.split()[-1]   # send word: exact last token


def write_md(rows):
    lines = ['# Queue 44: prebuffer discard vs in-session one-word utterances (offline replay)', '',
             'Owner voice from ~/.samsara/debug/hotkey_*.wav, replayed through the live onset rule '
             '(100 ms frames, silero_vad_v6.onnx, prob > 0.5). "discarded ahead" = rewind audio policy=discard '
             'drops; "speech lost" = ms between the waveform word start and the retained buffer start '
             '(negative = retained buffer starts before the word). "Starts mid-word" = first 20 ms of the '
             'retained buffer is above the +10 dB speech-energy threshold, checked by eye on the plots. '
             'Soft rows are the same clips attenuated 12 dB, not re-spoken.', '',
             '| # | clip | variant | said | phase | discarded ahead ms | speech lost ms | starts mid-word | buffer RMS | discard transcript | match | keep transcript | match |',
             '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    n = 0
    for r in rows:
        if 'p0' not in r:
            continue
        for tag in ('p0', 'worst'):
            if tag == 'worst' and r['worst']['phase_ms'] == r['p0']['phase_ms']:
                continue
            x = r[tag]; n += 1
            lines.append(f"| {n} | {r['file'][7:22]} | {r['variant']} | {r['said']} | {x['phase_ms']}{' (worst)' if tag == 'worst' else ''} | "
                         f"{x['discarded_ahead_ms']:.0f} | {x['speech_lost_ms']:.0f} | {'yes' if x['starts_in_speech_energy'] else 'no'} | "
                         f"{x['buffer_rms']:.4f} | {x['transcript_discard'] or '(empty)'} | {'Y' if matches(r['said'], x['transcript_discard']) else 'N'} | "
                         f"{x['transcript_keep'] or '(empty)'} | {'Y' if matches(r['said'], x['transcript_keep']) else 'N'} |")
    open(OUT_MD, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print(f'wrote {OUT_MD} ({n} rows)')


ONSETS_JSON = os.path.join(HERE, 'wake_prebuffer_onsets.json')
ONSETS_MD = os.path.join(HERE, 'wake_prebuffer_onsets.md')


def first_word(text):
    toks = norm(text).split()
    return toks[0].strip(string.punctuation) if toks else ''


def onsets(limit=40):
    """Mechanism check on real owner onsets (no control words on disk): for recent hotkey
    clips with >= 300 ms clean lead-in, where does the discard boundary fall relative to the
    word start, and does the FIRST word survive? Stores only the first word, never the
    dictated sentence."""
    os.makedirs(PLOTS, exist_ok=True)
    vad, model = Vad(), whisper()
    rows = []
    for p in sorted(glob.glob(SRC_GLOB), reverse=True):
        if len(rows) >= limit:
            break
        if os.path.getsize(p) > 20 * SR * 2:
            continue
        base = read_wav(p)
        ws, floor, thr = energy_onset(base)
        if ws is None or ws < int(0.3 * SR):
            continue
        ref = first_word(transcribe(model, base[:ws + int(2.5 * SR)]))
        if not ref:
            continue
        for variant, gain in (('normal', 1.0), ('soft -12 dB', 10 ** (SOFT_DB / 20))):
            audio = base * gain
            per = []
            for ph in PHASES_MS:
                cut = onset_frame(vad, audio, int(ph * SR / 1000))
                if cut is None:
                    per.append(None); continue
                kept = audio[cut:cut + int(2.5 * SR)]
                first20 = float(np.sqrt(np.mean(kept[:320] ** 2)))
                per.append({'phase_ms': ph, 'speech_lost_ms': (cut - ws) / SR * 1000,
                            'starts_in_speech_energy': first20 > thr * gain if gain != 1 else first20 > thr,
                            'buffer_rms': float(np.sqrt(np.mean(audio[cut:] ** 2))), 'cut': cut})
            valid = [x for x in per if x]
            if not valid:
                rows.append({'file': os.path.basename(p), 'variant': variant, 'ref_first_word': ref, 'onset': 'never'})
                continue
            worst = max(valid, key=lambda x: x['speech_lost_ms'])
            got = first_word(transcribe(model, audio[worst['cut']:worst['cut'] + int(2.5 * SR)]))
            # Control: same frame phase and same buffer end, but policy=keep (15-frame rewind).
            keep_start = max(0, worst['cut'] - PREBUFFER_FRAMES * FRAME)
            got_keep = first_word(transcribe(model, audio[keep_start:worst['cut'] + int(2.5 * SR)]))
            rows.append({'file': os.path.basename(p), 'variant': variant, 'ref_first_word': ref,
                         'lost_ms_by_phase': [round(x['speech_lost_ms']) if x else None for x in per],
                         'worst': {k: v for k, v in worst.items() if k != 'cut'}, 'discard_first_word_worst': got,
                         'keep_first_word_worst': got_keep})
            if worst['speech_lost_ms'] > 0 and variant == 'normal' or got != ref:
                plot(os.path.join(PLOTS, f"onset_{os.path.basename(p)[:-4]}_{'soft' if gain != 1 else 'normal'}.png"),
                     audio, ws, worst['cut'], f"{os.path.basename(p)} [{variant}] first word '{ref}', "
                     f"phase {worst['phase_ms']} lost {worst['speech_lost_ms']:.0f} ms -> '{got}'")
            print(f"{os.path.basename(p)} [{variant}] ref '{ref}' lost {rows[-1]['lost_ms_by_phase']} -> '{got}'", flush=True)
    json.dump(rows, open(ONSETS_JSON, 'w'), indent=1)
    ok = [r for r in rows if 'worst' in r]
    lines = ['# Queue 44: where the discard boundary lands on the owner\'s real speech onsets (offline replay)', '',
             'First word of recent hotkey clips (no isolated end words exist on disk). Speech lost = ms from the '
             'waveform word start to the retained-buffer start under policy=discard, at frame phases 0/25/50/75 ms. '
             'Discarded ahead of the onset is always the full 1500 ms rewind. First word re-transcribed from the '
             'worst-phase retained buffer.', '',
             '| clip | variant | lost ms by phase | worst starts mid-word | buffer RMS | first word (full) | first word (discard, worst) | same | first word (keep, same window) | same |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for r in ok:
        w = r['worst']
        lines.append(f"| {r['file'][7:22]} | {r['variant']} | {r['lost_ms_by_phase']} | {'yes' if w['starts_in_speech_energy'] else 'no'} | "
                     f"{w['buffer_rms']:.4f} | {r['ref_first_word']} | {r['discard_first_word_worst'] or '(empty)'} | "
                     f"{'Y' if r['ref_first_word'] == r['discard_first_word_worst'] else 'N'} | "
                     f"{r['keep_first_word_worst'] or '(empty)'} | {'Y' if r['ref_first_word'] == r['keep_first_word_worst'] else 'N'} |")
    for variant in ('normal', 'soft -12 dB'):
        vs = [r for r in ok if r['variant'] == variant]
        if vs:
            lost = [r['worst']['speech_lost_ms'] for r in vs]
            lines += ['', f"**{variant}:** n={len(vs)}; worst-phase speech lost median {np.median(lost):.0f} ms, "
                      f"max {max(lost):.0f} ms; boundary after word start in {sum(l > 0 for l in lost)}/{len(vs)}; "
                      f"retained buffer starts mid-word in {sum(r['worst']['starts_in_speech_energy'] for r in vs)}/{len(vs)}; "
                      f"first word changed under discard in {sum(r['ref_first_word'] != r['discard_first_word_worst'] for r in vs)}/{len(vs)}, "
                      f"under keep (control) in {sum(r['ref_first_word'] != r['keep_first_word_worst'] for r in vs)}/{len(vs)}."]
    open(ONSETS_MD, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print(f'wrote {ONSETS_MD}')


LIVE_DIR = os.path.expanduser('~/.samsara-daily/debug_audio')
LIVE_JSON = os.path.join(HERE, 'wake_prebuffer_live_session.json')
LIVE_MD = os.path.join(HERE, 'wake_prebuffer_live_session.md')
# 2026-09-14 20:26 live session (owner script, daily build with the wake-gate fix,
# debug_dump_wake_audio=true). In-session dumps mapped by timestamp to the [WAKE-GATE] /
# [HEAR] / [END] / [CANCEL] log lines (reports/44/artifacts/live_log_extract.txt).
LIVE_ROWS = [
    ('wake_202818_307048.wav', 'R1', 'normal', 'this is round one, over', 'This is round one, over.', 'END over', 'pass', 1710.9),
    ('wake_202842_802484.wav', 'R2', 'normal', 'this is round two, cancel', 'This is round two, cancel.', 'CANCEL', 'pass', 1677.4),
    ('wake_202851_696225.wav', 'R3', 'normal', 'over', 'Over.', 'END over', 'pass', 1686.4),
    ('wake_202912_689871.wav', 'R4', 'normal', 'this is round four', 'This is round four.', 'delivered (1.0 s quick timeout)', 'pass', 1656.8),
    ('wake_202924_796701.wav', 'R5', 'normal', 'this is round five', 'This is round five.', 'delivered (1.0 s quick timeout)', 'pass', 1672.1),
    ('wake_202926_210456.wav', 'R5', 'soft', 'cancel', '(not transcribed)', 'skipped: in_session_near_silence rms 0.0016 < 0.0020', 'skip', None),
    ('wake_202953_704197.wav', 'R6', 'soft', 'over', 'over.', 'END over', 'pass', 1668.9),
    ('wake_203012_895987.wav', 'R7', 'soft', 'cancel', 'cancel.', 'CANCEL', 'pass', 1662.1),
    ('wake_203021_401262.wav', 'R8', 'normal', 'done', 'done.', 'END done', 'pass', 1666.4),
]


def live():
    """Inspect the in-session retained buffers themselves. Under policy=discard the dump starts
    at the onset frame (after any post-wake guard trim), so its first samples ARE the retained
    buffer start. Mid-word = the first 20 ms already carry speech-level energy relative to the
    buffer's own quiet tail (quietest 100 ms), confirmed on the plots."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    os.makedirs(PLOTS, exist_ok=True)
    vad, model = Vad(), whisper()
    rows = []
    for name, rnd, level, said, heard, outcome, gate, discarded in LIVE_ROWS:
        a = read_wav(os.path.join(LIVE_DIR, name))
        win = 320
        rms20 = np.array([np.sqrt(np.mean(a[i:i + win] ** 2)) for i in range(0, len(a) - win, 160)])
        quiet = float(min(np.sqrt(np.mean(a[i:i + 1600] ** 2)) for i in range(0, len(a) - 1600, 800)))
        peak20 = float(rms20.max())
        first20 = float(rms20[0])
        # Silero on the first ring frame of the dump (the onset frame the live consumer kept).
        speech0, p0 = vad.frame_is_speech(a[:FRAME])
        redo = transcribe(model, a)
        row = {'file': name, 'round': rnd, 'level': level, 'said': said, 'heard_live': heard, 'outcome': outcome,
               'gate': gate, 'discarded_ms_logged': discarded, 'duration_s': len(a) / SR,
               'buffer_rms': float(np.sqrt(np.mean(a ** 2))), 'quiet_100ms_rms': quiet, 'peak_20ms_rms': peak20,
               'first_20ms_rms': first20, 'first20_over_quiet_db': 20 * np.log10(first20 / max(quiet, 1e-6)),
               'first20_over_peak_db': 20 * np.log10(first20 / peak20), 'onset_frame_silero_max': p0,
               'retranscribed': redo}
        rows.append(row)
        t = np.arange(min(len(a), int(0.7 * SR))) / SR * 1000
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 4.4), sharex=True)
        ax1.plot(t, a[:len(t)], lw=0.6, color='#333')
        ax1.axvline(20, color='#c00', lw=0.8)
        ax1.set_title(f"{name} {rnd} {level} said '{said}' | live heard '{heard}' | first 20 ms "
                      f"{row['first20_over_quiet_db']:+.1f} dB vs quiet, {row['first20_over_peak_db']:+.1f} dB vs peak", fontsize=8)
        ax2.specgram(a[:len(t)], NFFT=320, Fs=SR, noverlap=240, cmap='magma', xextent=(0, t[-1]))
        ax2.set_ylim(0, 6000); ax2.set_xlabel('ms from start of retained buffer (policy=discard)'); ax2.set_ylabel('Hz')
        fig.tight_layout(); fig.savefig(os.path.join(PLOTS, f'live_{rnd}_{level}_{name[:-4]}.png'), dpi=90); plt.close(fig)
        print(f"{rnd} {level:<6} '{said}' heard '{heard}' rms {row['buffer_rms']:.4f} first20 {row['first20_over_quiet_db']:+.1f} dB vs quiet "
              f"{row['first20_over_peak_db']:+.1f} dB vs peak, onset-frame silero {p0:.2f}, re-transcribed '{redo}'", flush=True)
    json.dump(rows, open(LIVE_JSON, 'w'), indent=1)


if __name__ == '__main__':
    {'scan': scan, 'evidence': evidence, 'onsets': onsets, 'live': live,
     'md': lambda: write_md(json.load(open(OUT_JSON)))}[sys.argv[1]]()
