"""Named pieces of text the user can say into place: "insert <name>".

Dictate a paragraph once, name it, reuse it forever. That is the whole
feature, and it is the interaction that matters most for someone who cannot
type: the expensive part is saying the text, and this makes it a one-off.

Storage follows samsara/quick_memo.py rather than inventing a second
convention: JSONL under ~/.samsara/snippets/, one record per line, appended
for creation and rewritten whole (atomically, under the same per-file lock)
for edits and deletes.

    {"id", "name", "text", "created", "last_used", "use_count"}

Why this shape does not need a migration for placeholders ({date}, {name}),
which are deliberately out of scope now:

  * A placeholder lives INSIDE `text`. A reader that does not expand it types
    it literally, which is exactly today's behaviour -- so adding expansion
    later changes what a reader DOES with the field, never the field itself.
  * Deciding whether to expand needs at most one new key (a "format" that
    defaults to "plain" when absent). Adding a defaulted key to JSONL rows
    needs no migration pass.
  * The rewrite path preserves keys it does not know about (see
    `_write_all`): every record is written back as it was loaded, plus the
    fields being changed. So a row written by a newer Samsara survives an
    older one reading and rewriting the file, which is the failure a
    migration would otherwise have to repair.

Names are matched on a normalised form (samsara.command_catalog's own
normalisation, so a snippet name and a command phrase are compared the way
the matcher compares them). Exact first; a near miss is reported as a
suggestion and NEVER typed -- inserting the wrong paragraph into someone's
document silently is worse than doing nothing.

A snippet's text is data. It is typed, never dispatched, whatever it says.
"""
from __future__ import annotations

import difflib
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from samsara.log import get_logger
from samsara.paths import samsara_home_dir

logger = get_logger("Samsara")

_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()

#: A near miss has to be close, not merely closest: difflib's ratio over the
#: normalised names. Below this the answer is "no snippet called that", which
#: is a better answer than a confident wrong one.
SUGGEST_CUTOFF = 0.72
#: Names longer than this are a paragraph, not a name, and would be unsayable.
MAX_NAME_CHARS = 60


class SnippetError(ValueError):
    """A snippet could not be saved, and the caller must tell the user why."""


# ---------------------------------------------------------------------------
# Paths and locking (the quick_memo pattern)
# ---------------------------------------------------------------------------

def snippet_dir(home=None) -> Path:
    root = Path(home) if home is not None else samsara_home_dir()
    return root / "snippets"


def snippet_file(home=None) -> Path:
    return snippet_dir(home) / "snippets.jsonl"


def _file_lock(path: Path):
    key = str(path).lower()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def new_snippet_id(now=None) -> str:
    """Sortable and unique, like quick_memo.new_memo_id: a timestamp a human
    can read plus enough randomness that two in the same millisecond cannot
    collide."""
    moment = now or datetime.now()
    return f"{moment.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:6]}"


def normalize_name(name: str) -> str:
    """The form names are compared in -- the same normalisation the command
    matcher uses, so "Sign Off." and "sign off" are one name, and a snippet
    name can be compared with a command phrase on equal terms."""
    from samsara.command_catalog import normalize_phrase  # noqa: PLC0415
    return normalize_phrase(name or "")


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------

