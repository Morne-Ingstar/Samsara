"""The gate between what the model said and what may be staged for apply.

Pure functions -- no I/O, no app, no side effects -- so every guard is
testable on its own.

Honest statement of what this can and cannot do: a sanitizer cannot decide
semantically whether a model "edited" or "answered". What it CAN do is
reject the recognisable SHAPES of an answer, and require that the rewrite
is measurably derived from the source. That is what is implemented. Each
guard returns a short reason string (which becomes Proposal.none's reason,
and reaches the user on the chip) or None to pass.

Modelled on samsara/smart_corrections.py's _sanitize_output, which solved
the same problem for a much narrower edit (a correction is near-identity).
An edit legitimately deviates further -- "make it shorter" may halve the
text -- so the thresholds here are looser and the answer-shape guards do
the work that a tight similarity floor does there.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from functools import lru_cache

#: G5. difflib ratio over normalised text, below which the rewrite is not
#: recognisably derived from the source. 0.40, not smart_corrections'
#: effective ~0.85: SequenceMatcher.ratio() is 2*M/(len(a)+len(b)), so a
#: rewrite that faithfully keeps HALF the source's characters already scores
#: only 2*0.5/1.5 = 0.67, and one that keeps a third scores 0.50. 0.40 admits
#: an aggressive but honest "make it much shorter" while still rejecting a
#: reply that merely shares a topic with the source.
SIMILARITY_FLOOR = 0.40

#: G7. An edit does not double the text. An answer, an elaboration or a
#: "here are three options" does.
GROWTH_CEILING = 2.0

#: G8. A single-line source rewritten into a multi-line block is a
#: structured answer (a list, a table, a preamble + body), not edited prose.
MAX_INJECTED_NEWLINES = 1

#: G3. The opening frames of a model talking TO you instead of emitting the
#: text. Matched at the very start of the output only, case-insensitively.
_PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"sure[,!.]|certainly[,!.]|of course[,!.]|absolutely[,!.]|"
    r"here(?:'s| is| are)\b|"
    r"i(?:'ve| have| will|'ll| can)\b|"
    r"the (?:revised|corrected|edited|updated|rewritten|shortened|new) (?:text|version|sentence)\b|"
    r"(?:revised|corrected|edited|updated|rewritten|result|output|answer)\s*:|"
    r"as an ai\b|"
    r"i'm sorry\b|i am sorry\b|i cannot\b|i can't\b"
    r")",
    re.IGNORECASE,
)

_THINK_OPEN_RE = re.compile(r"<think\b", re.IGNORECASE)
_LEADING_THINK = re.compile(r"\A<think\b[^>]*>.*?</think\s*>", re.I | re.S)
_FENCE = re.compile(r"\A```[ \t]*[\w+-]*[ \t]*\r?\n(.*?)\r?\n[ \t]*```\Z", re.S)
_PASSAGE = re.compile(r"\A<<<PASSAGE[ \t]*\r?\n(.*?)\r?\n[ \t]*PASSAGE>>>\Z", re.I | re.S)
_PASSAGE_MARKER = re.compile(r"<<<PASSAGE|PASSAGE>>>", re.I)
_CLEANUP_HEADER = re.compile(
    r"\A(?:sure[,!.]\s*)?(?:here(?:['\u2019]s| is)\s+)?"
    r"(?:the\s+)?(?:revised|corrected|edited|updated|cleaned(?:[- ]up)?) "
    r"(?:text|passage|version|sentence)\s*:\s*", re.I)

#: G6. A source that ASKS something. If the rewrite stops asking, the model
#: answered the text instead of editing it -- the single crispest signature
#: of the failure this gate exists for, and the one the brief names.
_INTERROGATIVE_OPENERS = frozenset({
    "what", "whats", "why", "how", "when", "where", "who",
    "whose", "which", "is", "are", "was", "were", "do", "does", "did", "can",
    "could", "should", "would", "will", "shall", "am", "have", "has", "may",
})

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_LOCK_TOKEN = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']*[^\s<>\"'.,!?;:)]"
    r"|[\w.+-]+@[\w.-]+\.\w+"
    r"|(?:\b[A-Za-z]:[\\/]|\\\\|/)[^\s<>\"']*[^\s<>\"'.,!?;:)]"
    r"|[^\W_]*\d[^\W_]*(?:[._:/-][^\W_]+)+"
    r"|[-+\u2212]?(?:[$\u00a3\u20ac])?\d+(?:,\d{3})*(?:\.\d+)?%?(?![^\W_])"
    r"|[^\W_]+(?:[_'\u2019][^\W_]+)*"
    r"|\S", re.I)
_CONTRACTIONS = {
    "can't": (("can", "not"), ("cannot",)),
    "won't": (("will", "not"),),
    "shan't": (("shall", "not"),),
    "let's": (("let", "us"),),
    "i'm": (("i", "am"),),
}
_S_CONTRACTIONS = frozenset(
    "he she it that there here what who where when how why".split())
_RE_VE_SUBJECTS = frozenset("you we they".split())
_LL_D_SUBJECTS = _RE_VE_SUBJECTS | {"i", "he", "she", "it", "that", "there", "who"}
_NEGATIVE_AUXILIARIES = frozenset(
    "are is was were do does did have has had could should would must need dare".split())


def _expansions(word):
    if word in _CONTRACTIONS:
        return _CONTRACTIONS[word]
    if word.endswith("n't") and word[:-3] in _NEGATIVE_AUXILIARIES:
        return ((word[:-3], "not"),)
    stem, _, suffix = word.partition("'")
    if suffix == "s" and stem in _S_CONTRACTIONS:
        return ((stem, "is"), (stem, "has"))
    if suffix == "re" and stem in _RE_VE_SUBJECTS:
        return ((stem, "are"),)
    if suffix == "ve" and stem in _RE_VE_SUBJECTS | {"i", "could", "should", "would", "might", "must"}:
        return ((stem, "have"),)
    if suffix == "ll" and stem in _LL_D_SUBJECTS:
        return ((stem, "will"),)
    if suffix == "d" and stem in _LL_D_SUBJECTS:
        return ((stem, "had"), (stem, "would"))
    return ()


@lru_cache(maxsize=2048)
def _phones(word):
    try:
        import pronouncing
    except ImportError:
        return ()
    return tuple(tuple(phone.rstrip("012") for phone in form.split())
                 for form in pronouncing.phones_for_word(word))


_SOUND_CONFUSIONS = {frozenset(pair.split()) for pair in (
    "B P", "D T", "G K", "V F", "Z S", "ZH SH", "DH TH",
    "IH IY", "EH AE", "AH AO", "UH UW",
)}
_SPELLING = re.compile(r"[a-z]+(?:'[a-z]+)?\Z")
_REPEATED_LETTER = re.compile(r"([a-z])\1+")


def _same_sound(source, target):
    if not (_SPELLING.fullmatch(source) and _SPELLING.fullmatch(target)):
        return False
    before, after = _phones(source), _phones(target)
    if not before and after and len(target) >= 6 and len(source) == len(target) + 1:
        # One extra repeated letter in an unknown spelling. Never use generic
        # character similarity for two real words or two unknown spellings.
        return any(source[:run.start()] + source[run.start() + 1:] == target
                   for run in _REPEATED_LETTER.finditer(source))
    for left in before:
        for right in after:
            if left == right:
                return True
            # One easily confused sound in a longer word. No phone insertion
            # or deletion, which could admit a changed tense or plural.
            if len(left) == len(right) and len(left) >= 5:
                differences = [frozenset((a, b)) for a, b in zip(left, right) if a != b]
                if len(differences) == 1 and differences[0] in _SOUND_CONFUSIONS:
                    return True
    return False


def _locked_tokens(text):
    return tuple(word.lower().replace("\u2019", "'")
                 if _WORD_RE.fullmatch(word.replace("'", "").replace("\u2019", "")) else word
                 for word in _LOCK_TOKEN.findall(unicodedata.normalize("NFC", text))
                 if len(word) != 1 or word in "&@#" or not unicodedata.category(word).startswith("P"))


def word_lock(original: str, rewritten: str) -> bool:
    """Align ordered tokens using identity, an exact expansion, or one sound match."""
    before, after = _locked_tokens(original), _locked_tokens(rewritten)
    end = len(before), len(after)
    pending = [(0, 0, frozenset(), frozenset())]
    visited = set()
    while pending:
        i, j, removed, added = state = pending.pop()
        if state in visited:
            continue
        visited.add(state)
        if (i, j) == end:
            return True
        if i == end[0] or j == end[1]:
            continue
        left, right = before[i], after[j]
        if left == right:
            pending.append((i + 1, j + 1, removed, added))
            continue
        for expansion in _expansions(left):
            if after[j:j + len(expansion)] == expansion:
                pending.append((i + 1, j + len(expansion), removed, added))
        for expansion in _expansions(right):
            if before[i:i + len(expansion)] == expansion:
                pending.append((i + len(expansion), j + 1, removed, added))
        # A word that disappears at one position and reappears at another
        # is a move, even if both individual replacements sound identical.
        if left not in added and right not in removed and _same_sound(left, right):
            pending.append((i + 1, j + 1, removed | {left}, added | {right}))
    return False


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace, drop non-word characters. Used only
    for MEASURING similarity -- never for anything that gets applied."""
    return " ".join(_WORD_RE.findall((text or "").lower()))


