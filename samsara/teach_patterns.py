"""Voice-teaching patterns for the transcription VOCABULARY and CORRECTIONS
stores -- siblings to samsara/ava_corrections.py's TEACHING_PATTERNS /
parse_teaching / parse_forget, but targeting samsara/ui/voice_training_qt.py's
VoiceTrainingQt.custom_vocab / corrections_dict instead of ava_corrections.json.

Kept in ONE discoverable module (not folded into ask_ollama.py or
ava_corrections.py) so a future Quick Reference window can list every
supported voice-teaching phrasing from a single import.

Linguistic split from ava_corrections' existing patterns -- deliberately
DISTINCT trigger words, verified non-overlapping against every
ava_corrections/ava_profile pattern:
  - "I say X, I mean Y" / "X means Y"        -> Ava alias (ava_corrections.py, unchanged)
  - "correct X to Y" / "when you hear X write/type/use Y" -> correction (this module)
  - "add the word X to my vocabulary" / "learn the word X" -> vocabulary (this module)

ONE real collision was found during audit: ava_corrections.FORGET_PATTERNS'
generic `^forget (.+)$` would otherwise swallow "forget the word X" /
"forget the correction X" before this module's own, more specific forget
pattern ever got a chance to run. Resolved at the dispatch site
(plugins/commands/ask_ollama.py's _check_teaching_intent) by checking this
module's patterns BEFORE ava_corrections' checks -- see that function's
comment for the full reasoning.

=============================================================================
THE ANTI-BOOTSTRAP PROBLEM (2026-07-11 respec) -- why this module exists
in this shape
=============================================================================
The naive design (this module's 2026-07-11 first cut) had a bootstrap flaw:
a taught word/correction was captured from the SAME ASR being taught. If you
say "add the word Morne to my vocabulary" and Whisper mishears "Morne" as
"more nay", the vocabulary now permanently contains the WRONG spelling --
and every future utterance gets biased toward that wrong spelling via
Whisper's initial_prompt. Corrections have the identical flaw on their LHS:
"correct flat to hat" captures BOTH sides from the same fallible pass.

Three independent SPELLING-TRUTH CHANNELS fix this -- nothing persists
unless it came from one of them:

  1. BUFFER-SOURCED CORRECTION TARGETS (resolve_correction_target below).
     A correction's LHS ("that" or a named X) is NEVER trusted from the
     teaching utterance's own transcription of X. "that" resolves to the
     literal most-recent segment Samsara already emitted (session history);
     a named X is fuzzy-matched against recent segments and the BUFFER'S
     own literal text is what gets stored -- never the freshly-transcribed
     X token. The segment was ALREADY committed to screen/history before
     this teaching utterance happened, so it's a fixed, independently-
     verifiable truth rather than a second dice-roll from the same model.

  2. VOICE SPELLING MODE (letters -> samsara/letter_spelling.parse_letters).
     "spelled <LETTERS>" makes individual LETTER NAMES ("M", "oh", "are")
     the unit Whisper has to get right, not a whole word. Letter names are
     a closed, tiny vocabulary with well-known mis-transcription patterns
     (handled by a homophone map), so this is a fundamentally easier/more
     reliable transcription target than the word itself -- and when NOT
     spelled, a plain word is only trusted if it's independently verifiable
     against a real dictionary (see is_known_dictionary_word).

  3. SELECTION / CLIPBOARD TEXT GRABS (grab_selection_text /
     grab_clipboard_text). Bypass ASR entirely -- the text comes from
     whatever the user already had highlighted or copied, read via the
     Win32 clipboard, not decoded from audio at all. This is the first real
     consumer of commit 4ca1ad8's ClipboardSnapshot / clipboard-sequence-
     number guard (built then, unwired until now): grabbing a selection
     means deliberately overwriting the clipboard with Ctrl+C, so it reuses
     that same save/restore machinery to leave the user's actual clipboard
     untouched afterward.

Every one of these still passes through a CONFIRMATION READBACK (built at
the ask_ollama.py dispatch layer, using record/consume-pending helpers
below) before anything persists, UNLESS the source is exact by construction
(selection/clipboard text, or a plain word independently verified against
the dictionary) -- see each function's docstring for exactly which paths
skip the readback and why that's still safe.
"""
import difflib
import re
import threading
import time
import unicodedata

