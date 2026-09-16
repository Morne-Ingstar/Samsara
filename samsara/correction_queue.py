"""Review-gated queue of correction pairs captured from the live dictation
preview (queue 85: click a word, say the replacement).

Nothing in this module ever writes to the corrections dictionary. A captured
pair lands here as "pending" and stays there until a human accepts it in the
correction-capture window; accepting is what writes
`voice_training_window.corrections_dict` (see samsara/ui/correction_capture_qt
_on_always_fix), exactly as correction capture v1 already does. A corrupt
queue file therefore cannot damage the dictionary: they are separate files,
and this one is rebuilt from scratch if it will not parse.

Three classes of pair, decided by `classify`:

  never   -- refused outright and not queued at all. Context-dependent
             homophones ("to"/"two"/"too"), case-only and punctuation-only
             edits, rewrites, and any pair whose WRONG side is a very common
             word: a dictionary entry replaces that word in EVERY future
             dictation, so "to" -> "two" would quietly corrupt everything the
             user dictates from then on. These are one-off draft fixes only.
  confirm -- queued as pending, shown for review. Everything learnable.
             `count` rises each time the same pair is captured again, which is
             what separates a systematic mishearing from a one-off proper noun;
             the review window can sort by it.
  (accepted/rejected) -- the state a reviewed pair moves to. `undo_last_accepted`
             hands the most recently accepted pair back so a voice command can
             remove it from the dictionary again.

Pure storage plus classification: no Qt, no audio decoding, no dictionary
writes. Audio is referenced by path only; the caller writes the file.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Optional

from samsara.correction_capture import extract_corrections
from samsara.log import get_logger

logger = get_logger(__name__)

QUEUE_FILENAME = "correction_queue.json"
QUEUE_VERSION = 1
#: Hard cap. Oldest pending entries are dropped first; accepted history is
#: kept longer because the voice undo reads it.
MAX_PENDING = 300
MAX_ACCEPTED = 100

#: Sets of words a dictionary substitution must never touch: which one is
#: right depends on the sentence, so a global "X -> Y" rule is wrong about
#: half the time. A pair whose two sides fall in the same set is refused.
HOMOPHONE_SETS = (
    frozenset({"to", "too", "two"}),
    frozenset({"there", "their", "theyre", "they're"}),
    frozenset({"its", "it's"}),
    frozenset({"your", "you're", "youre"}),
    frozenset({"then", "than"}),
    frozenset({"hear", "here"}),
    frozenset({"were", "we're", "weren't", "where", "wear"}),
    frozenset({"affect", "effect"}),
    frozenset({"accept", "except"}),
    frozenset({"lose", "loose"}),
    frozenset({"who's", "whose", "whos"}),
    frozenset({"by", "buy", "bye"}),
    frozenset({"no", "know"}),
    frozenset({"right", "write", "rite"}),
    frozenset({"one", "won"}),
    frozenset({"for", "four", "fore"}),
)

#: Never learn a rule that rewrites one of these on sight. They are too common
#: for a global substitution to be safe, whatever the user meant in one draft.
COMMON_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "can", "did", "do", "does",
    "for", "from", "get", "go", "had", "has", "have", "he", "her", "him", "his", "i", "if",
    "in", "is", "it", "just", "like", "me", "my", "not", "of", "on", "or", "our", "out",
    "said", "say", "she", "so", "that", "the", "them", "they", "this", "to", "up", "us",
    "was", "we", "what", "when", "will", "with", "would", "you",
})

NEVER_HOMOPHONE = "homophone -- which one is right depends on the sentence"
NEVER_COMMON = "too common a word to rewrite everywhere"
NEVER_SAME = "no difference once case and punctuation are ignored"


def _phoneme_distance(left: list[str], right: list[str]) -> int:
    """Levenshtein distance for the CMU phoneme lists used by the project's
    existing phonetic audit.  Keeping it here makes the candidate table usable
    in the packaged app, where developer-only ``tools/`` is not installed."""
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, item in enumerate(left, 1):
        current = [i] + [0] * len(right)
        for j, other in enumerate(right, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1,
                             previous[j - 1] + (item != other))
        previous = current
    return previous[-1]


def phonetic_neighbours(word: str, terms, limit: int = 5) -> list[str]:
    """Return likely one-word replacements from *terms*, closest first.

    This is deliberately a tiny, user-owned table rather than an English-word
    thesaurus: the choices are vocabulary or corrections the person has
    already taught Samsara.  Known words use CMU phonemes; proper names absent
    from CMUdict fall back to a strict spelling-neighbour check.
    """
    try:
        import pronouncing  # noqa: PLC0415 -- an existing project dependency
    except Exception:
        return []
    source = (word or "").strip()
    if not source:
        return []
    source_phones = [p.split() for p in pronouncing.phones_for_word(source.lower())]
    seen, ranked = set(), []
    for term in terms or ():
        candidate = " ".join(str(term or "").split())
        folded = candidate.casefold()
        if (not candidate or folded == source.casefold() or folded in seen
                or not re.fullmatch(r"[A-Za-z]+(?:['-][A-Za-z]+)*", candidate)):
            continue
        seen.add(folded)
        candidate_phones = [p.split() for p in pronouncing.phones_for_word(candidate.lower())]
        # Proper names are why this feature exists and are often absent from
        # CMUdict.  Compare their spellings only when either side has no CMU
        # pronunciation; this preserves useful one-letter name variants while
        # keeping ordinary words on the phoneme path.
        if not source_phones or not candidate_phones:
            source_variants = [list(source.casefold())]
            candidate_variants = [list(candidate.casefold())]
        else:
            source_variants = source_phones
            candidate_variants = candidate_phones
        distance = min(_phoneme_distance(left, right)
                       for left in source_variants for right in candidate_variants)
        phonemes = min(max(len(left), len(right))
                       for left in source_variants for right in candidate_variants)
        # Exact homophones always qualify.  For near neighbours, permit one
        # changed phoneme in a short word and at most one third of a longer one.
        allowed = max(1, phonemes // 3)
        if distance <= allowed:
            ranked.append((distance / max(phonemes, 1), distance, candidate.casefold(), candidate))
    ranked.sort()
    return [candidate for _ratio, _distance, _folded, candidate in ranked[:max(0, limit)]]


def _norm(word: str) -> str:
    return " ".join((word or "").strip().lower().split())


def queue_path(home_dir: Optional[str] = None) -> str:
    """The queue file, next to the user's other Samsara data."""
    if home_dir is None:
        home_dir = os.environ.get("SAMSARA_HOME_DIR") or os.path.join(
            os.path.expanduser("~"), ".samsara")
    return os.path.join(home_dir, QUEUE_FILENAME)


