"""Pure dictation fragment seams and commit re-decode quality policy."""
from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Optional


def span_needs_redecode(durations, uncertain_seam=False) -> bool:
    """1258: only multi-fragment spans with boundary evidence need a pass."""
    return len(durations) >= 2 and (uncertain_seam or any(s < 1.5 for s in durations))


def stitch_span_audio(fragments, rate=16000):
    """1258: speech-only clips, 160 ms seams and 10 ms fades through silence.

    Inputs come from the retained VAD store, so capture-tail silence is already
    removed. Fade each side into the quiet seam rather than overlap phonemes.
    Refuse incomplete/oversized evidence instead of decoding a partial span.
    """
    import numpy as np
    if rate != 16000 or not fragments:
        return None
    clips = [np.asarray(pcm, dtype=np.float32) for pcm in fragments]
    pause, fade = round(.160 * rate), round(.010 * rate)
    if any(pcm.ndim != 1 or not len(pcm) or not np.isfinite(pcm).all() for pcm in clips):
        return None
    total = sum(len(pcm) for pcm in clips) + pause * (len(clips) - 1)
    if total > 20 * rate:
        return None
    result = np.zeros(total, dtype=np.float32)
    offset = 0
    for index, pcm in enumerate(clips):
        target = result[offset:offset + len(pcm)]
        target[:] = pcm
        width = min(fade, len(pcm) // 2)
        if width and index:
            target[:width] *= np.linspace(0, 1, width, dtype=np.float32)
        if width and index < len(clips) - 1:
            target[-width:] *= np.linspace(1, 0, width, dtype=np.float32)
        offset += len(pcm) + pause
    return result


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

_PRONOUN_I = re.compile(r"\AI(?:['’](?:m|ll|ve|d))?\Z")

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

    def stage_chunk(self, prepared: str, app=None) -> str:
        """Apply only boundary punctuation/case; retain the prepared interior.

        Vocabulary is an in-memory snapshot of the composing application's
        dictionary, never a profile read. Edited punctuation has no stripped
        suffix and therefore never receives a replacement period here.
        """
        if self.clause == "empty":
            return ""
        chunk = _LEADING_ELLIPSIS.sub("", prepared).strip() if self.lead_stripped else prepared.strip()
        if not chunk:
            return ""
        if self.join and self.clause != "ellipsis_only":
            word = re.search(r"[^\W\d_]+(?:['’][^\W\d_]+)*", chunk)
            if word:
                core = word.group()
                store = getattr(app, 'dictionary_store', None)
                if store is None:
                    store = getattr(app, 'voice_training_window', None)
                vocabulary = list(getattr(store, 'custom_vocab', ()))
                vocabulary.extend(getattr(store, 'corrections_dict', {}).values())
                known_capital = any(
                    token.casefold() == core.casefold() and token[:1].isupper()
                    for entry in vocabulary for token in re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*", entry)
                )
                if (not any(c.isalnum() for c in chunk[:word.start()])
                        and core[:1].isupper() and not _PRONOUN_I.fullmatch(core)
                        and not core.isupper() and not any(c.isupper() for c in core[1:])
                        and not known_capital):
                    chunk = chunk[:word.start()] + core[0].lower() + chunk[word.start() + 1:]
        return (". " if self.stripped and not self.join else " ") + chunk

    @staticmethod
    def sentence_complete(fragment: str) -> bool:
        """Recognize clear closed clauses, not arbitrary capitalization.

        This deliberately small grammar leaves uncertain tails joined. It is
        not a general English parser: an open function word, auxiliary or
        participle cannot establish a complete clause on its own.
        """
        clause = re.split(r"[.!?…]+", fragment)[-1].strip()
        words = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", clause.lower())
        if len(words) < 2:
            return False
        open_tail = _SENTENCE_CANNOT_END_WORDS | _OPEN_CLAUSE_CONJUNCTIONS | {
            'to', 'of', 'for', 'with', 'from', 'at', 'in', 'on', 'by', 'about',
            'into', 'as', 'if', 'when', 'while', 'than', 'that', 'which', 'who',
            'is', 'are', 'was', 'were', 'be', 'been', 'being', 'am', 'will',
            'would', 'can', 'could', 'should', 'must', 'may', 'might', 'have',
            'has', 'had', 'not', 'very', 'so', 'too',
            'whole', 'other', 'same', 'next', 'last', 'first', 'another',
        }
        if words[-1] in open_tail or words[-1].endswith(('ing', 'ly')):
            return False
        # 1253: a copula also occurs inside dependent clauses. Leave those
        # uncertain pauses joined; the whole-draft re-decode supplies periods.
        # Unambiguous closed predicates and past-tense clauses with an object.
        if words[-1] in {'worked', 'stopped', 'finished', 'arrived', 'left', 'ended', 'failed'}:
            return True
        return (len(words) >= 3 and words[0] in {'i', 'you', 'he', 'she', 'it', 'we', 'they'}
                and words[1].endswith('ed'))


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
                         previous_fragment: Optional[str] = None, *,
                         decoder_trailing: bool = True) -> Optional[EllipsisSeam]:
    """1250: decoder boundary dots mark a pause, never spoken punctuation.

    Remove both sides of a seam, including a leading-only marker. Keep a
    single period for a clearly completed clause; join uncertain tails.
    The caller disables trailing removal after a deliberate draft edit.
    No seam is decided until a subsequent nonempty chunk arrives, so the
    final trailing ellipsis and ellipses inside either chunk are untouched.
    """
    previous = (previous_text or "").rstrip()
    match = _TRAILING_ELLIPSIS.search(previous) if decoder_trailing else None
    raw = new_chunk_raw or ""
    lead = _LEADING_ELLIPSIS.match(raw)
    if match is None and lead is None:
        return None
    lead_stripped = lead.group(0) if lead else ""
    if lead_stripped:
        raw = raw[lead.end():]
    new_tokens = raw.strip().split()
    if not new_tokens:
        return EllipsisSeam(join=False, clause="empty", lead_stripped=lead_stripped)
    body = previous[:match.start()].rstrip() if match else previous
    stripped = previous_text[len(body):] if match else ""
    prior_words = body.split()
    if not prior_words:
        return EllipsisSeam(join=True, clause="ellipsis_only", stripped=stripped,
                            lead_stripped=lead_stripped)

    if match is None:
        return EllipsisSeam(join=not chunk_ends_terminal(body), clause="ellipsis_leading",
                            lead_stripped=lead_stripped)

    fragment = previous_fragment if previous_fragment is not None else previous
    fragment = _TRAILING_ELLIPSIS.sub("", fragment).rstrip()
    if EllipsisSeam.sentence_complete(fragment):
        return EllipsisSeam(join=False, clause="ellipsis_new_sentence", stripped=stripped,
                            lead_stripped=lead_stripped)

    first = new_tokens[0]
    first_alpha = next((ch for ch in first if ch.isalpha()), "")
    if first_alpha and first_alpha.islower() and first[0].isalpha():
        return EllipsisSeam(join=True, clause="ellipsis_lowercase", stripped=stripped,
                            lead_stripped=lead_stripped)

    fragment_words = [w.strip(string.punctuation + "…").lower()
                      for w in fragment.split()]
    if fragment_words and all(w in _HESITATION_SOUNDS for w in fragment_words):
        return EllipsisSeam(join=True, clause="ellipsis_hesitation_only", stripped=stripped,
                            lead_stripped=lead_stripped)

    last_word = prior_words[-1].strip(string.punctuation).lower()
    if last_word in _SENTENCE_CANNOT_END_WORDS:
        return EllipsisSeam(join=True, clause="ellipsis_determiner", stripped=stripped,
                            lead_stripped=lead_stripped)
    if (last_word in _OPEN_CLAUSE_CONJUNCTIONS
            and _PRONOUN_I.match(first.rstrip(string.punctuation))):
        return EllipsisSeam(join=True, clause="ellipsis_conjunction_i", stripped=stripped,
                            lead_stripped=lead_stripped)
    return EllipsisSeam(join=True, clause="ellipsis_capitalized", stripped=stripped,
                        lead_stripped=lead_stripped)


def chunk_ends_terminal(text: str) -> bool:
    """True if text's last non-whitespace character is terminal punctuation."""
    t = (text or "").rstrip()
    return bool(t) and t[-1] in TERMINAL_PUNCTUATION