def similarity(original: str, rewritten: str) -> float:
    """Character overlap over normalized text, for the generic edit guard."""
    a, b = _norm(original), _norm(rewritten)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.rstrip().endswith("?"):
        return True
    words = _norm(t).split()
    return bool(words) and words[0] in _INTERROGATIVE_OPENERS


def clean(raw: str, original: str = "", *, cleanup: bool = False) -> str:
    """Unwrap one layer of each known model wrapper, preserving source wrappers."""
    text = (raw or "").strip()
    source = original.strip()
    wrappers = [_LEADING_THINK, _FENCE, _PASSAGE]
    if cleanup:
        wrappers.append(_CLEANUP_HEADER)
    pairs = (('"', '"'), (chr(0x2018), chr(0x2019)), (chr(0x201C), chr(0x201D)))
    stripped_quotes = False
    while text:
        for wrapper in wrappers:
            match = wrapper.match(text)
            if match is None or wrapper.match(source):
                continue
            body = match[1] if wrapper in (_FENCE, _PASSAGE) else text[match.end():]
            if wrapper is _CLEANUP_HEADER and not word_lock(
                    original, clean(body, original)):
                continue
            text = body.strip()
            wrappers.remove(wrapper)
            break
        else:
            if stripped_quotes or len(text) < 2:
                break
            for opening, closing in pairs:
                if text.startswith(opening) and text.endswith(closing) and not source.startswith(opening):
                    text = text[1:-1].strip()
                    stripped_quotes = True
                    break
            else:
                break
    return text


