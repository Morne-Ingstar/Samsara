"""Queue 44 run 2: why did hands-free DICTATE stage " nd" for a spoken "and"?

Two competing explanations for the 2026-09-15 14:09-14:12 live incident:

  H1 (brief): capture clipped the first phoneme of a short standalone word.
  H2 (log):   the DICTATE lane's initial_prompt (last 200 chars of the pending
              buffer, dictation.py _handle_command_mode_utterance) steered
              Whisper to the fragment "nd"/"ndows", which then fed back into
              every later prompt.

Offline, on the owner's own recorded voice. Does NOT import dictation and does
not change behaviour.

Audio: ~/.samsara/debug/hotkey_*.wav whose logged [OK] transcript starts with
"And". The hotkey lane keeps its lead-in, so the word start is inside the file.
Each word is cut at its energy onset .. Whisper word end, then shaped like a
toggle-DICTATE lane buffer (wake_consumer.py): 500 ms of the clip's own lead-in
+ word + 700 ms of the clip's own room tone (rewind(6) = 5 pre frames + onset
frame; 0.65 s dictate silence closes the chunk).

Decode: the owner's exact lane params (performance_mode=accurate, then the
lane's overrides language='en', vad_filter=False, initial_prompt=context tail).

Prompts: the real 200-char tails, rebuilt from the live log's staged texts and
checked against every logged pending_chars.

    F:\\envs\\sami\\python.exe perf_artifacts\\nd_prompt_contamination.py
"""
import ast
import collections
import json
import os
import re
import string
import sys
import wave

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser('~/.samsara')
LOGS = [os.path.join(HOME, 'logs', 'samsara.log.1'), os.path.join(HOME, 'logs', 'samsara.log')]
OUT_JSON = os.path.join(HERE, 'nd_prompt_contamination.json')
OUT_MD = os.path.join(HERE, 'nd_prompt_contamination.md')
WAVS = os.path.join(HERE, 'nd_prompt_contamination_wavs')

SR = 16000
LEAD_S = 0.5
TAIL_S = 0.7
MAX_CLIPS = 24
CLIP_CUTS_MS = (40, 80, 120)
SESSION_DAY = '2026-09-15'
SESSION_FROM, SESSION_TO = '14:08:37', '14:12:30'
# Utterances whose prompt tail we rebuild, keyed by the staged line's time.
# Value: what the live decode produced for a standalone "and".
TAIL_POINTS = {
    '14:09:20': 'and..',   # standalone "and", correct
    '14:11:36': 'and',     # standalone "and", correct
    '14:11:40': 'nd',      # first " nd" -- 4 s after the correct one
    '14:11:45': 'nd',
    '14:11:55': 'nd',
}


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


# -- prompt tails from the live log ------------------------------------------

def rebuild_tails():
    """Replay dictate_staged texts; verify pending_chars at every step."""
    lines = []
    for lf in LOGS:
        for line in open(lf, encoding='utf-8', errors='replace'):
            if line.startswith(SESSION_DAY) and 'outcome=dictate_staged' in line:
                t = line[11:19]
                if SESSION_FROM <= t <= SESSION_TO:
                    lines.append((t, line))
    buf = ''
    tails, checks = {}, []
    for t, line in lines:
        detail = ast.literal_eval(line[line.index('detail=') + 7:].strip())
        text, pending = detail['text'], detail['pending_chars']
        prompt_before = buf[-200:]
        if pending == len(text.strip()):
            buf = text.strip()                       # first chunk of a fresh buffer
        elif len(buf) + len(text) == pending:
            buf += text
        elif buf.rstrip().endswith('.') and len(buf.rstrip()) - 1 + len(text) == pending:
            buf = buf.rstrip()[:-1] + text           # continuation strips one full stop
        else:
            raise SystemExit(f'pending_chars mismatch at {t}: have {len(buf)} + {len(text)} != {pending}')
        checks.append((t, pending, len(buf)))
        if t in TAIL_POINTS:
            tails[t] = prompt_before
    missing = set(TAIL_POINTS) - set(tails)
    if missing:
        raise SystemExit(f'tail points not found: {sorted(missing)}')
    return tails, checks