from samsara.correction_capture import _MAX_PHRASE_WORDS, _is_case_only, _is_punctuation_only
from samsara.letter_spelling import parse_letters

try:
    import pronouncing
    _HAS_PRONOUNCING = True
except ImportError:
    pronouncing = None
    _HAS_PRONOUNCING = False


# ============================================================================
# STEP 2 -- command grammar
# ============================================================================
#
# Every pattern below is tried in the ORDER listed inside parse_vocab_add /
# parse_correction_add: source-grab patterns (selection/clipboard) are tried
# BEFORE the general literal-word patterns, because "add the selection to my
# vocabulary" would otherwise be swallowed by the literal pattern with W =
# "the selection" (a string, not an instruction to go read the clipboard).

_VOCAB_SOURCE_PATTERN = re.compile(
    r'^add (?:the )?(selected word|selected text|selection|highlighted word|'
    r'highlighted text|clipboard) to (?:my )?(?:vocabulary|vocab|dictionary)$',
    re.IGNORECASE,
)

_VOCAB_LITERAL_PATTERNS = [
    re.compile(
        r'^add (?:the word )?(.+?) to (?:my )?(?:vocabulary|vocab|dictionary)'
        r'(?: spelled (.+))?$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^learn the word (.+?)(?: spelled (.+))?$',
        re.IGNORECASE,
    ),
]

_CORRECTION_SOURCE_PATTERN = re.compile(
    r'^correct (that|.+?) to (?:the )?(selection|selected word|selected text|clipboard)$',
    re.IGNORECASE,
)

_CORRECTION_LITERAL_PATTERNS = [
    # "correct that to Y" -- LHS is always the literal keyword "that";
    # kept as its own pattern (rather than folded into the general one
    # below) so the LHS-kind is unambiguous from the match itself, no
    # after-the-fact string comparison against "that" needed.
    re.compile(
        r'^correct that to (.+?)(?: spelled (.+))?$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^correct (.+?) to (.+?)(?: spelled (.+))?$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^when you (?:hear|write) (.+?) (?:write|type|use) (.+?)(?: spelled (.+))?$',
        re.IGNORECASE,
    ),
]

UNDO_PATTERN = re.compile(r'^undo that$', re.IGNORECASE)

FORGET_PATTERN = re.compile(r'^forget the (word|correction) (.+)$', re.IGNORECASE)

# A pending confirmation can be rejected without needing a fixed "ava
# cancel" prefix -- see plugins/commands/ask_ollama.py's pending-
# confirmation gate, which checks this against free text reaching
# _check_teaching_intent while a vocab/correction confirmation is open.
_REJECT_PATTERN = re.compile(r'^(no|nope|cancel|nevermind|never mind)$', re.IGNORECASE)


def _word_count_ok(phrase: str) -> bool:
    """Same 1-4 word bound as correction_capture.py's _MAX_PHRASE_WORDS --
    imported, not duplicated."""
    words = phrase.split()
    return 0 < len(words) <= _MAX_PHRASE_WORDS


_SOURCE_KEYWORD_TO_KIND = {
    'selection': 'selection',
    'selected word': 'selection',
    'selected text': 'selection',
    'highlighted word': 'selection',
    'highlighted text': 'selection',
    'clipboard': 'clipboard',
}


def parse_vocab_add(text: str) -> "dict | None":
    """Parse a vocabulary-teaching utterance.

    Returns None if no pattern matches. Otherwise a dict:
      {'kind': 'source', 'source': 'selection' | 'clipboard'}
        -- caller must grab the word from that source (grab_selection_text /
           grab_clipboard_text below), not from this utterance's own text.
      {'kind': 'literal', 'word': str, 'letters': str | None}
        -- 'word' is the AS-TRANSCRIBED word/phrase from THIS utterance
           (not yet trustworthy -- see resolve_vocab_word_truth below for
           what happens to it next). 'letters' is the raw "spelled <...>"
           tail text if present, else None.
    """
    text = (text or "").strip()

    m = _VOCAB_SOURCE_PATTERN.match(text)
    if m:
        keyword = m.group(1).lower()
        return {'kind': 'source', 'source': _SOURCE_KEYWORD_TO_KIND[keyword]}

    for pattern in _VOCAB_LITERAL_PATTERNS:
        m = pattern.match(text)
        if m:
            word = m.group(1).strip()
            letters = m.group(2).strip() if m.group(2) else None
            if word and _word_count_ok(word):
                return {'kind': 'literal', 'word': word, 'letters': letters}
    return None


