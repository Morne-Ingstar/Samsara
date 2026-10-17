"""Pure dictation fragment seams and commit re-decode quality policy."""
from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Optional


TERMINAL_PUNCTUATION = ".!?:;"

_LEADING_FILLERS = ("um", "uh", "uhh", "umm", "like", "so", "well", "okay", "ok")


# -- Queue 88: is the commit re-decode better than the staged text? ---------
#
# The commit re-decode (dictation.py's _dictate_commit_redecode) hands Whisper
# the whole stitched thought so it can hear across the pause boundaries the
# staged fragments were cut on. Usually that is a large win. Sometimes it is
# not: Whisper's long-form loop can settle into a run-on transcript, opening a
# new segment with a capital and never terminating the previous one, and the
# result is measurably WORSE punctuated than the fragments it replaced.
#
#   2026-09-15 16:23:58, 107.6 s stitched, committed 1,140 chars:
#     staged    " When they come up, the, the window's faded, so you can't
#                really see it." / " I mean, if you squint, you kind of can."
#     re-decode "...the windows faded, so you can't really see it. I mean if
#                you squint you kind of can I'm assuming we haven't You know
#                blah blah blah I guess I'll just keep talking..."
#
# Measured over the 37 DICTATE commits in that day's log, the re-decode's OWN
# sentence-mark density separates the failures from the good re-decodes with
# no overlap (bad: 0.004-0.016 marks/word; good: 0.029-0.176). The ratio to
# the staged text does NOT separate them on its own -- a choppy staged buffer
# carries an absurd density of its own (one full stop every two or three
# words), so a good re-decode of it can still look like a big drop.
_SENTENCE_MARK_RE = re.compile(r"[.!?…]")
_DENSITY_WORD_RE = re.compile(r"[A-Za-z0-9'’]+")

#: A re-decode with fewer than one sentence mark per this many words is a
#: run-on, independently of what it was decoded from. Ordinary dictated prose
#: sits near one mark per 15-25 words.
REDECODE_RUN_ON_WORDS_PER_MARK = 40.0

#: ...and the staged text only wins if it is materially better punctuated,
#: not merely different. Both guards must fire.
REDECODE_STAGED_ADVANTAGE = 2.0
REDECODE_MIN_STAGED_MARKS = 2


def sentence_mark_density(text: str) -> float:
    """Sentence-ending marks (``. ! ? …``) per word. 0.0 for empty text."""
    words = len(_DENSITY_WORD_RE.findall(text or ""))
    if not words:
        return 0.0
    return len(_SENTENCE_MARK_RE.findall(text)) / words


def redecode_is_poorer(staged: str, redecoded: str) -> bool:
    """True when the commit re-decode is worse punctuated than the staged
    fragments it would replace, so the staged text must be committed instead.

    Deliberately asymmetric: the re-decode is preferred unless it is BOTH an
    objective run-on AND clearly beaten by the staged text. Joining fragments
    legitimately removes sentence ends, and the whole point of the re-decode
    is to rescue a staged buffer that has little punctuation of its own -- so
    a staged text with fewer than REDECODE_MIN_STAGED_MARKS marks, or without
    REDECODE_STAGED_ADVANTAGE times the density, never wins.
    """
    if not (staged or "").strip() or not (redecoded or "").strip():
        return False
    redecoded_density = sentence_mark_density(redecoded)
    if redecoded_density * REDECODE_RUN_ON_WORDS_PER_MARK >= 1.0:
        return False
    if len(_SENTENCE_MARK_RE.findall(staged)) < REDECODE_MIN_STAGED_MARKS:
        return False
    return sentence_mark_density(staged) >= REDECODE_STAGED_ADVANTAGE * redecoded_density


# ---------------------------------------------------------------------------
# DICTATE chunk seam-join heuristic
# ---------------------------------------------------------------------------


def _is_continuation_chunk(new_chunk_raw: str) -> bool:
    """Return True when the chunk looks like a continuation of prior prose.

    The same leading-filler skip used by ``seam_join`` is applied first;
    after that, a lower-case alphabetic first character means continuation,
    while uppercase or non-alpha start means new sentence.

    Queue 81: the fragment's own first word must start lower case too. Whisper
    capitalises the first word of a decode it treats as a new sentence, so a
    capitalised filler ("So, what's the solution there?", "Okay, now I'm
    reading") is a sentence start; skipping it and reading the lower-case word
    after it welded real sentences together in the owner's logs.
    """
    stripped = (new_chunk_raw or "").strip()
    if not stripped:
        return False
    if not stripped[0].islower():
        return False

    tokens = stripped.split()
    idx = 0
    while idx < len(tokens) and tokens[idx].strip(string.punctuation).lower() in _LEADING_FILLERS:
        idx += 1
    if idx >= len(tokens):
        return False

    seam_word = tokens[idx]
    return bool(seam_word) and seam_word[0].isalpha() and seam_word[0].islower()