# -- clips -------------------------------------------------------------------

def and_clips():
    pairs = []
    for lf in LOGS:
        lines = open(lf, encoding='utf-8', errors='replace').read().splitlines()
        for i, line in enumerate(lines):
            m = re.search(r'Dumped hotkey buffer -> (\S+\.wav)', line)
            if not m:
                continue
            for j in range(i + 1, min(i + 8, len(lines))):
                if 'Dumped hotkey buffer' in lines[j]:
                    break
                k = lines[j].find('[OK] ')
                if k >= 0:
                    pairs.append((m.group(1), lines[j][k + 5:].strip()))
                    break
    return [(w, t) for w, t in pairs if re.match(r'^and\b', t, re.I) and os.path.exists(w)]


WIN, HOP = 320, 80          # 20 ms windows, 5 ms hop
EARCON_END_S = 0.40         # hotkey start earcon sits at ~0.2-0.3 s in these dumps
MAX_ROOM_RMS = 0.02         # exclude clips that open mid-speech / on loud background
MIN_WORD_MS, MAX_WORD_MS = 150, 550


def envelope(audio):
    return np.array([np.sqrt(np.mean(audio[i:i + WIN] ** 2)) for i in range(0, len(audio) - WIN, HOP)])


def locate_and(audio):
    """(room_start, onset, word_end) in samples, or None.

    Room = quietest 300 ms after the earcon and before speech. Onset = first
    window after the earcon above max(room x 10 dB, 0.004) sustained 30 ms.
    Provisional word end = deepest envelope dip between onset+MIN_WORD_MS and
    onset+MAX_WORD_MS; main() refines it with Whisper word timestamps."""
    env = envelope(audio)
    k0 = int(EARCON_END_S * SR / HOP)
    room_w = int(0.3 * SR / HOP)
    # onset first with a provisional floor, then the room before it
    floor = float(np.percentile(env[k0:k0 + int(0.3 * SR / HOP)], 20)) if len(env) > k0 + room_w else 1.0
    if floor > MAX_ROOM_RMS:
        return None
    thr = max(floor * 3.162, 0.004)
    onset_k = next((k for k in range(k0, len(env) - 6) if np.all(env[k:k + 6] > thr)), None)
    if onset_k is None or (onset_k - k0) < room_w:
        return None
    means = [env[k:k + room_w].mean() for k in range(k0, onset_k - room_w - 10)]
    if not means:
        return None
    room_k = k0 + int(np.argmin(means))
    lo, hi = onset_k + int(MIN_WORD_MS * SR / 1000 / HOP), onset_k + int(MAX_WORD_MS * SR / 1000 / HOP)
    if hi >= len(env):
        return None
    end_k = lo + int(np.argmin(env[lo:hi]))
    return room_k * HOP, onset_k * HOP, end_k * HOP + WIN // 2


def room_tone(room, n):
    reps = int(np.ceil(n / max(len(room), 1)))
    return np.tile(room, reps)[:n]


# -- decode ------------------------------------------------------------------

def lane_params(prompt):
    # get_transcription_params(include_vocabulary=False), performance_mode=accurate
    # (owner config), + _handle_command_mode_utterance overrides.
    p = dict(language='en', beam_size=5, vad_filter=False, condition_on_previous_text=True,
             without_timestamps=False, word_timestamps=False,
             no_speech_threshold=0.6, log_prob_threshold=-1.0)
    if prompt:
        p['initial_prompt'] = prompt
    return p


def decode(model, audio, prompt):
    segs, _ = model.transcribe(audio, **lane_params(prompt))
    return ''.join(s.text for s in segs).strip()


def bucket(text):
    n = norm(text)
    if n == 'and':
        return 'and'
    if n == 'nd' or n.startswith('nd'):
        return 'nd'
    if not n:
        return 'empty'
    return 'other'


SAPI_DIR = os.path.join(HERE, 'nd_prompt_contamination_sapi')
ROOM_FLOOR_RMS = 0.00125    # owner's calibrated room floor (queue 67: 0.00125 vs 0.00118 independent)
SAPI_LEVELS = {'normal': 0.03, 'soft': 0.03 / 3.981}   # soft = -12 dB


