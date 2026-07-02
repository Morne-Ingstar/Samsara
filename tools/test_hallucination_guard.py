"""Tests for the _is_hallucinated_segments() output-text backstop in dictation.py.

NOTE: this file did not exist prior to this change (the task that requested
it assumed it already did) -- created fresh here, alongside the existing
tools/test_halluc_gate.py (which covers the causal input-side defenses:
_buffer_has_contiguous_speech and _fade_edges). This file covers the
output-text backstop specifically.

_is_hallucinated_segments(seg_list, text) is a BACKSTOP, not the primary
hallucination defense (see its docstring in dictation.py) -- these tests
exercise it directly with synthetic segment lists, no audio, no model load.

Run with: F:\\envs\\sami\\python.exe tools\\test_hallucination_guard.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dictation


def check(name, text, expected, seg_list=None):
    actual = dictation._is_hallucinated_segments(seg_list or [], text)
    status = "PASS" if actual == expected else "FAIL"
    print(f"  [{status}] {name}: text={text!r} expected={expected} actual={actual}")
    return actual == expected


def main():
    results = []

    print("--- Fix 2: bare 2-3 token whole-utterance repeats (Signature D) ---")
    results.append(check("bare two-token repeat", "click click", True))
    results.append(check("punctuated two-token repeat", "Click, click.", True))
    results.append(check("bare three-token repeat", "beep beep beep", True))
    results.append(check(
        "embedded mention inside real speech (must NOT fire)",
        "I just had a click click get transcribed", False,
    ))
    # "no no" is a legitimate two-word repeat that could plausibly be real
    # speech (emphatic denial) -- but a bare two-token identical-word
    # whole-utterance is overwhelmingly the guard's domain in practice, and
    # the task accepts this as intentional collateral rather than adding a
    # word-specific exception (that kind of allowlist is exactly the kind of
    # signature creep this backstop is meant to avoid -- see its docstring).
    results.append(check("'no no' -- accepted collateral, not a bug", "no no", True))

    print("\n--- Pre-existing signatures, sanity-checked for regression ---")
    results.append(check(
        "Signature B: four-token low-diversity repeat (already worked)",
        "click, click, click, click", True,
    ))
    results.append(check(
        "negative control: real multi-word sentence",
        "I would like to schedule a meeting for tomorrow afternoon", False,
    ))
    results.append(check("single word (too short to trigger Signature D)", "hello", False))
    results.append(check("empty string", "", False))

    total = len(results)
    passed = sum(results)
    print(f"\nRESULT: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
