"""Queue 80, step 3: how often a session-control word was said and NOT recognised.

Log only; nothing is imported from the app. Re-run:

    python perf_artifacts/session_control_misses.py [LOG ...] [--out-prefix PATH]

Every DICTATE-lane utterance is read from samsara.log as
(capture time, transcript, outcome). The hands-free session recognises:

    end            outcome dictate_committed (whole utterance, or ". End." suffix)
    scratch that   outcome scratch_success
    cancel/abort   outcome abort (phrase anywhere in the utterance)
    refused commit outcome dictate_commit_refused (heard, gate said no)

A MISS is a staged utterance that was a control attempt. Two independent
signals, reported separately so neither hides the other:

  1. near-form: the whole normalised transcript is a known mis-hearing of a
     control word ("and", "nd", "in", "the end", "and.", "scratch", "scratch
     dat", "cancer" ...) or ends in "end" without the sentence punctuation
     the suffix rule needs.
  2. repeat: a short staged utterance (<= 3 words) followed within
     REPEAT_WINDOW_S by a recognised control of the same family, with nothing
     else staged in between -- the user said it again.

Each candidate is printed with its neighbours so the classification can be
checked by eye; the verdict column is the rule that fired, not a guess.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_control_latency import read_lines, utterances  # noqa: E402

REPEAT_WINDOW_S = 12.0

FAMILY_OF_OUTCOME = {
    "dictate_committed": "end",
    "scratch_success": "scratch that",
    "abort": "cancel",
    "dictate_commit_refused": "end",
}

NEAR_FORMS = {
    "end": {"and", "nd", "in", "an", "en", "the end", "and and", "end end", "ending", "ends", "and then",
            "hand", "then", "yeah end", "okay end", "end it", "end please", "and that", "n", "ed"},
    "scratch that": {"scratch", "scratch it", "scratch dat", "scratched that", "scratch this", "scratch that that",
                     "scratchthat", "stretch that", "scratch at", "catch that", "scratch the", "scrap that"},
    "cancel": {"cancer", "cancelled", "counsel", "council", "cancel it", "canceled"},
}


def norm(text: str) -> str:
    text = re.sub(r"[^a-z' ]", " ", (text or "").lower())
    words = [w for w in text.split() if w not in {"um", "uh", "uhh", "umm"}]
    return " ".join(words)


def near_form_family(text: str) -> str | None:
    n = norm(text)
    for family, forms in NEAR_FORMS.items():
        if n in forms:
            return family
    raw = (text or "").strip()
    # "...fixed end" -- the suffix rule needs ". End" / "! End" / "? End".
    if re.search(r"[A-Za-z,]\s+end\W*$", raw, re.I) and not re.search(r"[.!?]\s*end\W*$", raw, re.I):
        return "end"
    return None


def analyse(paths: list[Path]) -> dict:
    lines = read_lines(paths)
    utts = [u for u in utterances(lines) if u["mode"] == "dictate" and "outcome" in u]
    # Commits recorded without a transcript (hotkey commit) still count as commits,
    # but only transcript-carrying ones can be misses.
    recognised = {"end": 0, "scratch that": 0, "cancel": 0}
    refused = 0
    for u in utts:
        fam = FAMILY_OF_OUTCOME.get(u["outcome"])
        if fam and u.get("text") is not None:
            if u["outcome"] == "dictate_commit_refused":
                refused += 1
            else:
                recognised[fam] += 1

    candidates = []
    for i, u in enumerate(utts):
        if u["outcome"] != "dictate_staged" or u.get("text") is None:
            continue
        fam_near = near_form_family(u["text"])
        fam_repeat = None
        words = norm(u["text"]).split()
        if 0 < len(words) <= 3:
            for v in utts[i + 1:]:
                if v["t_capture"] - u["t_capture"] > REPEAT_WINDOW_S:
                    break
                fam = FAMILY_OF_OUTCOME.get(v["outcome"])
                if fam:
                    fam_repeat = fam
                    break
                if v["outcome"] == "dictate_staged":
                    break
        if fam_near or fam_repeat:
            prev_text = utts[i - 1].get("text") if i else None
            nxt = utts[i + 1] if i + 1 < len(utts) else None
            candidates.append({
                "ts": u["capture_ts"],
                "text": u["text"],
                "duration_s": u["duration_s"],
                "gate_run_ms": u.get("gate_run_ms"),
                "near_form": fam_near,
                "followed_by": fam_repeat,
                "prev": prev_text,
                "next": None if nxt is None else f"{nxt.get('outcome')}: {nxt.get('text')}",
            })

    def count(pred):
        return sum(1 for c in candidates if pred(c))

    families = {}
    for fam in recognised:
        both = count(lambda c: c["near_form"] == fam and c["followed_by"] == fam)
        near_only = count(lambda c: c["near_form"] == fam and c["followed_by"] != fam)
        repeat_only = count(lambda c: c["followed_by"] == fam and c["near_form"] != fam)
        families[fam] = {
            "recognised": recognised[fam],
            "miss_near_form_and_repeated": both,
            "miss_near_form_only": near_only,
            "short_staged_then_repeated_only": repeat_only,
        }
    return {
        "logs": [str(p) for p in paths],
        "dictate_utterances": len(utts),
        "refused_commits": refused,
        "families": families,
        "candidates": candidates,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--out-prefix", default=None)
    args = ap.parse_args(argv)
    home = Path.home() / ".samsara" / "logs"
    paths = [Path(p) for p in args.logs] or [home / "samsara.log.1", home / "samsara.log"]
    result = analyse(paths)
    print(json.dumps({k: v for k, v in result.items() if k != "candidates"}, indent=1))
    for c in result["candidates"]:
        print(f"{c['ts']}  near={c['near_form']!s:13} then={c['followed_by']!s:13} "
              f"dur={c['duration_s']}s run={c['gate_run_ms']}ms  {c['text']!r}  | prev={str(c['prev'])[:50]!r} | next={str(c['next'])[:60]!r}")
    if args.out_prefix:
        Path(args.out_prefix + ".json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