def run_arms(model, row, pre, word, tail, tails, base):
    """Same token through every arm; WAVs kept for inspection."""
    intact = np.concatenate([pre, word, tail]).astype(np.float32)
    write_wav(os.path.join(WAVS, f'{base}_intact.wav'), intact)
    row['decodes']['intact/no_prompt'] = decode(model, intact, None)
    for t, tail_text in tails.items():
        row['decodes'][f'intact/tail_{t}'] = decode(model, intact, tail_text)
    for cut in CLIP_CUTS_MS:
        # "clipped" = the brief's mechanism: buffer begins cut ms INTO the word.
        clipped = np.concatenate([word[int(cut * SR / 1000):], tail]).astype(np.float32)
        write_wav(os.path.join(WAVS, f'{base}_clip{cut}.wav'), clipped)
        row['decodes'][f'clip{cut}/no_prompt'] = decode(model, clipped, None)
        row['decodes'][f'clip{cut}/tail_14:11:36'] = decode(model, clipped, tails['14:11:36'])
    print(f"{row['source']} {row['clip']} word {row['word_ms']}ms: " +
          ' | '.join(f'{k}={v!r}' for k, v in row['decodes'].items()), flush=True)
    return row


def owner_tokens(model, skipped):
    for path, logged in reversed(and_clips()):
        audio = read_wav(path)
        loc = locate_and(audio)
        if loc is None:
            skipped['owner:no_quiet_room_or_onset'] += 1
            continue
        room_start, onset, dip_end = loc
        # Word end from Whisper word timestamps on a window that starts just
        # before the onset (so the first word's timing is meaningful).
        w0 = onset - int(0.1 * SR)
        segs, _ = model.transcribe(audio[w0:onset + int(1.5 * SR)], language='en', beam_size=5,
                                   word_timestamps=True, vad_filter=False,
                                   condition_on_previous_text=False)
        words = [w for s in segs for w in (s.words or [])]
        if not words or norm(words[0].word) != 'and':
            skipped['owner:first_word_not_and'] += 1
            continue
        word_end = w0 + int((words[0].end + 0.03) * SR)
        if len(words) > 1:
            word_end = min(word_end, w0 + int(words[1].start * SR))
        word_end = min(word_end, onset + int(MAX_WORD_MS * SR / 1000))
        if word_end - onset < int(MIN_WORD_MS * SR / 1000):
            word_end = dip_end
        room = audio[room_start:room_start + int(0.3 * SR)]
        word = audio[onset:word_end]
        pre = np.concatenate([room_tone(room, int(LEAD_S * SR) - int(0.1 * SR)),
                              audio[onset - int(0.1 * SR):onset]])
        tail = room_tone(room, int(TAIL_S * SR))
        # Validity: the cut must be heard as exactly "and" with no prompt.
        if norm(decode(model, np.concatenate([pre, word, tail]).astype(np.float32), None)) != 'and':
            skipped['owner:cut_not_heard_as_and_without_prompt'] += 1
            continue
        yield ({'source': 'owner', 'clip': os.path.basename(path), 'logged': logged[:60],
                'word_ms': round((word_end - onset) * 1000 / SR),
                'room_rms': round(float(np.sqrt(np.mean(room ** 2))), 5),
                'word_rms': round(float(np.sqrt(np.mean(word ** 2))), 5), 'decodes': {}},
               pre, word, tail, os.path.basename(path)[:-4])


def sapi_tokens():
    rng = np.random.default_rng(44)

    def noise(n):
        return (rng.standard_normal(n) * ROOM_FLOOR_RMS).astype(np.float32)

    for path in sorted(os.listdir(SAPI_DIR)):
        audio = read_wav(os.path.join(SAPI_DIR, path))
        env = envelope(audio)
        live = np.flatnonzero(env > env.max() * 0.02)
        word = audio[live[0] * HOP:live[-1] * HOP + WIN]
        for level, target in SAPI_LEVELS.items():
            w = (word * (target / float(np.sqrt(np.mean(word ** 2))))).astype(np.float32)
            w = w + noise(len(w))
            yield ({'source': f'sapi_{level}', 'clip': path, 'word_ms': round(len(w) * 1000 / SR),
                    'room_rms': ROOM_FLOOR_RMS, 'word_rms': round(target, 5), 'decodes': {}},
                   noise(int(LEAD_S * SR)), w, noise(int(TAIL_S * SR)), f'sapi_{path[:-4]}_{level}')


