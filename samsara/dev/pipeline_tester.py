#!/usr/bin/env python3
"""
Pipeline tester -- end-to-end simulation of the wake word -> command pipeline.

Uses the REAL modules (not stubs) so results match actual app behavior.
Run from the project root:

    python -m samsara.dev.pipeline_tester

Or with a custom phrase:

    python -m samsara.dev.pipeline_tester "jarvis dictate hello world"
"""

import sys
import os
import re
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from samsara.wake_word_matcher import match_wake_phrase
from samsara.wake_corrections import apply_corrections, was_corrected
from samsara.command_parser import parse_wake_command, normalize_command_text


WAKE_PHRASE = "jarvis"


def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def run_pipeline(text, wake_phrase=WAKE_PHRASE, verbose=True):
    """Run a single utterance through the full pipeline. Returns the final intent dict or None."""

    if verbose:
        print("=" * 64)
        print(f"  RAW         | {text}")

    # Stage 1: Corrections
    text_lower = text.lower().strip()
    corrected = apply_corrections(text_lower)
    changed = was_corrected(text_lower, corrected)
    if verbose:
        tag = "*" if changed else " "
        print(f"{tag} CORRECTED   | {corrected}" + (" (changed)" if changed else ""))

    # Stage 2: Wake word detection
    matched, match_type, match_index = match_wake_phrase(corrected, wake_phrase)
    if verbose:
        if matched:
            print(f"  WAKE        | YES  type={match_type}  index={match_index}")
        else:
            print(f"  WAKE        | NO   type={match_type}")
            # Show near-misses
            for word in corrected.split():
                clean = re.sub(r'[^\w]', '', word)
                if clean and similarity(clean, wake_phrase) > 0.55:
                    print(f"  NEAR MISS   | '{clean}' (similarity={similarity(clean, wake_phrase):.0%})")

    if not matched:
        if verbose:
            print(f"  RESULT      | -- no wake word detected --")
        return None

    # Stage 3: Extract command text after wake word
    command_raw = corrected[match_index + len(wake_phrase):]
    if verbose:
        print(f"  CMD RAW     | '{command_raw}'")

    # Stage 4: Normalize
    command_norm = normalize_command_text(command_raw)
    if verbose:
        print(f"  NORMALIZED  | '{command_norm}'")

    # Stage 5: Parse intent
    intent = parse_wake_command(command_raw)
    if verbose:
        t = intent["type"]
        n = intent.get("name", "")
        c = intent.get("content", "")
        symbol = "D" if t == "dictation" else "C" if t == "command_text" else "?"
        print(f"  PARSED [{symbol}]  | type={t}  name={n}  content='{c}'")

    return intent


# ---- Built-in test suite ----

TEST_CASES = [
    # (input_text, expected_type, expected_name, description)

    # Clean inputs
    ("jarvis dictate hello world",          "dictation", "dictate",       "clean dictate with content"),
    ("jarvis dictate",                      "dictation", "dictate",       "bare dictate"),
    ("jarvis long dictate",                 "dictation", "long_dictate",  "bare long dictate"),
    ("jarvis short dictate hello",          "dictation", "short_dictate", "short dictate with content"),
    ("jarvis quick dictate",                "dictation", "short_dictate", "quick dictate synonym"),

    # Punctuation noise from Whisper
    ("jarvis, dictate hello world",         "dictation", "dictate",       "comma after wake word"),
    ("jarvis - dictate hello world",        "dictation", "dictate",       "dash after wake word"),
    ("jarvis. dictate hello",               "dictation", "dictate",       "period after wake word"),
    ("jarvis — dictate: hello world",       "dictation", "dictate",       "em dash + colon"),

    # Weird spacing
    ("jarvis     dictate     hello world",  "dictation", "dictate",       "extra spaces"),

    # Filler words
    ("jarvis please dictate hello",         "dictation", "dictate",       "leading filler"),
    ("jarvis um dictate hello",             "dictation", "dictate",       "um filler"),

    # Non-dictation commands (should be command_text)
    ("jarvis copy that",                    "command_text", None,         "regular command"),
    ("jarvis open chrome",                  "command_text", None,         "launch command"),
    ("jarvis select all",                   "command_text", None,         "hotkey command"),

    # Wake word positions
    ("hey jarvis dictate hello",            "dictation", "dictate",       "wake word mid-phrase"),
    ("so I said jarvis dictate hello",      "dictation", "dictate",       "wake word late in phrase"),

    # Misrecognitions (need entries in CORRECTIONS to pass)
    # ("charvis dictate hello world",       "dictation", "dictate",       "misrecognition: charvis"),

    # Failure cases — no wake word
    ("hello world",                         None,        None,            "no wake word at all"),
    ("dictate hello world",                 None,        None,            "dictate without wake word"),

    # Edge cases
    ("jarvis",                              "unknown",   None,            "wake word only, nothing after"),
    ("jarvis,",                             "unknown",   None,            "wake word + punctuation only"),
]


def run_test_suite(wake_phrase=WAKE_PHRASE):
    """Run all test cases and report pass/fail."""
    passed = 0
    failed = 0
    errors = []

    print("\n" + "=" * 64)
    print("  PIPELINE TEST SUITE")
    print("=" * 64)

    for text, expected_type, expected_name, desc in TEST_CASES:
        intent = run_pipeline(text, wake_phrase, verbose=False)

        actual_type = intent["type"] if intent else None
        actual_name = intent.get("name") if intent else None

        type_ok = actual_type == expected_type
        name_ok = actual_name == expected_name

        if type_ok and name_ok:
            passed += 1
            print(f"  PASS  | {desc}")
        else:
            failed += 1
            detail = f"expected type={expected_type} name={expected_name}, got type={actual_type} name={actual_name}"
            errors.append((desc, text, detail))
            print(f"  FAIL  | {desc}")
            print(f"        | {detail}")

    print("-" * 64)
    print(f"  {passed} passed, {failed} failed out of {passed + failed}")

    if errors:
        print(f"\n  FAILURES:")
        for desc, text, detail in errors:
            print(f"    {desc}")
            print(f"      input:  {text}")
            print(f"      {detail}")

    return failed == 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Custom phrase from command line
        phrase = " ".join(sys.argv[1:])
        run_pipeline(phrase)
    else:
        # Run full suite first
        all_passed = run_test_suite()
        # Then run each case with full verbose output
        print("\n\n" + "=" * 64)
        print("  DETAILED TRACE (all cases)")
        print("=" * 64)
        for text, _, _, _ in TEST_CASES:
            run_pipeline(text)
        print()