def parse_correction_add(text: str) -> "dict | None":
    """Parse a correction-teaching utterance.

    Returns None if no pattern matches. Otherwise a dict:
      {'lhs_kind': 'that' | 'named', 'lhs_raw': str | None,
       'rhs_kind': 'source', 'rhs_source': 'selection' | 'clipboard'}
        -- RHS must be grabbed from that source, not this utterance's text.
      {'lhs_kind': 'that' | 'named', 'lhs_raw': str | None,
       'rhs_kind': 'literal', 'rhs': str, 'letters': str | None}
        -- 'lhs_raw' is the AS-TRANSCRIBED X for a 'named' LHS (never
           trusted as-is -- see resolve_correction_target below); None
           when lhs_kind is 'that'. 'rhs' is the as-transcribed Y; letters
           is the raw "spelled <...>" tail if present, else None.

    Word-count/atomicity validation happens in validate_correction_pair()
    below, not here -- matches ava_corrections.parse_teaching's own split
    of "parse" vs "validate".
    """
    text = (text or "").strip()

    m = _CORRECTION_SOURCE_PATTERN.match(text)
    if m:
        lhs_token = m.group(1).strip()
        source_kind = _SOURCE_KEYWORD_TO_KIND[m.group(2).lower()]
        if lhs_token.lower() == 'that':
            return {'lhs_kind': 'that', 'lhs_raw': None,
                    'rhs_kind': 'source', 'rhs_source': source_kind}
        return {'lhs_kind': 'named', 'lhs_raw': lhs_token,
                'rhs_kind': 'source', 'rhs_source': source_kind}

    m = _CORRECTION_LITERAL_PATTERNS[0].match(text)  # "correct that to Y..."
    if m:
        rhs = m.group(1).strip()
        letters = m.group(2).strip() if m.group(2) else None
        if rhs:
            return {'lhs_kind': 'that', 'lhs_raw': None,
                    'rhs_kind': 'literal', 'rhs': rhs, 'letters': letters}

    for pattern in _CORRECTION_LITERAL_PATTERNS[1:]:
        m = pattern.match(text)
        if m:
            lhs = m.group(1).strip()
            rhs = m.group(2).strip()
            letters = m.group(3).strip() if m.group(3) else None
            if lhs and rhs:
                return {'lhs_kind': 'named', 'lhs_raw': lhs,
                        'rhs_kind': 'literal', 'rhs': rhs, 'letters': letters}
    return None


def parse_undo(text: str) -> bool:
    return bool(UNDO_PATTERN.match((text or "").strip()))


def parse_forget(text: str) -> "tuple[str, str] | None":
    """Return (kind, phrase) where kind is 'word' or 'correction', else
    None."""
    m = FORGET_PATTERN.match((text or "").strip())
    if m:
        return (m.group(1).lower(), m.group(2).strip())
    return None


def parse_reject(text: str) -> bool:
    """True if text is a bare rejection of a pending confirmation ("no",
    "cancel", etc). Only meaningful while a vocab/correction confirmation
    is actually pending -- the dispatch layer gates on that, not this
    function alone (a bare "no" said outside a pending-confirmation
    context is not this module's concern)."""
    return bool(_REJECT_PATTERN.match((text or "").strip()))


