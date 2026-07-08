"""Declarative battery of dictation stress-test steps for the guided
Stress-Test Wizard (samsara/ui/stress_wizard_qt.py).

This module defines WHAT to test and HOW to judge the result. It does not
capture audio or drive the UI -- the wizard runs each step through the
user's NORMAL dictation hotkey/wake flow and reads the resulting
samsara.diagnostics.DiagRecord + captured text back in.

Reuses, never duplicates:
  - samsara.diagnostics (DiagRecord, classify()) for signal capture.
  - samsara.ui.voice_training_qt._normalize_phrase for text comparison --
    the same lowercase/strip-punctuation/collapse-whitespace normalization
    the Calibration tab's test phrases already use.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from samsara.diagnostics import DiagRecord
from samsara.ui.voice_training_qt import _normalize_phrase

# (passed, reason) -- reason is always a short human-readable string, shown
# to the user regardless of pass/fail so failures are diagnosable at a glance.
PassCriteria = Callable[[Optional[DiagRecord], Optional[str]], "tuple[bool, str]"]

_WORD_COUNT_TOLERANCE = 0.4   # matches smart_corrections' own deviation guardrail

_SHORT_PHRASE = "quick brown fox"
_LONG_MONOLOGUE = (
    "Voice recognition software has come a long way over the past several "
    "years, transforming how people interact with their computers and "
    "phones. Instead of typing every message, many users now speak "
    "naturally and let the software handle transcription, punctuation, "
    "and formatting automatically, saving time throughout the day while "
    "reducing strain on the hands and wrists, especially for people who "
    "write frequently during a normal working day."
)
_HOMOPHONE_SENTENCE = "They're going to their car over there."
_NUMBERS_PUNCT_SENTENCE = "The meeting is at 3:30 PM on July 9th, room 12B."
_HOMOPHONE_WATCH_WORDS = ("they're", "their", "there")


@dataclass
class StressTestStep:
    id: str
    title: str
    instruction: str
    expected_text: "str | None"
    category: str
    pass_criteria: PassCriteria


# ---------------------------------------------------------------------------
# pass_criteria factories -- each returns a closure matching PassCriteria
# ---------------------------------------------------------------------------

def _make_exact_match_criteria(expected: str) -> PassCriteria:
    norm_expected = _normalize_phrase(expected)

    def _pc(rec: "DiagRecord | None", got_text: "str | None"):
        if not got_text:
            return False, "No text captured"
        norm_got = _normalize_phrase(got_text)
        if norm_got == norm_expected:
            return True, "Exact match"
        return False, f"Expected '{expected}', got '{got_text}'"

    return _pc


def _pc_no_output(rec: "DiagRecord | None", got_text: "str | None"):
    """PASS = nothing was transcribed -- for accidental-tap/silent-hold
    steps where speaking never happened."""
    if got_text:
        return False, f"Expected no output, got: '{got_text}'"
    if rec is not None and rec.text:
        return False, f"Expected no output, diagnostics recorded text: '{rec.text}'"
    return True, "No output produced, as expected"


def _make_word_count_tolerance_criteria(
    expected: str, tolerance: float = _WORD_COUNT_TOLERANCE
) -> PassCriteria:
    expected_words = len(expected.split())

    def _pc(rec: "DiagRecord | None", got_text: "str | None"):
        if not got_text:
            return False, "No text captured"
        got_words = len(got_text.split())
        if expected_words == 0:
            if got_words == 0:
                return True, "Expected empty text"
            return False, "Expected empty text but got content"
        deviation = abs(got_words - expected_words) / expected_words
        if deviation > tolerance:
            return False, (
                f"Word count deviated {deviation * 100:.0f}% "
                f"(expected ~{expected_words}, got {got_words})"
            )
        return True, f"Word count within tolerance ({got_words}/{expected_words})"

    return _pc


def _make_homophone_criteria(
    expected: str, watch_words=_HOMOPHONE_WATCH_WORDS
) -> PassCriteria:
    norm_expected = _normalize_phrase(expected)
    expected_tokens = norm_expected.split()

    def _pc(rec: "DiagRecord | None", got_text: "str | None"):
        if not got_text:
            return False, "No text captured"
        norm_got = _normalize_phrase(got_text)
        if norm_got == norm_expected:
            return True, "All homophones correct"
        got_tokens = norm_got.split()
        wrong = []
        for w in watch_words:
            if w not in expected_tokens:
                continue
            idx = expected_tokens.index(w)
            got_word = got_tokens[idx] if idx < len(got_tokens) else None
            if got_word != w:
                wrong.append(f"{w}->{got_word}")
        if wrong:
            return False, f"Homophone(s) incorrect: {', '.join(wrong)}"
        return False, f"Text differs from expected (got: '{got_text}')"

    return _pc


def _make_jargon_criteria(terms) -> PassCriteria:
    norm_terms = [(t, _normalize_phrase(t)) for t in terms]

    def _pc(rec: "DiagRecord | None", got_text: "str | None"):
        if not got_text:
            return False, "No text captured"
        norm_got = _normalize_phrase(got_text)
        missing = [orig for orig, norm in norm_terms if norm not in norm_got]
        if missing:
            return False, f"Missing term(s): {', '.join(missing)}"
        return True, "All vocabulary terms present"

    return _pc


def _pc_quiet_speech_report(rec: "DiagRecord | None", got_text: "str | None"):
    """Behavioral, not a pass/fail gate -- always 'passes'; the reason
    string reports the signals the wizard's report should surface."""
    if rec is None:
        return True, "No diagnostics record captured (informational only)"
    return True, (
        f"no_speech_prob={rec.no_speech_prob}, avg_logprob={rec.avg_logprob} "
        "(informational -- not a pass/fail gate)"
    )