def classify(wrong: str, right: str) -> tuple:
    """(verdict, reason) where verdict is "confirm" or "never".

    Runs the pair through correction capture v1's own extractor first, so the
    click path and the type-a-correction path agree about what is learnable,
    then applies the rules that only matter for a rule stored forever."""
    w, r = _norm(wrong), _norm(right)
    if not w or not r:
        return "never", "nothing to learn from an empty word"
    if w == r:
        return "never", NEVER_SAME
    for group in HOMOPHONE_SETS:
        if w in group and r in group:
            return "never", NEVER_HOMOPHONE
    if w in COMMON_WORDS:
        return "never", NEVER_COMMON
    # max_edit_ratio=1.0 on purpose: v1's rewrite gate asks "did more than half
    # the TEXT change", which is always true for a one-word pair and would
    # refuse every click correction. The user pointed at one word, so the
    # question is only whether that pair is an atomic substitution -- which is
    # what the rest of extract_corrections decides.
    result = extract_corrections(wrong, right, max_edit_ratio=1.0)
    if not result.learnable:
        reason = result.rejected[0][2] if result.rejected else "not an atomic substitution"
        return "never", reason
    return "confirm", ""


class CorrectionQueue:
    """The pending/accepted store. Every mutation rewrites the file atomically."""

    def __init__(self, path: Optional[str] = None, home_dir: Optional[str] = None):
        self.path = path or queue_path(home_dir)
        self._data = self._load()

    # -- persistence ---------------------------------------------------

    def _empty(self) -> dict:
        return {"version": QUEUE_VERSION, "pending": [], "accepted": [], "rejected": []}

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return self._empty()
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            # Never fatal and never silent: the dictionary lives in a different
            # file, so the worst case here is losing un-reviewed captures.
            logger.warning("[CORRECTIONS] queue unreadable (%s); starting a new one", exc)
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        for key in ("pending", "accepted", "rejected"):
            if not isinstance(data.get(key), list):
                data[key] = []
        data["version"] = QUEUE_VERSION
        return data

    def save(self) -> bool:
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".correction_queue-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            logger.warning("[CORRECTIONS] could not save the queue: %s", exc)
            return False

    # -- reading -------------------------------------------------------

    @property
    def pending(self) -> list:
        return list(self._data["pending"])

    @property
    def accepted(self) -> list:
        return list(self._data["accepted"])

    def find(self, wrong: str, right: str, bucket: str = "pending") -> Optional[dict]:
        w, r = _norm(wrong), _norm(right)
        for entry in self._data.get(bucket, []):
            if entry.get("wrong_norm") == w and entry.get("right_norm") == r:
                return entry
        return None

    def correction_terms(self) -> list[str]:
        """Words already seen in this person's review-gated correction table.

        They complement the live training vocabulary supplied by the preview;
        no general dictionary is used, so a suggestion is always grounded in
        something the person has already chosen to teach or review.
        """
        terms = []
        for bucket in ("pending", "accepted", "rejected"):
            for entry in self._data.get(bucket, []):
                terms.extend((entry.get("wrong", ""), entry.get("right", "")))
        return terms

    # -- writing -------------------------------------------------------

    def capture(self, wrong: str, right: str, *, source: str = "preview_click",
                audio: Optional[str] = None, context: str = "",
                now: Optional[float] = None) -> dict:
        """Record one correction the user made in the draft.

        Returns {"stored": bool, "verdict": str, "reason": str, "entry": dict|None}.
        `stored` is only ever True for the pending queue -- this never writes
        the dictionary, so a wrong capture costs the user one review click."""
        now = time.time() if now is None else now
        verdict, reason = classify(wrong, right)
        if verdict == "never":
            logger.info('[CORRECTIONS] not learnable: "%s" -> "%s" (%s)', wrong, right, reason)
            return {"stored": False, "verdict": verdict, "reason": reason, "entry": None}
        entry = self.find(wrong, right)
        if entry is None:
            entry = {
                "wrong": wrong, "right": right,
                "wrong_norm": _norm(wrong), "right_norm": _norm(right),
                "count": 0, "first_seen": now, "last_seen": now,
                "sources": [], "audio": [], "context": context,
            }
            self._data["pending"].append(entry)
        entry["count"] += 1
        entry["last_seen"] = now
        if source not in entry["sources"]:
            entry["sources"].append(source)
        if audio and audio not in entry["audio"]:
            entry["audio"].append(audio)
        if context and not entry.get("context"):
            entry["context"] = context
        overflow = len(self._data["pending"]) - MAX_PENDING
        if overflow > 0:
            del self._data["pending"][:overflow]
        self.save()
        logger.info('[CORRECTIONS] queued for review: "%s" -> "%s" (seen %d time(s), audio %d)',
                    wrong, right, entry["count"], len(entry["audio"]))
        return {"stored": True, "verdict": verdict, "reason": "", "entry": entry}

    def accept(self, wrong: str, right: str, now: Optional[float] = None) -> Optional[dict]:
        """Move a pending pair to accepted. The CALLER writes the dictionary;
        this only records that a human said yes, so the voice undo can find it."""
        entry = self.find(wrong, right)
        if entry is None:
            return None
        self._data["pending"].remove(entry)
        entry["accepted_at"] = time.time() if now is None else now
        self._data["accepted"].append(entry)
        overflow = len(self._data["accepted"]) - MAX_ACCEPTED
        if overflow > 0:
            del self._data["accepted"][:overflow]
        self.save()
        return entry

    def reject(self, wrong: str, right: str) -> Optional[dict]:
        entry = self.find(wrong, right)
        if entry is None:
            return None
        self._data["pending"].remove(entry)
        self._data["rejected"].append(entry)
        self.save()
        return entry

    def undo_last_accepted(self) -> Optional[dict]:
        """The most recently accepted pair, removed from the accepted list and
        handed back so the caller can delete it from the dictionary. The pair
        is NOT re-queued: the user just said they did not want it."""
        if not self._data["accepted"]:
            return None
        entry = self._data["accepted"].pop()
        self._data["rejected"].append(entry)
        self.save()
        logger.info('[CORRECTIONS] undo: "%s" -> "%s" removed from the accepted list',
                    entry.get("wrong"), entry.get("right"))
        return entry

    def drop_pending(self, wrong: str, right: str) -> Optional[dict]:
        """Forget a captured pair entirely (the voice undo right after a
        correction, before any review)."""
        entry = self.find(wrong, right)
        if entry is None:
            return None
        self._data["pending"].remove(entry)
        self.save()
        return entry