def validate_correction_pair(wrong: str, right: str) -> "tuple[bool, str | None]":
    """Reuse correction_capture.py's ATOMIC-SUBSTITUTION RULE PREDICATES
    (case-only / punctuation-only / max-phrase-words) directly on a
    standalone (wrong, right) pair.

    Deliberately NOT calling correction_capture.extract_corrections()
    itself: that's a whole-SENTENCE diff extractor (original vs corrected
    full text), and empirically it rejects EVERY standalone short pair --
    e.g. extract_corrections("flat", "hat") returns rejected=[('flat',
    'hat', 'looks like a rewrite')] -- because its whole-text rewrite gate
    (matcher.ratio(), calibrated for a mostly-unchanged sentence with a
    small edited region) always fires when the ENTIRE short input differs
    with no common words. That gate doesn't apply to this module's input
    shape (a standalone pair, not a before/after sentence), so only the
    underlying per-span RULES are reused, not the diff machinery around
    them.

    Returns (True, None) or (False, reason)."""
    wrong, right = wrong.strip(), right.strip()
    if not wrong or not right:
        return False, "empty phrase"
    if wrong == right:
        return False, "identical phrase"
    if not _word_count_ok(wrong) or not _word_count_ok(right):
        return False, "too long -- looks like a rewrite"
    if _is_case_only(wrong, right):
        return False, "case-only difference"
    if _is_punctuation_only(wrong, right):
        return False, "punctuation-only difference"
    return True, None


# ============================================================================
# STEP 3A -- buffer-sourced correction targets (spelling-truth channel 1)
# ============================================================================

_LHS_RECENT_WINDOW = 10          # "last ~10 buffer segments" per spec
_LHS_FUZZY_MATCH_THRESHOLD = 0.75  # SequenceMatcher ratio, both whole-segment
                                    # and windowed comparisons (see below)


def _best_match_in_segment(target: str, segment: str) -> "tuple[str, float]":
    """Score `segment` as a source for `target`, returning the best
    candidate SUBSTRING of `segment` (exact literal text, original casing)
    and its match ratio against target.

    Two candidates are considered and the higher-scoring one wins:
      1. The WHOLE segment (handles "that segment basically IS the target",
         e.g. a short dictated utterance that was just the one word/phrase).
      2. The best-matching contiguous word-WINDOW of the same length as
         target within the segment (handles "target is one word/phrase
         buried inside a longer dictated sentence").
    Window matching is what makes storing "the buffer's literal string"
    meaningful when the segment is a full sentence -- without it, a target
    like "flat" against buffer segment "I want to buy a flat today" would
    score a low whole-string ratio and never resolve, even though "flat"
    is right there verbatim.
    """
    whole_ratio = difflib.SequenceMatcher(None, target.lower(), segment.lower()).ratio()
    best_candidate, best_ratio = segment, whole_ratio

    seg_words = segment.split()
    target_word_count = max(1, len(target.split()))
    if target_word_count < len(seg_words):
        for start in range(0, len(seg_words) - target_word_count + 1):
            window = ' '.join(seg_words[start:start + target_word_count])
            ratio = difflib.SequenceMatcher(None, target.lower(), window.lower()).ratio()
            if ratio > best_ratio:
                best_candidate, best_ratio = window, ratio

    return best_candidate, best_ratio


def get_recent_dictated_segments(app, limit: int = _LHS_RECENT_WINDOW) -> "list[str]":
    """Most-recent-first list of up to `limit` plain-dictation segments
    (is_command == False) from this session's in-memory history buffer
    (dictation.py's self.history -- a (timestamp, text, is_command) tuple
    list, capped at self.max_history, appended to on every emitted
    segment). This is the SESSION BUFFER STEP 1(d)/3A refer to: it already
    holds exactly what was actually typed/displayed (post-corrections,
    post-formatting -- see dictation.py's add_to_history call sites, always
    called with the final delivered text), which is what "literal last
    emitted segment" means.

    Command-matched utterances (is_command=True) are excluded -- "correct
    that" should never resolve to Samsara's own previous command
    acknowledgement text.

    Returns [] if app has no history buffer yet (nothing dictated this
    session) or the attribute doesn't exist -- callers must treat that as
    "no match" (see resolve_correction_target), not raise.
    """
    history = getattr(app, 'history', None)
    if not history:
        return []
    segments = [text for (_ts, text, is_command) in reversed(history) if not is_command and text]
    return segments[:limit]