# ---------------------------------------------------------------------------
# Static battery steps
# ---------------------------------------------------------------------------

STEP_SHORT_WORD = StressTestStep(
    id="short_word",
    title="Single word",
    instruction="Say the single word shown below.",
    expected_text="testing",
    category="accuracy",
    pass_criteria=_make_exact_match_criteria("testing"),
)

STEP_SHORT_PHRASE = StressTestStep(
    id="short_phrase",
    title="Short phrase",
    instruction="Say the short phrase shown below.",
    expected_text=_SHORT_PHRASE,
    category="accuracy",
    pass_criteria=_make_exact_match_criteria(_SHORT_PHRASE),
)

STEP_ACCIDENTAL_TAP = StressTestStep(
    id="accidental_tap",
    title="Accidental tap",
    instruction=(
        "Press and release your dictation hotkey immediately, "
        "without saying anything (a quick accidental tap)."
    ),
    expected_text=None,
    category="hallucination",
    pass_criteria=_pc_no_output,
)

STEP_SILENT_HOLD = StressTestStep(
    id="silent_hold",
    title="Silent hold",
    instruction=(
        "Hold your dictation hotkey for about 3 seconds without saying "
        "anything, then release."
    ),
    expected_text=None,
    category="hallucination",
    pass_criteria=_pc_no_output,
)

STEP_LONG_MONOLOGUE = StressTestStep(
    id="long_monologue",
    title="Long monologue",
    instruction="Read the paragraph shown below aloud, at a natural pace.",
    expected_text=_LONG_MONOLOGUE,
    category="truncation",
    pass_criteria=_make_word_count_tolerance_criteria(_LONG_MONOLOGUE),
)

STEP_HOMOPHONES = StressTestStep(
    id="homophones",
    title="Homophones",
    instruction="Say the sentence shown below exactly as written.",
    expected_text=_HOMOPHONE_SENTENCE,
    category="smart_corrections",
    pass_criteria=_make_homophone_criteria(_HOMOPHONE_SENTENCE),
)

STEP_NUMBERS_PUNCT = StressTestStep(
    id="numbers_punct",
    title="Numbers & punctuation",
    instruction="Say the sentence shown below exactly as written.",
    expected_text=_NUMBERS_PUNCT_SENTENCE,
    category="formatting",
    pass_criteria=_make_exact_match_criteria(_NUMBERS_PUNCT_SENTENCE),
)

STEP_QUIET_SPEECH = StressTestStep(
    id="quiet_speech",
    title="Quiet speech",
    instruction=f'Say "{_SHORT_PHRASE}" as quietly as you comfortably can.',
    expected_text=None,
    category="quality",
    pass_criteria=_pc_quiet_speech_report,
)

