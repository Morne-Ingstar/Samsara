"""Tests for the _is_hallucinated_segments() output-text backstop in dictation.py.

NOTE: this file did not exist prior to this change (the task that requested
it assumed it already did) -- created fresh here, alongside the existing
tools/test_halluc_gate.py (which covers the causal input-side defenses:
_buffer_has_contiguous_speech and _fade_edges). This file covers the
output-text backstop specifically.

_is_hallucinated_segments(seg_list, text) is a BACKSTOP, not the primary
hallucination defense (see its docstring in dictation.py) -- these tests
exercise it directly with synthetic segment lists, no audio, no model load.

Signature D corroboration fix: a bare 2-3 token whole-utterance identical-
word repeat ("click click", "no no", "beep beep beep") is NOT on its own a
reliable hallucination signal -- real emphatic speech looks identical at
the text level. Signature D now only fires when acoustically corroborated:
every segment's no_speech_prob > 0.5 (near-silence). See dictation.py's
Signature D comment block for the full rationale.

Run with: F:\\envs\\sami\\python.exe tools\\test_hallucination_guard.py
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dictation


def _seg(no_speech_prob=None, compression_ratio=1.0):
    return types.SimpleNamespace(
        no_speech_prob=no_speech_prob, compression_ratio=compression_ratio,
    )


def check(name, text, expected, seg_list=None):
    actual = dictation._is_hallucinated_segments(seg_list or [], text)
    status = "PASS" if actual == expected else "FAIL"
    print(f"  [{status}] {name}: text={text!r} expected={expected} actual={actual}")
    return actual == expected


def main():
    results = []

    print("--- Signature D corroboration fix: bare 2-3 token whole-utterance repeats ---")

    # No seg_list at all -- nothing to corroborate with, must NOT fire.
    results.append(check("'click click' with empty seg_list -- must NOT fire", "click click", False))
    results.append(check("'beep beep beep' with empty seg_list -- must NOT fire", "beep beep beep", False))

    # Real emphatic speech: low no_speech_prob (mic was live, real audio) --
    # must pass through untouched regardless of the repeated text.
    low_nsp_segs = [_seg(no_speech_prob=0.05)]
    results.append(check("'no no' with LOW no_speech_prob -- must NOT fire (real speech)",
                          "no no", False, seg_list=low_nsp_segs))
    results.append(check("'stop stop' with LOW no_speech_prob -- must NOT fire (real speech)",
                          "stop stop", False, seg_list=low_nsp_segs))
    results.append(check("'yes yes yes' with LOW no_speech_prob -- must NOT fire (real speech)",
                          "yes yes yes", False, seg_list=[_seg(no_speech_prob=0.1)]))

    # Phantom hallucination: high no_speech_prob across every segment
    # (near-silent buffer) -- must still be caught.
    high_nsp_segs = [_seg(no_speech_prob=0.9)]
    results.append(check("'click click' with HIGH no_speech_prob (all segments) -- MUST fire",
                          "click click", True, seg_list=high_nsp_segs))
    results.append(check(
        "'beep beep beep' with HIGH no_speech_prob (all segments) -- MUST fire",
        "beep beep beep", True, seg_list=[_seg(no_speech_prob=0.85), _seg(no_speech_prob=0.95)],
    ))

    # Partial corroboration (not EVERY segment high) -- must NOT fire.
    mixed_nsp_segs = [_seg(no_speech_prob=0.9), _seg(no_speech_prob=0.2)]
    results.append(check(
        "'click click' with MIXED no_speech_prob (one segment low) -- must NOT fire",
        "click click", False, seg_list=mixed_nsp_segs,
    ))

    # Segment present but missing no_speech_prob telemetry -- nothing to
    # corroborate with, must NOT fire.
    results.append(check(
        "'click click' with segment missing no_speech_prob -- must NOT fire",
        "click click", False, seg_list=[_seg(no_speech_prob=None)],
    ))

    results.append(check(
        "embedded mention inside real speech (must NOT fire)",
        "I just had a click click get transcribed", False, seg_list=high_nsp_segs,
    ))

    print("\n--- Pre-existing signatures, sanity-checked for regression ---")
    results.append(check(
        "Signature B: four-token low-diversity repeat (already worked, no seg_list needed)",
        "click, click, click, click", True,
    ))
    results.append(check(
        "negative control: real multi-word sentence",
        "I would like to schedule a meeting for tomorrow afternoon", False,
        seg_list=[_seg(no_speech_prob=0.05)],
    ))
    results.append(check("single word (too short to trigger Signature D)", "hello", False))
    results.append(check("empty string", "", False))

    total = len(results)
    passed = sum(results)
    print(f"\nRESULT: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