AUDIO_DIRNAME = "correction_audio"
AUDIO_SAMPLE_RATE = 16000
#: Keep at most this many clips; the oldest go first. Each is one utterance of
#: 16 kHz mono audio (a few seconds, ~100 KB), so the folder stays small.
MAX_AUDIO_CLIPS = 200


def audio_dir(home_dir: Optional[str] = None) -> str:
    if home_dir is None:
        home_dir = os.environ.get("SAMSARA_HOME_DIR") or os.path.join(
            os.path.expanduser("~"), ".samsara")
    return os.path.join(home_dir, AUDIO_DIRNAME)


def save_audio(samples, wrong: str, *, sample_rate: int = AUDIO_SAMPLE_RATE,
               home_dir: Optional[str] = None) -> Optional[str]:
    """Write the utterance behind a correction to a .wav and return its path.

    This is the half of the feature the accuracy story depends on (SAMSARA_MAP
    2.6): a dictionary of "heard X, meant Y" pairs WITH the audio can later
    bias an initial_prompt or seed a fine-tune set, which text pairs alone
    cannot. Best-effort: a failure here never costs the user their correction.
    """
    if samples is None:
        return None
    try:
        import wave  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        array = np.asarray(samples, dtype=np.float32).flatten()
        if array.size == 0:
            return None
        peak = float(np.max(np.abs(array))) or 1.0
        if peak > 1.0:
            array = array / peak
        pcm = (array * 32767.0).astype(np.int16)
        directory = audio_dir(home_dir)
        os.makedirs(directory, exist_ok=True)
        safe = "".join(ch for ch in _norm(wrong) if ch.isalnum())[:24] or "word"
        name = f"{int(time.time() * 1000)}-{safe}.wav"
        path = os.path.join(directory, name)
        with wave.open(path, "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(int(sample_rate))
            fh.writeframes(pcm.tobytes())
        _prune_audio(directory)
        return path
    except Exception as exc:
        logger.warning("[CORRECTIONS] could not save the correction audio: %s", exc)
        return None


def _prune_audio(directory: str) -> None:
    try:
        clips = sorted(
            (os.path.join(directory, n) for n in os.listdir(directory) if n.endswith(".wav")),
            key=os.path.getmtime)
        for path in clips[:-MAX_AUDIO_CLIPS]:
            os.remove(path)
    except OSError as exc:
        logger.debug("[CORRECTIONS] audio prune skipped: %s", exc)


_QUEUE: Optional[CorrectionQueue] = None


def get_queue(path: Optional[str] = None) -> CorrectionQueue:
    """Process-wide queue. Tests pass an explicit path (or call reset_queue)."""
    global _QUEUE
    if _QUEUE is None or (path is not None and path != _QUEUE.path):
        _QUEUE = CorrectionQueue(path)
    return _QUEUE


def reset_queue() -> None:
    global _QUEUE
    _QUEUE = None


def undo_last_correction(app=None) -> dict:
    """Take back the most recent correction, by voice.

    An ACCEPTED pair is removed from the dictionary as well as the queue -- a
    wrong entry there rewrites that word in every future dictation, so getting
    rid of it must not need a mouse. With nothing accepted, the newest
    un-reviewed capture is dropped instead, which is what "forget that" means
    right after a click correction.

    Returns {"undone": bool, "wrong": str, "right": str, "where": str}."""
    queue = get_queue()
    entry = queue.undo_last_accepted()
    where = "dictionary"
    if entry is None:
        pending = queue.pending
        if not pending:
            return {"undone": False, "wrong": "", "right": "", "where": "nothing"}
        newest = max(pending, key=lambda e: e.get("last_seen", 0))
        entry = queue.drop_pending(newest.get("wrong", ""), newest.get("right", ""))
        where = "review queue"
        if entry is None:
            return {"undone": False, "wrong": "", "right": "", "where": "nothing"}
    wrong, right = entry.get("wrong", ""), entry.get("right", "")
    if where == "dictionary" and app is not None:
        vt = getattr(app, "voice_training_window", None)
        corrections = getattr(vt, "corrections_dict", None)
        if isinstance(corrections, dict) and wrong in corrections:
            try:
                del corrections[wrong]
                rebuild = getattr(vt, "_rebuild_corrections_pattern", None)
                if callable(rebuild):
                    rebuild()
                save = getattr(vt, "save_training_data", None)
                if callable(save):
                    save()
                logger.info('[CORRECTIONS] removed "%s" -> "%s" from the dictionary', wrong, right)
            except Exception as exc:
                logger.warning("[CORRECTIONS] could not remove the entry: %s", exc)
                return {"undone": False, "wrong": wrong, "right": right, "where": "dictionary"}
    return {"undone": True, "wrong": wrong, "right": right, "where": where}