STEP_FAST_SPEECH = StressTestStep(
    id="fast_speech",
    title="Fast speech",
    instruction="Say the phrase shown below as fast as you comfortably can.",
    expected_text=_SHORT_PHRASE,
    category="accuracy",
    pass_criteria=_make_exact_match_criteria(_SHORT_PHRASE),
)


# ---------------------------------------------------------------------------
# Dynamic jargon step -- built from the user's trained vocabulary
# ---------------------------------------------------------------------------

_JARGON_MAX_TERMS = 5


def build_jargon_step(voice_training_window) -> "StressTestStep | None":
    """Build the jargon step from the user's custom vocabulary + corrections
    dict (VoiceTrainingQt data). Returns None when there's no vocabulary to
    test -- the wizard omits the step entirely in that case.

    `voice_training_window` is duck-typed: anything with `.custom_vocab`
    (list[str]) and `.corrections_dict` (dict[str, str]) attributes works,
    so this is testable without a real VoiceTrainingQt/Qt dependency.
    """
    if voice_training_window is None:
        return None

    vocab = list(getattr(voice_training_window, 'custom_vocab', None) or [])
    corrections = getattr(voice_training_window, 'corrections_dict', None) or {}

    terms = []
    seen = set()
    for term in vocab + list(corrections.values()):
        key = term.strip().lower()
        if not term.strip() or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= _JARGON_MAX_TERMS:
            break

    if not terms:
        return None

    term_list = ", ".join(terms)
    return StressTestStep(
        id="jargon",
        title="Vocabulary & jargon",
        instruction=(
            f"Speak a sentence naturally using these words: {term_list}"
        ),
        expected_text=None,
        category="vocabulary",
        pass_criteria=_make_jargon_criteria(terms),
    )


# ---------------------------------------------------------------------------
# Battery assembly
# ---------------------------------------------------------------------------

def build_battery(voice_training_window=None) -> list:
    """Build the full ordered battery. The jargon step is inserted between
    'homophones' and 'numbers_punct' only when the user has trained
    vocabulary/corrections to test; omitted entirely otherwise."""
    steps = [
        STEP_SHORT_WORD,
        STEP_SHORT_PHRASE,
        STEP_ACCIDENTAL_TAP,
        STEP_SILENT_HOLD,
        STEP_LONG_MONOLOGUE,
        STEP_HOMOPHONES,
    ]
    jargon_step = build_jargon_step(voice_training_window)
    if jargon_step is not None:
        steps.append(jargon_step)
    steps.extend([
        STEP_NUMBERS_PUNCT,
        STEP_QUIET_SPEECH,
        STEP_FAST_SPEECH,
    ])
    return steps


# ---------------------------------------------------------------------------
# Report generation -- pure, Qt-free so it's directly unit-testable; the
# wizard's "Copy report" button just calls this and copies the string.
# ---------------------------------------------------------------------------

def generate_report(results) -> str:
    """Plain-text report for the wizard's final screen / "Copy report" button.

    `results` is a list of dicts, one per attempted/skipped step:
        {'step': StressTestStep, 'passed': bool | None, 'reason': str,
         'verdicts': list[str]}
    `passed` is None for a skipped step.
    """
    passed_count = sum(1 for r in results if r['passed'] is True)
    failed_count = sum(1 for r in results if r['passed'] is False)
    skipped_count = sum(1 for r in results if r['passed'] is None)

    lines = [
        "Samsara Stress Test Report",
        "=" * 30,
        "",
        f"{passed_count} passed, {failed_count} failed, {skipped_count} skipped "
        f"(of {len(results)})",
        "",
    ]

    for r in results:
        status = "SKIP" if r['passed'] is None else ("PASS" if r['passed'] else "FAIL")
        lines.append(f"[{status}] {r['step'].title}: {r['reason']}")

    all_verdicts = []
    seen = set()
    for r in results:
        for v in r.get('verdicts', []) or []:
            if v == "OK" or v in seen:
                continue
            seen.add(v)
            all_verdicts.append(v)

    lines.append("")
    if all_verdicts:
        lines.append("Diagnostics verdicts observed:")
        for v in all_verdicts:
            lines.append(f"  - {v}")
    else:
        lines.append("No non-OK diagnostics verdicts observed.")

    return "\n".join(lines)