def _read_all(home=None) -> list:
    path = snippet_file(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # One corrupt line must not cost the user every other snippet.
            logger.warning("[SNIPPET] Skipping unreadable line in %s", path)
            continue
        if isinstance(record, dict) and record.get("id") and record.get("name") is not None:
            out.append(record)
    return out


def _write_all(records, home=None) -> None:
    """Replace the whole file atomically, under the lock appends use.

    `records` are written back as given -- including keys this version does
    not know about. That is what makes a new field a non-migration."""
    path = snippet_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with _file_lock(path):
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _append(record, home=None) -> None:
    path = snippet_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def load_snippets(home=None) -> list:
    """Every snippet, oldest first (the order they were appended in)."""
    return _read_all(home)


# ---------------------------------------------------------------------------
# Name collisions
# ---------------------------------------------------------------------------

def reserved_names(app=None) -> dict:
    """{normalised phrase -> what it already is} for everything a snippet
    name must not shadow.

    Three sources, because one is not enough and the brief's own example
    proves it. `reserved_whole_utterances()` covers the SESSION control words
    (the lane switches, scratch-that, the commit word, sleep, stop, Ava) --
    but "insert tab" is not one of those. It is a FORMATTING TOKEN, turned
    into a tab character before anything could dispatch it, and a registered
    command phrase is a third thing again. All three are checked."""
    from samsara.command_catalog import normalize_phrase  # noqa: PLC0415

    out: dict = {}

    # 1. Session control words.
    try:
        from samsara.command_catalog import reserved_whole_utterances  # noqa: PLC0415
        for phrase in reserved_whole_utterances():
            if phrase:
                out.setdefault(phrase, "a session control phrase")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[SNIPPET] reserved words unavailable: %s", exc)

    # 2. Formatting tokens -- "insert tab" becomes a tab character in the
    #    dictation pipeline and never reaches a command at all.
    try:
        from samsara import formatting_tokens  # noqa: PLC0415
        for phrase, _repl in getattr(formatting_tokens, "_SIMPLE_TOKENS", ()):
            normalized = normalize_phrase(phrase)
            if normalized:
                out.setdefault(normalized, "a formatting token")
        for trigger, _b, _a in getattr(formatting_tokens, "TRAILING_WRAPS", ()):
            normalized = normalize_phrase(trigger)
            if normalized:
                out.setdefault(normalized, "a formatting token")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[SNIPPET] formatting tokens unavailable: %s", exc)

    # 3. Registered command phrases and their aliases, from the LIVE registry
    #    when an app is to hand (it always is at save time in the running app).
    matcher = getattr(getattr(app, "command_executor", None), "_matcher", None)
    lister = getattr(matcher, "list_commands", None)
    if callable(lister):
        try:
            for row in lister():
                for phrase in _row_phrases(row):
                    normalized = normalize_phrase(phrase)
                    if normalized:
                        out.setdefault(normalized, "a voice command")
        except Exception as exc:
            logger.debug("[SNIPPET] live registry unavailable: %s", exc)
    return out


def _row_phrases(row) -> list:
    """Every spoken form of one registry row, tolerating both the dict and
    the object shape list_commands has used."""
    phrases = []
    for key in ("phrase", "name"):
        value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if isinstance(value, str) and value:
            phrases.append(value)
    aliases = row.get("aliases") if isinstance(row, dict) else getattr(row, "aliases", None)
    if isinstance(aliases, (list, tuple, set)):
        phrases += [a for a in aliases if isinstance(a, str) and a]
    return phrases


#: The command's own phrase. A snippet called "x" is said as "insert x", so
#: BOTH the bare name and the full utterance have to be checked -- a snippet
#: named "tab" is fine on its own and collides as "insert tab".
INSERT_PHRASE = "insert"
SAVE_PHRASE = "save snippet"


def name_conflict(name: str, app=None, home=None, exclude_id=None) -> Optional[str]:
    """Why `name` cannot be used, or None when it is free.

    Checked at SAVE time on purpose. A collision discovered at use time is a
    silent failure -- the user says "insert tab", gets a tab character, and
    has no way to find out why their paragraph did not appear."""
    normalized = normalize_name(name)
    if not normalized:
        return "A snippet needs a name you can say."
    if len(name.strip()) > MAX_NAME_CHARS:
        return (f"That name is {len(name.strip())} characters. "
                f"Keep it under {MAX_NAME_CHARS} so it is quick to say.")

    reserved = reserved_names(app)
    spoken = f"{INSERT_PHRASE} {normalized}"
    for candidate, label in ((normalized, None), (spoken, None)):
        what = reserved.get(candidate)
        if what:
            said = candidate if candidate != normalized else f'"{name.strip()}"'
            return (f'{said} is already {what}. Saying "{spoken}" would run that '
                    f"instead of typing your snippet. Pick another name.")

    for record in _read_all(home):
        if record.get("id") == exclude_id:
            continue
        if normalize_name(record.get("name")) == normalized:
            return f'There is already a snippet called "{record.get("name")}".'
    return None


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------

def add_snippet(name: str, text: str, home=None, app=None, now=None) -> dict:
    """Store one snippet, or raise SnippetError with a sentence to show."""
    if not (text or "").strip():
        raise SnippetError("A snippet needs some text to type.")
    conflict = name_conflict(name, app=app, home=home)
    if conflict:
        raise SnippetError(conflict)
    record = {
        "id": new_snippet_id(now),
        "name": name.strip(),
        "text": text,
        "created": (now or datetime.now()).isoformat(timespec="seconds"),
        "last_used": None,
        "use_count": 0,
    }
    _append(record, home)
    logger.info("[SNIPPET] saved %r (%d chars)", record["name"], len(text))
    return record


def update_snippet(snippet_id: str, *, name=None, text=None, home=None, app=None) -> Optional[dict]:
    """Change a snippet's name and/or text. Returns the new record, None when
    the id is unknown; raises SnippetError when the new name is not usable."""
    records = _read_all(home)
    target = next((r for r in records if r.get("id") == snippet_id), None)
    if target is None:
        return None
    if name is not None and normalize_name(name) != normalize_name(target.get("name")):
        conflict = name_conflict(name, app=app, home=home, exclude_id=snippet_id)
        if conflict:
            raise SnippetError(conflict)
    if text is not None and not text.strip():
        raise SnippetError("A snippet needs some text to type.")
    if name is not None:
        target["name"] = name.strip()
    if text is not None:
        target["text"] = text
    _write_all(records, home)
    return target


def delete_snippet(snippet_id: str, home=None) -> bool:
    records = _read_all(home)
    kept = [r for r in records if r.get("id") != snippet_id]
    if len(kept) == len(records):
        return False
    _write_all(kept, home)
    return True


def record_use(snippet_id: str, home=None, now=None) -> Optional[dict]:
    """Stamp a successful insert. Best effort: a snippet that typed correctly
    must never look like a failure because the counter could not be written."""
    try:
        records = _read_all(home)
        target = next((r for r in records if r.get("id") == snippet_id), None)
        if target is None:
            return None
        target["use_count"] = int(target.get("use_count") or 0) + 1
        target["last_used"] = (now or datetime.now()).isoformat(timespec="seconds")
        _write_all(records, home)
        return target
    except Exception as exc:
        logger.debug("[SNIPPET] could not record use of %s: %s", snippet_id, exc)
        return None


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def find_snippet(name: str, home=None, records=None) -> Optional[dict]:
    """Exact match on the normalised name, or None. Never approximate."""
    normalized = normalize_name(name)
    if not normalized:
        return None
    for record in (records if records is not None else _read_all(home)):
        if normalize_name(record.get("name")) == normalized:
            return record
    return None


def suggest_names(name: str, home=None, records=None, limit: int = 1) -> list:
    """The closest stored names to `name`, best first, or [] when nothing is
    close enough. Only ever used to ASK; never to insert."""
    normalized = normalize_name(name)
    if not normalized:
        return []
    pool = records if records is not None else _read_all(home)
    by_norm = {}
    for record in pool:
        key = normalize_name(record.get("name"))
        if key:
            by_norm.setdefault(key, record.get("name"))
    close = difflib.get_close_matches(normalized, list(by_norm), n=limit, cutoff=SUGGEST_CUTOFF)
    return [by_norm[key] for key in close]


def search_snippets(records, needle: str) -> list:
    """Name-or-text substring filter, the way memos_qt filters memos."""
    needle = (needle or "").strip().lower()
    if not needle:
        return list(records)
    return [r for r in records
            if needle in str(r.get("name", "")).lower()
            or needle in str(r.get("text", "")).lower()]