def resolve_correction_target(lhs_kind: str, lhs_raw: "str | None",
                               recent_segments: "list[str]") -> "str | None":
    """Resolve a correction's LHS to a BUFFER-LITERAL string, or None if
    no acceptable match exists (caller must then refuse and persist
    nothing -- never fall back to lhs_raw).

    recent_segments: most-recent-first list of session-buffer text (see
    plugins/commands/ask_ollama.py's get_recent_dictated_segments, which
    reads dictation.py's self.history). Only the first _LHS_RECENT_WINDOW
    entries are considered.

    lhs_kind == 'that': returns recent_segments[0] verbatim (the literal
    most-recent emitted segment) -- no fuzzy matching, no windowing. An
    empty buffer (nothing dictated yet this session) returns None.

    lhs_kind == 'named': fuzzy-matches lhs_raw against each of the last
    _LHS_RECENT_WINDOW segments (via _best_match_in_segment, so a target
    buried inside a longer segment is still found), picks the single best
    match across all of them, and returns it ONLY if its score clears
    _LHS_FUZZY_MATCH_THRESHOLD. Below threshold -> None (refuse; the
    teaching utterance's own lhs_raw is NEVER used as a fallback -- that
    would defeat the entire point of this channel).
    """
    if lhs_kind == 'that':
        return recent_segments[0] if recent_segments else None

    if not lhs_raw or not recent_segments:
        return None

    window = recent_segments[:_LHS_RECENT_WINDOW]
    best_overall_candidate = None
    best_overall_ratio = 0.0
    for segment in window:
        if not segment:
            continue
        candidate, ratio = _best_match_in_segment(lhs_raw, segment)
        if ratio > best_overall_ratio:
            best_overall_candidate, best_overall_ratio = candidate, ratio

    if best_overall_ratio >= _LHS_FUZZY_MATCH_THRESHOLD:
        return best_overall_candidate
    return None


# ============================================================================
# STEP 3B -- selection / clipboard text grabs (spelling-truth channel 3)
# ============================================================================

_SELECTION_POLL_TIMEOUT_S = 0.4
_SELECTION_POLL_INTERVAL_S = 0.02
_SOURCE_TEXT_MAX_CHARS = 60  # generous cap for a 1-4 word phrase


def _sanitize_source_text(raw: "str | None") -> "str | None":
    """Strip surrounding whitespace/punctuation, collapse internal
    whitespace, enforce the 1-4 word bound and a sane length cap. Returns
    None (refuse) on empty or oversized input -- caller speaks the
    refusal, persists nothing."""
    if not raw:
        return None
    text = unicodedata.normalize('NFC', raw).strip()
    text = text.strip('.,;:!?"\'()[]{}')
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return None
    if len(text) > _SOURCE_TEXT_MAX_CHARS:
        return None
    if not _word_count_ok(text):
        return None
    return text


def grab_clipboard_text() -> "str | None":
    """Read the CURRENT clipboard's text content, sanitized. Does not
    mutate the clipboard at all (pure read) -- unlike grab_selection_text,
    there is nothing to save/restore here."""
    import pyperclip
    try:
        raw = pyperclip.paste()
    except Exception:
        return None
    return _sanitize_source_text(raw)


def grab_selection_text(timeout_s: float = _SELECTION_POLL_TIMEOUT_S) -> "str | None":
    """Grab the CURRENTLY SELECTED/HIGHLIGHTED text (not the clipboard's
    existing content) by sending Ctrl+C and reading what changed, then
    restoring the clipboard to exactly what it held before this call.

    Algorithm (spec-mandated): snapshot -> Ctrl+C -> poll the clipboard
    SEQUENCE NUMBER (samsara.clipboard.get_clipboard_sequence_number, from
    commit 4ca1ad8) for up to `timeout_s`; if it never advances, nothing
    was selected (Ctrl+C was a no-op) -- return None. If it advances, read
    the newly-copied text. Either way, the pre-existing clipboard content
    is ALWAYS restored (finally) before returning.

    Deliberately does NOT set ClipboardSnapshot.seq on the pre-Ctrl+C
    snapshot before restoring: that guard exists to detect a USER
    clipboard change during a paste window and skip restoring over it (see
    samsara/clipboard.py's restore_clipboard). Here, the sequence change
    we're about to restore over is OUR OWN Ctrl+C, not a concurrent user
    edit -- setting .seq would make restore_clipboard see "clipboard
    changed since snapshot" (true, but expected and intentional) and
    silently skip the restore, leaving the user's clipboard clobbered with
    whatever we just selected. Leaving .seq at its default None opts this
    call out of that check entirely, which is the correct behavior for
    this specific, self-caused clipboard change.
    """
    import pyautogui
    from samsara import clipboard as clipboard_module

    pre_snapshot = clipboard_module.save_clipboard()
    seq_before = clipboard_module.get_clipboard_sequence_number()
    try:
        pyautogui.hotkey('ctrl', 'c')

        deadline = time.monotonic() + timeout_s
        advanced = False
        while time.monotonic() < deadline:
            current_seq = clipboard_module.get_clipboard_sequence_number()
            if current_seq is not None and seq_before is not None and current_seq != seq_before:
                advanced = True
                break
            if current_seq is None or seq_before is None:
                # Sequence number unavailable (non-Windows / API failure) --
                # can't detect "nothing selected" reliably; treat as if it
                # advanced and let sanitization catch genuinely empty text.
                advanced = True
                break
            time.sleep(_SELECTION_POLL_INTERVAL_S)

        if not advanced:
            return None

        import pyperclip
        try:
            raw = pyperclip.paste()
        except Exception:
            raw = None
        return _sanitize_source_text(raw)
    finally:
        clipboard_module.restore_clipboard(pre_snapshot)


