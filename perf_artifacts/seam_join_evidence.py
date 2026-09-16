"""Queue 81 evidence: replay every staged DICTATE sequence in the owner's logs
through the staging code before and after the ellipsis-seam join, and show
every seam whose result changed.

Staged texts come from `[SESSION] mode=dictate outcome=dictate_staged` lines
(the 'text' field is exactly what was appended). A new sequence starts where
pending_chars equals the stripped text length (fresh buffer). Each sequence is
fed to SessionModeManager._stage_dictate_chunk of:
  before: a copy of samsara/session_modes.py taken before this change
  after:  the working tree
Committed text is then formatted the way the fallback commit does when the
re-decode is skipped (process_transcription's number rule + auto-capitalise,
clean_text, formatting tokens; smart_correct is an LLM pass and is not run).

Does not import dictation.

    F:\\envs\\sami\\python.exe perf_artifacts\\seam_join_evidence.py <before_copy.py>
"""
import ast
import importlib.util
import json
import os
import re
import sys
from unittest.mock import Mock

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from samsara import session_modes as after_mod  # noqa: E402
from samsara.cleanup import clean_text  # noqa: E402
from samsara.formatting_tokens import apply_formatting_tokens  # noqa: E402
from samsara.number_format import format_spoken_numbers  # noqa: E402

LOGS = [os.path.expanduser(p) for p in (
    '~/.samsara/logs/samsara.log.3', '~/.samsara/logs/samsara.log.1', '~/.samsara/logs/samsara.log')]
OUT_JSON = os.path.join(HERE, 'seam_join_evidence.json')
OUT_MD = os.path.join(HERE, 'seam_join_evidence.md')


def load_before(path):
    spec = importlib.util.spec_from_file_location('session_modes_before', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['session_modes_before'] = mod
    spec.loader.exec_module(mod)
    return mod


def manager(mod):
    return mod.SessionModeManager(
        abort_phrases=['cancel'], foreground_exe_resolver=Mock(return_value='notepad.exe'),
        foreground_hwnd_resolver=Mock(return_value=1), inject_fn=Mock(), remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(), agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True,
        clock=lambda: 0.0)


def stage_all(mod, texts):
    mgr = manager(mod)
    mgr.force_mode(mod.SessionMode.DICTATE)
    for text in texts:
        mgr._stage_dictate_chunk(text)
    return mgr.dictate_pending_buffer


def fallback_commit_format(text):
    """dictation.py _inject_fn with auto_capitalize/format_numbers on, clean mode."""
    text = format_spoken_numbers(text)
    text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
    text = re.sub(r'([.!?]\s+)([a-z])', lambda m: m.group(1) + m.group(2).upper(), text)
    text = clean_text(text, mode='clean')
    return apply_formatting_tokens(text)


def sequences():
    seqs, cur = [], None
    for lf in LOGS:
        if not os.path.exists(lf):
            continue
        for line in open(lf, encoding='utf-8', errors='replace'):
            if 'outcome=dictate_staged' not in line:
                continue
            try:
                detail = ast.literal_eval(line[line.index('detail=') + 7:].strip())
            except Exception:
                continue
            text, pending = detail['text'], detail['pending_chars']
            if cur is None or pending == len(text.strip()):
                cur = {'start': line[:19], 'texts': []}
                seqs.append(cur)
            cur['texts'].append(text.strip())
    return seqs


def main():
    import logging
    logging.disable(logging.CRITICAL)
    before_mod = load_before(sys.argv[1])
    changed, seam_total, clause_counts = [], 0, {}
    for seq in sequences():
        texts = seq['texts']
        for i in range(1, len(texts)):
            seam_total += 1
            prev_buf_after = stage_all(after_mod, texts[:i])
            decision = after_mod.decide_ellipsis_seam(prev_buf_after, texts[i], texts[i - 1])
            if decision is not None:
                clause_counts[(decision.clause, decision.join)] = clause_counts.get((decision.clause, decision.join), 0) + 1
            # The seam decision depends only on the previous fragment's ending
            # (and whether it ended terminal), so the seam is replayed on its
            # own two fragments; a full-prefix replay would re-flag every later
            # seam in a sequence once one earlier seam differed.
            pair = [texts[i - 1], texts[i]]
            b = stage_all(before_mod, pair)
            a = stage_all(after_mod, pair)
            if a != b:
                changed.append({'start': seq['start'], 'prev': texts[i - 1], 'next': texts[i],
                                'clause': decision.clause if decision else None,
                                'before_tail': b[-90:], 'after_tail': a[-90:]})
    focus = {}
    for seq in sequences():
        if seq['start'].startswith('2026-09-15 14:50') or seq['start'].startswith('2026-09-15 14:58'):
            focus[seq['start']] = {
                'before': fallback_commit_format(stage_all(before_mod, seq['texts'])),
                'after': fallback_commit_format(stage_all(after_mod, seq['texts'])),
            }
    report = {'sequences': len(sequences()), 'seams': seam_total, 'changed': changed,
              'ellipsis_clause_counts': {f'{c} join={j}': n for (c, j), n in sorted(clause_counts.items())},
              'fallback_commit_14_50_and_14_58': focus}
    json.dump(report, open(OUT_JSON, 'w', encoding='utf-8'), indent=1)
    md = ['# Queue 81: ellipsis-seam join replayed over every staged sequence in the logs', '',
          f"{report['sequences']} staged sequences, {report['seams']} seams; "
          f"{len(changed)} seams stage differently after the change.", '',
          '## Ellipsis seams by clause (after)', '']
    md += [f'- {k}: {v}' for k, v in report['ellipsis_clause_counts'].items()]
    md += ['', '## Every changed seam', '', '| when | previous fragment | next fragment | clause | before (tail) | after (tail) |',
           '|---|---|---|---|---|---|']
    for c in changed:
        md.append('| {start} | `{prev}` | `{next}` | {clause} | `{before_tail}` | `{after_tail}` |'.format(
            **{k: str(v).replace('|', '/') for k, v in c.items()}))
    md += ['', '## Fallback commit text (re-decode skipped), before vs after', '']
    for start, v in focus.items():
        md += [f'### sequence starting {start}', '', '**before:**', '', v['before'], '', '**after:**', '', v['after'], '']
    open(OUT_MD, 'w', encoding='utf-8').write('\n'.join(md) + '\n')
    print(f"sequences {report['sequences']} seams {report['seams']} changed {len(changed)}")
    print(json.dumps(report['ellipsis_clause_counts'], indent=1))
    for c in changed:
        print(f"[{c['clause']}] {c['prev'][-40:]!r} + {c['next'][:40]!r}\n    before ...{c['before_tail'][-60:]!r}\n    after  ...{c['after_tail'][-60:]!r}")


if __name__ == '__main__':
    main()
