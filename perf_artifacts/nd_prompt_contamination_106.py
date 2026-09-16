"""Queue 106 gate: does _sanitise_context_tail move the poisoned-tail row?

Queue 44 run 2 (nd_prompt_contamination.py) established the mechanism: the
same recording of "and" decodes correctly 33/37 times behind a clean prompt
tail and 0/37 times behind "...I don't know and nd nd nd". Queue 106 cleans
the tail before it reaches the decoder. This script measures whether that
cleaning recovers the row.

WHY A SECOND SCRIPT, not a re-run of the first one. The original cannot see a
dictation.py change at all: rebuild_tails() replays the raw staged texts out
of the live log and slices buf[-200:] itself, then hands each arm that literal
string as initial_prompt. Nothing of Samsara's is in its path, so running it
unchanged before and after a fix reproduces the same table twice. What is
needed is the same tokens, the same tails and the same model, with each real
tail ALSO put through the production sanitiser -- so "before" and "after" are
paired arms on one run rather than two runs that differ by whatever else
moved. Every clip, cut, room-tone and decode parameter is imported from the
original, unchanged.

Arms per token:
    no_prompt                     -- the floor (no context at all)
    tail_<t>/raw                  -- exactly queue 44's arm
    tail_<t>/clean                -- the same tail through _sanitise_context_tail

_sanitise_context_tail is lifted out of dictation.py's source with ast rather
than imported, so this keeps the original's "does not import dictation"
property (Samsara may be running from this tree) while still testing the real
production function, not a copy that could drift from it.

Writes nd_prompt_contamination_106.{json,md}; the queue 44 evidence files are
never touched.

    F:\\envs\\sami\\python.exe perf_artifacts\\nd_prompt_contamination_106.py
"""
import collections
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import nd_prompt_contamination as base   # noqa: E402  (path set above)

OUT_JSON = os.path.join(HERE, 'nd_prompt_contamination_106.json')
OUT_MD = os.path.join(HERE, 'nd_prompt_contamination_106.md')


# -- the production sanitiser, without importing dictation -------------------

def load_sanitiser():
    """Load the production pure gate without importing the running app."""
    from samsara.transcript_gates import _CONTEXT_TAIL_CHARS, _sanitise_context_tail
    return _sanitise_context_tail, _CONTEXT_TAIL_CHARS


def run_arms(model, row, pre, word, tail, prompts, base_name):
    import numpy as np
    intact = np.concatenate([pre, word, tail]).astype(np.float32)
    for arm, prompt in prompts.items():
        row['decodes'][arm] = base.decode(model, intact, prompt)
    print(f"{row['source']} {row['clip']} word {row['word_ms']}ms: " +
          ' | '.join(f'{k}={v!r}' for k, v in row['decodes'].items()), flush=True)
    return row


def main():
    sanitise, cap = load_sanitiser()
    tails, checks = base.rebuild_tails()
    print(f'rebuilt {len(checks)} staged steps; pending_chars verified at every one')

    prompts = {'no_prompt': None}
    cleaned = {}
    for t in sorted(tails):
        raw = tails[t]
        clean = sanitise(raw, cap)
        cleaned[t] = clean
        prompts[f'tail_{t}/raw'] = raw
        prompts[f'tail_{t}/clean'] = clean or None
        print(f'  tail {t} (live -> {base.TAIL_POINTS[t]!r})')
        print(f'    raw   {len(raw):3d}: ...{raw[-64:]!r}')
        print(f'    clean {len(clean):3d}: ...{clean[-64:]!r}')

    for name, raw in base.INCIDENT_TAILS.items():
        clean = sanitise(raw, cap)
        cleaned[name] = clean
        prompts[f'{name}/raw'] = raw
        prompts[f'{name}/clean'] = clean or None
        print(f'  {name}: raw={raw!r}; clean={clean!r}')

    from faster_whisper import WhisperModel
    model = WhisperModel('medium', device='cuda', compute_type='float16')
    os.makedirs(base.WAVS, exist_ok=True)

    rows, skipped = [], collections.Counter()
    for row, pre, word, tail, name in base.owner_tokens(model, skipped):
        rows.append(run_arms(model, row, pre, word, tail, prompts, name))
    for row, pre, word, tail, name in base.sapi_tokens():
        rows.append(run_arms(model, row, pre, word, tail, prompts, name))
    print(f'rows {len(rows)}; skipped {dict(skipped)}')

    sources = sorted({r['source'] for r in rows})
    arms = list(prompts)
    summary = {}
    for src in sources + ['ALL']:
        sub = [r for r in rows if src == 'ALL' or r['source'] == src]
        summary[src] = {'n': len(sub)}
        for arm in arms:
            c = collections.Counter(base.bucket(r['decodes'][arm]) for r in sub)
            summary[src][arm] = {k: c.get(k, 0) for k in ('and', 'nd', 'other', 'empty')}

    json.dump({'tails_raw': tails, 'tails_clean': cleaned, 'cap': cap,
               'tail_live_result': base.TAIL_POINTS, 'pending_checks': checks,
               'rows': rows, 'summary': summary, 'skipped': dict(skipped)},
              open(OUT_JSON, 'w', encoding='utf-8'), indent=1)

    md = ['# Queue 106: the same tails, raw and sanitised', '',
          'Paired arms on ONE run: every token goes through the no-prompt floor, each of the five '
          'real 200-char tails queue 44 rebuilt from the live log, and the same five tails put '
          'through dictation.py `_sanitise_context_tail` (lifted from source, not reimplemented). '
          'Clips, cuts, room tone, lane decode parameters and the bucketing are imported unchanged '
          f'from nd_prompt_contamination.py. pending_chars verified at all {len(checks)} staged steps.',
          '']
    for src in sources + ['ALL']:
        md += [f"## {src} (n={summary[src]['n']})", '', '| arm | and | nd | other | empty |',
               '|---|---|---|---|---|']
        for arm in arms:
            s = summary[src][arm]
            md.append(f"| {arm} | {s['and']} | {s['nd']} | {s['other']} | {s['empty']} |")
        md.append('')
    md += ['## What the sanitiser did to each tail', '']
    for t in sorted(tails):
        md += [f'- **{t}** (live decode of a standalone "and": `{base.TAIL_POINTS[t]}`)',
               f'  - raw   `...{tails[t][-90:]}`',
               f'  - clean `...{cleaned[t][-90:]}`']
    md += ['', '## Per token', '']
    for r in rows:
        md.append(f"- [{r['source']}] `{r['clip']}` word {r['word_ms']} ms, rms {r['word_rms']}: " +
                  '; '.join(f'{k} `{v}`' for k, v in r['decodes'].items()))
    md += ['', f'Skipped owner clips: {dict(skipped)}']
    open(OUT_MD, 'w', encoding='utf-8').write('\n'.join(md) + '\n')
    print(json.dumps(summary['ALL'], indent=1))


if __name__ == '__main__':
    main()