def main():
    tails, checks = rebuild_tails()
    print(f'rebuilt {len(checks)} staged steps; pending_chars verified at every one')
    for t, v in tails.items():
        print(f'  tail {t} (live -> {TAIL_POINTS[t]!r}): ...{v[-70:]!r}')

    from faster_whisper import WhisperModel
    model = WhisperModel('medium', device='cuda', compute_type='float16')
    os.makedirs(WAVS, exist_ok=True)

    rows, skipped = [], collections.Counter()
    for row, pre, word, tail, base in owner_tokens(model, skipped):
        rows.append(run_arms(model, row, pre, word, tail, tails, base))
    for row, pre, word, tail, base in sapi_tokens():
        rows.append(run_arms(model, row, pre, word, tail, tails, base))
    print(f'rows {len(rows)}; skipped {dict(skipped)}')

    sources = sorted({r['source'] for r in rows})
    arms = list(rows[0]['decodes']) if rows else []
    summary = {}
    for src in sources + ['ALL']:
        sub = [r for r in rows if src == 'ALL' or r['source'] == src]
        summary[src] = {'n': len(sub)}
        for arm in arms:
            c = collections.Counter(bucket(r['decodes'][arm]) for r in sub)
            summary[src][arm] = {k: c.get(k, 0) for k in ('and', 'nd', 'other', 'empty')}
    json.dump({'tails': tails, 'tail_live_result': TAIL_POINTS, 'pending_checks': checks,
               'rows': rows, 'summary': summary, 'skipped': dict(skipped)},
              open(OUT_JSON, 'w', encoding='utf-8'), indent=1)

    md = ['# Queue 44 run 2: "and" -> "nd" -- prompt tail vs onset clipping', '',
          'Each token goes through every arm unchanged, so arms compare like with like. '
          'Lane buffer shape: 500 ms lead-in + word + 700 ms tail (toggle-DICTATE). Decode = owner '
          'lane params (performance_mode=accurate, language=en, vad_filter=False, beam 5). Prompt '
          f'tails rebuilt from the 14:08:37-14:12:30 live log; pending_chars verified at all {len(checks)} '
          'staged steps.', '',
          '- **owner**: real word-initial "and" from ~/.samsara/debug hotkey dumps, kept only if the cut '
          'decodes to exactly "and" with no prompt.',
          '- **sapi_normal / sapi_soft**: Windows SAPI "and" (6 voices x 3 rates), word RMS 0.03 / -12 dB, '
          f'white room noise at the owner calibrated floor {ROOM_FLOOR_RMS}. Synthetic voice: tests the '
          'decoder mechanism, not the owner accent.',
          "- **clipN**: buffer starts N ms into the word (the brief's hypothesis), no lead-in.", '']
    for src in sources + ['ALL']:
        md += [f"## {src} (n={summary[src]['n']})", '', '| arm | and | nd | other | empty |',
               '|---|---|---|---|---|']
        for arm in arms:
            s = summary[src][arm]
            md.append(f"| {arm} | {s['and']} | {s['nd']} | {s['other']} | {s['empty']} |")
        md.append('')
    md += ['## Prompt tails (last 200 chars of the pending buffer before the utterance)', '']
    for t, v in tails.items():
        md.append(f'- **{t}** (live decode of a standalone "and": `{TAIL_POINTS[t]}`): `...{v[-90:]}`')
    md += ['', '## Per token', '']
    for r in rows:
        md.append(f"- [{r['source']}] `{r['clip']}` word {r['word_ms']} ms, rms {r['word_rms']}: " +
                  '; '.join(f'{k} `{v}`' for k, v in r['decodes'].items()))
    md += ['', f'Skipped owner clips: {dict(skipped)}']
    open(OUT_MD, 'w', encoding='utf-8').write('\n'.join(md) + '\n')
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    main()