def check(raw: str, original: str, instruction: str = "", *, cleanup: bool = False) -> "str | None":
    """Run every guard in order. Returns the reason the rewrite is refused,
    or None when it may be staged.

    Cleanup unwraps model packaging and requires ordered word alignment.
    The generic edit guards allow broader changes and reject these shapes:

      G1 empty            - the model returned nothing usable.
      G2 artifact         - a <think> block or a code fence the SOURCE did
                            not have: the model went off-script. Rejected
                            outright rather than stripped, because stripping
                            can eat real dictated content that happened to
                            sit next to the artifact.
      G3 preamble         - the output OPENS by talking to the user
                            ("Sure,", "Here's the revised text:", "I've
                            shortened it"). The signature of a model that
                            replied rather than emitted prose.
      G4 instruction echo - the rewrite contains the instruction itself; the
                            model repeated the command back.
      G5 not derived      - similarity() below SIMILARITY_FLOOR: the rewrite
                            is not recognisably the source's text.
      G6 answered         - the source asks a question and the rewrite does
                            not. The model answered the text.
      G7 grew             - more than GROWTH_CEILING x the source's word
                            count. Edits do not double text; answers do.
      G8 restructured     - a single-line source came back as a multi-line
                            block: a list or a preamble+body answer.
    """
    if not (raw or "").strip():
        return "empty"

    text = clean(raw, original, cleanup=cleanup)
    artifact_text = text if cleanup else raw
    if _THINK_OPEN_RE.search(artifact_text) and not _THINK_OPEN_RE.search(original):
        return "model went off-script"
    if "```" in artifact_text and "```" not in original:
        return "model went off-script"
    if not text:
        return "empty"

    if cleanup:
        if (_PASSAGE_MARKER.search(text) and not _PASSAGE_MARKER.search(original)):
            return "model went off-script"
        # PLACEHOLDER COPY (1288)
        return None if word_lock(original, text) else "the model changed the wording"

    if _PREAMBLE_RE.match(text) and not _PREAMBLE_RE.match(original):
        return "model replied instead of editing"

    norm_instruction = _norm(instruction)
    if (norm_instruction and len(norm_instruction) >= 8
            and norm_instruction in _norm(text)
            and norm_instruction not in _norm(original)):
        return "model repeated the instruction"

    ratio = similarity(original, text)
    if ratio < SIMILARITY_FLOOR:
        return f"not derived from the text ({ratio:.2f} < {SIMILARITY_FLOOR:.2f})"

    if _is_question(original) and not _is_question(text):
        return "model answered instead of editing"

    orig_words = original.split()
    new_words = text.split()
    if orig_words and len(new_words) > GROWTH_CEILING * len(orig_words):
        return "rewrite grew too much"

    if original.count("\n") == 0 and text.count("\n") > MAX_INJECTED_NEWLINES:
        return "model returned a structured answer"

    return None