def seam_join(previous_chunk_ended_terminal: bool, new_chunk_raw: str) -> str:
    """Case-adjust a new DICTATE chunk for joining onto the previous one.

    Returns the chunk text only (no leading space) -- the caller prepends a
    single space when injecting a non-first chunk, mirroring the existing
    wake-session dictation pattern (_output_dictation: ' ' + text).

    If the previous chunk ended in terminal punctuation (. ! ? : ;), this is
    a fresh sentence -- text is returned unchanged, no case adjustment.

    Otherwise the seam word is lowercased UNLESS it looks like a genuine
    proper noun rather than Whisper's automatic utterance-initial
    capitalization. Heuristic: Whisper always capitalizes the very first
    word of whatever it transcribes, positionally, regardless of content.
    If the chunk's raw text has filler words ("um", "so", ...) BEFORE the
    capitalized token, that capital landed past position 0 of Whisper's own
    output -- not explainable by the automatic sentence-initial rule, so
    it's kept. If the capitalized token IS literally the first word Whisper
    produced (no fillers ahead of it), the capitalization is presumed
    automatic and gets lowercased.
    """
    stripped = (new_chunk_raw or "").strip()
    if not stripped:
        return stripped
    if previous_chunk_ended_terminal:
        return stripped

    tokens = stripped.split()
    idx = 0
    while idx < len(tokens) and tokens[idx].strip(string.punctuation).lower() in _LEADING_FILLERS:
        idx += 1
    if idx >= len(tokens):
        return stripped  # entirely filler words -- nothing to adjust

    seam_word = tokens[idx]
    fillers_were_skipped = idx > 0
    core = seam_word.strip(string.punctuation)
    looks_capitalized = bool(core) and core[0].isupper()

    if looks_capitalized and not fillers_were_skipped:
        tokens[idx] = seam_word[0].lower() + seam_word[1:]

    return " ".join(tokens)


# Queue 81 (2026-09-15): when a pause splits one sentence, Whisper marks the
# break itself -- it ends the unfinished fragment with an ellipsis and, given
# the DICTATE context tail as initial_prompt, starts the rest in lower case.
# The branch below used to strip ONE full stop, so "do we..." became "do we..",
# and commit-time auto-capitalisation then turned ".. deal" into ".. Deal".
# Only seams whose previous fragment ends in an ellipsis are decided here; any
# other seam keeps the single-period / seam_join handling unchanged.
_TRAILING_ELLIPSIS = re.compile(r"(?:\.{2,}|…)\s*\Z")

# A sentence cannot end on these words, so an ellipsis after one is always a
# trailing-off mid-sentence, whatever case the next fragment starts in.
_SENTENCE_CANNOT_END_WORDS = frozenset({"a", "an", "the", "my", "your", "our", "their"})

# Joining words that leave a clause open. With these, only a next fragment
# starting with the pronoun "I" -- whose capital carries no sentence-start
# information -- is treated as the continuation.
_OPEN_CLAUSE_CONJUNCTIONS = frozenset({"and", "but", "or", "because", "cause"})

_PRONOUN_I = re.compile(r"\AI(?:'(?:m|ll|ve|d))?\Z")

# Words that cannot follow an article or possessive, so a fragment starting
# with one after "the..." is the speaker restarting, not finishing the phrase
# (owner log: "the..." + "There's the six little squares"). Contractions are
# excluded by the apostrophe check in decide_ellipsis_seam.
_CANNOT_FOLLOW_DETERMINER = frozenset({
    "i", "you", "he", "she", "it", "we", "they", "there", "this", "that", "these",
    "those", "what", "which", "who", "when", "where", "why", "how", "if", "so",
    "and", "but", "or", "because", "the", "a", "an", "my", "your", "our", "their",
    "is", "are", "was", "were", "do", "does", "did", "can", "could", "should",
    "would", "will", "not", "no", "yes", "okay", "ok", "um", "uh", "like", "well",
})


@dataclass(frozen=True)
class EllipsisSeam:
    """How to stage a fragment after a fragment that ended in an ellipsis."""
    join: bool
    clause: str
    stripped: str = ""  # exact trailing text removed from the pending buffer
    lead_stripped: str = ""  # leading ellipsis removed from the NEW chunk