# ============================================================================
# STEP 3C -- dictionary-word check (governs whether letters are required)
# ============================================================================

def is_known_dictionary_word(word: str) -> bool:
    """True if `word` (or, for a multi-word phrase, EVERY space-separated
    token in it) is a known word per the bundled CMU pronouncing
    dictionary (the `pronouncing` package -- already a project dependency,
    see requirements.txt and tools/phonetic_audit.py for the established
    usage pattern). Punctuation (hyphens, apostrophes) makes a token
    unverifiable by this lookup, so any token containing non-alphabetic
    characters fails closed (not a known word) rather than guessing.

    Used ONLY to decide whether a plain (non-spelled) transcription can be
    trusted outright -- an unknown/compound/hyphenated word always falls
    through to "Ava asks to spell it" (STEP 3C), never silently persists
    an unverifiable spelling.
    """
    if not _HAS_PRONOUNCING or not word:
        return False
    for token in word.split():
        if not token.isalpha():
            return False
        if not pronouncing.phones_for_word(token.lower()):
            return False
    return True


# ============================================================================
# Confirmation readback text (STEP 3D)
# ============================================================================

def build_letters_readback(word: str) -> str:
    """'M, O, R, N, E -- Morne' -- spoken letter names (always rendered as
    plain uppercase single characters; letter NAMES read the same whether
    the final stored word is capitalized or not) followed by the assembled
    word, exactly as STEP 3D's example phrasing shows. Non-alphabetic
    characters (hyphen, space, apostrophe) are read out by their own name
    rather than spoken as the raw symbol, which reads oddly through TTS."""
    _SPOKEN_NAMES = {'-': 'hyphen', ' ': 'space', "'": 'apostrophe'}
    parts = [_SPOKEN_NAMES.get(ch, ch.upper()) for ch in word]
    return f"{', '.join(parts)} -- {word}"


def build_vocab_confirmation_prompt(word: str) -> str:
    return f"{build_letters_readback(word)}. Save it to your vocabulary?"


def build_correction_confirmation_prompt(wrong: str, right: str) -> str:
    return f"Correct '{wrong}' to {build_letters_readback(right)}. Save it?"


# ── Last-action stack (in-memory only, THIS session -- "undo that") ──────────
#
# Deliberately not persisted: undo is a live safety net for a just-spoken
# teaching command, not a durable history. Only successful ADDs are
# recorded (per spec -- "undo that" reverses the most recent vocab OR
# correction add, not a forget).

_last_action_lock = threading.Lock()
_last_action = None


def record_last_action(kind: str, **data) -> None:
    global _last_action
    with _last_action_lock:
        _last_action = {'kind': kind, **data}


def pop_last_action() -> "dict | None":
    global _last_action
    with _last_action_lock:
        action = _last_action
        _last_action = None
        return action


def peek_last_action() -> "dict | None":
    """Read-only -- for tests; dispatch code should use pop_last_action()."""
    with _last_action_lock:
        return dict(_last_action) if _last_action is not None else None