#: Whisper marks a continuation by ENDING the previous fragment with an ellipsis
#: and STARTING the next one with another ("when you send a window..." then
#: "...to the left"). The leading one is a decoder artifact, not speech, and it
#: used to defeat the very clause meant to catch this case: the first token was
#: "...to", so `first[0].isalpha()` was False and every clause below fell
#: through to ellipsis_new_sentence. Owner log 2026-09-16 12:09, four seams in
#: one thought: "So I was...... at the store. And then...... this guy".
_LEADING_ELLIPSIS = re.compile(r"\A\s*(?:\.{2,}|…)\s*")


# A fragment that is nothing but a hesitation sound carries no sentence of its
# own; clean_text later deletes "um"/"uh", which would leave its ellipsis glued
# to the previous full stop ("not good.... So how").
_HESITATION_SOUNDS = frozenset({"um", "umm", "uh", "uhh", "er", "erm", "hmm", "ah"})


def decide_ellipsis_seam(previous_text: str, new_chunk_raw: str,
                         previous_fragment: Optional[str] = None) -> Optional[EllipsisSeam]:
    """Decide join-or-separate for a seam whose previous fragment ends in an
    ellipsis ("..", "..." or the ellipsis character). Returns None for any
    other seam, which the caller handles as before.

    JOIN (strip the ellipsis, one space, the new fragment's text unchanged):
      ellipsis_lowercase    the new fragment's first word starts lower case
      ellipsis_determiner   the word before the ellipsis is an article or
                            possessive ("the...", "your...") and the new
                            fragment does not start with a contraction or a
                            word that cannot follow one (pronoun, verb "is",
                            conjunction, ...); its case is kept, so a name
                            stays capitalised ("the.." + "GitHub")
      ellipsis_conjunction_i  the word before the ellipsis is and/but/or/
                            because/cause and the new fragment starts with
                            the pronoun I (I, I'm, I'll, I've, I'd)
      ellipsis_hesitation_only  previous_fragment (the last staged fragment
                            on its own) is only hesitation sounds ("Um...");
                            the new fragment's case is kept
    Otherwise SEPARATE and the ellipsis stays: an upper-case start after
    anything else is a new sentence or a name, and a wrong join is harder to
    notice and fix by voice than an extra stop.
    """
    previous = (previous_text or "").rstrip()
    match = _TRAILING_ELLIPSIS.search(previous)
    if match is None:
        return None
    raw = new_chunk_raw or ""
    lead = _LEADING_ELLIPSIS.match(raw)
    lead_stripped = lead.group(0) if lead else ""
    if lead_stripped:
        raw = raw[lead.end():]
    new_tokens = raw.strip().split()
    if not new_tokens:
        return EllipsisSeam(join=False, clause="empty", lead_stripped=lead_stripped)
    body = previous[:match.start()].rstrip()
    prior_words = body.split()
    if not prior_words:
        return EllipsisSeam(join=False, clause="ellipsis_only", lead_stripped=lead_stripped)
    stripped = previous_text[len(body):]

    first = new_tokens[0]
    first_alpha = next((ch for ch in first if ch.isalpha()), "")
    if first_alpha and first_alpha.islower() and first[0].isalpha():
        return EllipsisSeam(join=True, clause="ellipsis_lowercase", stripped=stripped,
                            lead_stripped=lead_stripped)

    fragment_words = [w.strip(string.punctuation + "…").lower()
                      for w in (previous_fragment or "").split()]
    if fragment_words and all(w in _HESITATION_SOUNDS for w in fragment_words):
        return EllipsisSeam(join=True, clause="ellipsis_hesitation_only", stripped=stripped,
                            lead_stripped=lead_stripped)

    last_word = prior_words[-1].strip(string.punctuation).lower()
    first_word = first.strip(string.punctuation)
    if (last_word in _SENTENCE_CANNOT_END_WORDS
            and first[0].isalnum()
            and "'" not in first_word and "’" not in first_word
            and first_word.lower() not in _CANNOT_FOLLOW_DETERMINER):
        return EllipsisSeam(join=True, clause="ellipsis_determiner", stripped=stripped,
                            lead_stripped=lead_stripped)
    if (last_word in _OPEN_CLAUSE_CONJUNCTIONS
            and _PRONOUN_I.match(first.rstrip(string.punctuation))):
        return EllipsisSeam(join=True, clause="ellipsis_conjunction_i", stripped=stripped,
                            lead_stripped=lead_stripped)
    return EllipsisSeam(join=False, clause="ellipsis_new_sentence", lead_stripped=lead_stripped)


def chunk_ends_terminal(text: str) -> bool:
    """True if text's last non-whitespace character is terminal punctuation."""
    t = (text or "").rstrip()
    return bool(t) and t[-1] in TERMINAL_PUNCTUATION
