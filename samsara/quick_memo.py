"""Serialized, durable storage for short voice memos."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

from samsara.paths import samsara_home_dir
from samsara.log import get_logger

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
logger = get_logger("Samsara")


class MemoWriteError(OSError):
    """A memo could not be persisted and the caller must report it."""


def memo_dir(home=None) -> Path:
    root = Path(home) if home is not None else samsara_home_dir()
    if root.suffix.lower() == ".md":
        root = root.parent
    return root / "memos"


def memo_file(home=None) -> Path:
    supplied = Path(home) if home is not None else None
    if supplied is not None and supplied.suffix.lower() == ".md":
        return supplied
    return memo_dir(home) / "memos.md"


def _file_lock(path: Path):
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def append_memo(text, source, audio_path=None, home=None, category=None) -> Path:
    """Append one UTF-8 memo under its file lock, or raise MemoWriteError.

    Queue 92: this now also adds a row to the JSONL store (see add_memo).
    The signature, the return value and the MemoWriteError contract are
    unchanged -- queue 07's callers and tests do not know the difference.
    """
    add_memo(text, source, audio_path=audio_path, home=home, category=category)
    return memo_file(home)


def _write_markdown(text, audio_path=None, home=None) -> Path:
    """The queue 07 markdown append, unchanged: the durable, human-readable
    copy, written under a file lock and fsynced."""
    path = memo_file(home)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"## {timestamp}\n{text}\n"
    if audio_path is not None:
        relative = os.path.relpath(Path(audio_path), path.parent).replace(os.sep, "/")
        entry += f"[audio: {relative}]\n"
    entry += "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = _file_lock(path)
        with lock:
            is_new = not path.exists()
            with path.open("a", encoding="utf-8") as memo:
                if is_new:
                    memo.write("# Memos\n\n")
                memo.write(entry)
                memo.flush()
                os.fsync(memo.fileno())
    except OSError as exc:
        logger.exception("[MEMO] Could not append memo to %s", path)
        raise MemoWriteError(f"could not append memo to {path}") from exc
    return path


def _replace_with_retry(src, dst, attempts=6, initial_delay_s=0.02):
    """Replace a fresh file, tolerating short Windows scanner locks."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(attempts):
        try:
            return os.replace(src, dst)
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(initial_delay_s * (attempt + 1))


def retain_audio(audio, sample_rate, home=None) -> Path:
    """Save captured mono float audio for a memo and return its WAV path."""
    directory = memo_dir(home) / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now().strftime('%Y%m%dT%H%M%S_%f')}.wav"
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        values = [max(-1.0, min(1.0, float(value))) for value in audio]
        pcm = b"".join(int(value * 32767).to_bytes(2, "little", signed=True) for value in values)
        with wave.open(str(temporary), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(sample_rate))
            wav.writeframes(pcm)
        _replace_with_retry(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


# ---------------------------------------------------------------------------
# The memo store (queue 92)
# ---------------------------------------------------------------------------
# Queue 07 gave memos a human-readable file: `memos.md`, appended under a
# lock and fsynced. That file is still written, byte for byte, and it is
# still what "open the raw file" opens. What it cannot do is back a UI: a
# markdown file has no stable identity per entry, so nothing can address,
# re-categorise or delete one memo, and a file the user may also be editing
# by hand cannot be safely rewritten underneath them.
#
# So the STORE OF RECORD is `memos.jsonl` beside it: one JSON object per
# line, each with an id. Append-only for creation (the same discipline queue
# 07 proved), whole-file rewrite under the same lock via _replace_with_retry
# for edits and deletes. JSONL rather than SQLite because the volume is tens
# to hundreds of entries, it is recoverable by hand in any text editor, and
# it needs no schema migration the first time a field is added.
#
# The two files cannot silently diverge: load_memos() rebuilds the index
# from memos.md when the index is missing, so memos written by queue 07 --
# or by any build before this one -- appear in the UI with no migration step.

#: One JSON object per line, beside memos.md.
INDEX_SUFFIX = ".jsonl"
#: Where the memo's own fields live. `category_source` is the hook a future
#: capture router writes through; see set_category.
SOURCE_VOICE = "voice"
SOURCE_TYPED = "typed"
CATEGORY_SPOKEN = "spoken"
CATEGORY_MANUAL = "manual"
CATEGORY_ROUTER = "router"
#: Audio retention (queue 92). Bounded by BOTH age and total size; whichever
#: bites first wins. Pruning removes the WAV and never the transcript.
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_AUDIO_MB = 500
#: Separators accepted after a spoken category prefix ("shopping: milk").
#: A separator is REQUIRED -- without one, "Shopping list needs milk" would
#: lose its first word to a category.
_CATEGORY_SEPARATORS = ",:"


def index_file(home=None) -> Path:
    """The JSONL store beside the markdown file it mirrors."""
    return memo_file(home).with_suffix(INDEX_SUFFIX)


def categories_file(home=None) -> Path:
    return memo_file(home).with_suffix(".categories.json")


def new_memo_id(now=None) -> str:
    """Sortable and unique: the timestamp a human can read, plus enough
    randomness that two memos in the same millisecond cannot collide."""
    moment = now or datetime.now()
    return f"{moment.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:6]}"


def _relative_audio(audio_path, home=None) -> Optional[str]:
    """Audio is stored relative to the memo file, so moving the whole memo
    directory (or syncing it) does not break every entry."""
    if audio_path is None:
        return None
    base = memo_file(home).parent
    try:
        return os.path.relpath(Path(audio_path), base).replace(os.sep, "/")
    except (ValueError, OSError):
        return str(audio_path)


def audio_path_for(record, home=None) -> Optional[Path]:
    """The absolute WAV path for a record, or None when it has no audio or
    the audio has been pruned."""
    relative = (record or {}).get("audio")
    if not relative or (record or {}).get("audio_pruned"):
        return None
    return (memo_file(home).parent / relative).resolve()


def _read_index(home=None) -> list:
    path = index_file(home)
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
            # One corrupt line must not cost the user every other memo.
            logger.warning("[MEMO] Skipping unreadable index line in %s", path)
            continue
        if isinstance(record, dict) and record.get("id"):
            out.append(record)
    return out


def _write_index(records, home=None) -> None:
    """Replace the whole index atomically, under the same lock appends use."""
    path = index_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with _file_lock(path):
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _append_index(record, home=None) -> None:
    """Add one row. A failure here is logged, never raised: the memo is
    already safe in memos.md and load_memos() can rebuild from it."""
    path = index_file(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        logger.warning("[MEMO] Could not index memo %s: %s", record.get("id"), exc)


_MD_ENTRY = re.compile(
    r"^## (?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\n(?P<body>.*?)(?=\n## |\Z)",
    re.S | re.M)
_MD_AUDIO = re.compile(r"^\[audio: (?P<path>.+?)\]$", re.M)


def parse_markdown(text: str) -> list:
    """Memos recovered from a queue 07 `memos.md`.

    Used only to rebuild a missing index, so a user upgrading from a build
    that had no index keeps every memo they ever recorded.
    """
    out = []
    for match in _MD_ENTRY.finditer(text or ""):
        body = match.group("body")
        audio = None
        found = _MD_AUDIO.search(body)
        if found:
            audio = found.group("path")
            body = _MD_AUDIO.sub("", body)
        body = body.strip()
        if not body:
            continue
        try:
            when = datetime.strptime(match.group("when"), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        out.append(_record(body, SOURCE_VOICE, audio=audio, now=when))
    return out


def _record(text, source, audio=None, category=None, category_source=None, now=None) -> dict:
    moment = now or datetime.now()
    return {
        "id": new_memo_id(moment),
        "at": moment.replace(microsecond=0).isoformat(),
        "text": text,
        "source": source,
        "audio": audio,
        "audio_pruned": False,
        "category": category,
        "category_source": category_source,
        # A memo the user pinned is never pruned. This is what makes an
        # automatic retention policy safe to switch on by default.
        "keep": False,
    }


def load_memos(home=None) -> list:
    """Every memo, newest last. Rebuilds the index from memos.md when the
    index does not exist yet."""
    records = _read_index(home)
    # `exists`, not `if records`: an EMPTY index means every memo was
    # deleted, and the markdown fallback below must not resurrect them.
    # Only a MISSING index means "this build has not written one yet".
    if records or index_file(home).exists():
        return records
    markdown = memo_file(home)
    if not markdown.exists():
        return []
    try:
        recovered = parse_markdown(markdown.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("[MEMO] Could not read %s: %s", markdown, exc)
        return []
    if recovered:
        logger.info("[MEMO] Rebuilt index from %s (%d memos)", markdown, len(recovered))
        try:
            _write_index(recovered, home)
        except OSError as exc:
            logger.warning("[MEMO] Could not write rebuilt index: %s", exc)
    return recovered


def add_memo(text, source=SOURCE_VOICE, audio_path=None, home=None,
             category=None, category_source=None) -> dict:
    """Store one memo and return its record.

    The markdown file is written FIRST and its failure is the one that
    raises: it is the durable copy, and a memo that reached neither file is
    the only real loss. An index failure is logged and recoverable.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("memo text must not be empty")
    _write_markdown(text.strip(), audio_path=audio_path, home=home)
    record = _record(text.strip(), source, audio=_relative_audio(audio_path, home),
                     category=category, category_source=category_source)
    _append_index(record, home)
    return record


def set_category(memo_id, category, home=None, source=CATEGORY_MANUAL) -> bool:
    """Change one memo's category. Returns False when the id is unknown.

    THE CONTRACT A FUTURE CAPTURE ROUTER MUST KEEP: a category the user set
    by hand (source "manual") is final. An automatic classifier may fill an
    empty category or replace one it assigned itself, and must never
    overwrite a manual one -- being corrected and then re-corrected by a
    model is worse than having no classifier at all.
    """
    records = load_memos(home)
    changed = False
    for record in records:
        if record.get("id") != memo_id:
            continue
        if source == CATEGORY_ROUTER and record.get("category_source") == CATEGORY_MANUAL:
            return False
        record["category"] = category or None
        record["category_source"] = source if category else None
        changed = True
        break
    if changed:
        _write_index(records, home)
    return changed


def set_keep(memo_id, keep, home=None) -> bool:
    """Pin or unpin a memo's audio against the retention policy."""
    records = load_memos(home)
    for record in records:
        if record.get("id") == memo_id:
            record["keep"] = bool(keep)
            _write_index(records, home)
            return True
    return False


def _rewrite_markdown(records, home=None) -> None:
    """Rebuild memos.md from the records, in queue 07's exact format."""
    path = memo_file(home)
    body = ["# Memos\n\n"]
    for record in records:
        stamp = str(record.get("at", "")).replace("T", " ")[:16]
        body.append(f"## {stamp}\n{record.get('text', '')}\n")
        if record.get("audio"):
            body.append(f"[audio: {record['audio']}]\n")
        body.append("\n")
    text = "".join(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with _file_lock(path):
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def delete_memo(memo_id, home=None) -> bool:
    """Remove one memo, its audio, and its line in the markdown mirror.

    The mirror is rewritten too, not just the index: the page offers "open
    the raw file" a click away, and a memo the user deleted -- possibly
    because they did not want it kept -- must not still be sitting there.
    The cost is that hand-made edits to memos.md are normalised the next
    time a memo is deleted; the index is the store of record, and this keeps
    the two from disagreeing about what exists.
    """
    records = load_memos(home)
    kept = [r for r in records if r.get("id") != memo_id]
    if len(kept) == len(records):
        return False
    gone = [r for r in records if r.get("id") == memo_id]
    _write_index(kept, home)
    try:
        _rewrite_markdown(kept, home)
    except OSError as exc:
        logger.warning("[MEMO] Could not rewrite %s: %s", memo_file(home), exc)
    for record in gone:
        path = audio_path_for(record, home)
        if path is not None:
            try:
                path.unlink()
            except OSError as exc:
                logger.debug("[MEMO] Could not delete audio %s: %s", path, exc)
    return True


def search_memos(records, needle: str) -> list:
    """Case-insensitive match over the transcript and the category."""
    needle = (needle or "").strip().lower()
    if not needle:
        return list(records or [])
    return [r for r in (records or [])
            if needle in str(r.get("text", "")).lower()
            or needle in str(r.get("category") or "").lower()]


# ---- Categories -----------------------------------------------------------
# Explicit and cheap (queue 92): no model, no guessing. The list starts EMPTY
# and nothing is ever invented -- a memo cannot create a category by being
# mis-transcribed. Once the user has defined one, saying it as a prefix
# ("shopping: milk and eggs") files the memo under it; otherwise the whole
# utterance is the memo and the category is None.


def categories(home=None) -> list:
    path = categories_file(home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def save_categories(names, home=None) -> list:
    """Persist the category list, de-duplicated case-insensitively."""
    seen, out = set(), []
    for name in names or []:
        clean = str(name).strip()
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(clean)
    path = categories_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with _file_lock(path):
        try:
            temporary.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            _replace_with_retry(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return out


def add_category(name, home=None) -> list:
    return save_categories(categories(home) + [name], home)


def remove_category(name, home=None) -> list:
    lowered = str(name).strip().lower()
    return save_categories([c for c in categories(home) if c.lower() != lowered], home)


def split_category(text, known) -> tuple:
    """(category, remaining text) when a memo opens with a KNOWN category
    followed by a comma or a colon, else (None, text).

    Only a name already in `known` counts, and the separator is required:
    "Remember, I must call the dentist" keeps all of its words, and
    "Shopping list needs milk" is not filed under Shopping.
    """
    body = (text or "").strip()
    if not body:
        return None, body
    # Longest first, so "shopping list" wins over "shopping".
    for name in sorted(known or [], key=len, reverse=True):
        candidate = str(name).strip()
        if not candidate or len(body) <= len(candidate):
            continue
        if body[:len(candidate)].lower() != candidate.lower():
            continue
        rest = body[len(candidate):]
        stripped = rest.lstrip()
        if not stripped or stripped[0] not in _CATEGORY_SEPARATORS:
            continue
        remainder = stripped[1:].strip()
        if remainder:
            return candidate, remainder
    return None, body


# ---- Audio retention ------------------------------------------------------


def _audio_settings(config) -> tuple:
    config = config or {}
    days = config.get("memo_audio_retention_days", DEFAULT_RETENTION_DAYS)
    max_mb = config.get("memo_audio_max_mb", DEFAULT_MAX_AUDIO_MB)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    try:
        max_mb = int(max_mb)
    except (TypeError, ValueError):
        max_mb = DEFAULT_MAX_AUDIO_MB
    return days, max_mb


def prune_audio(config=None, home=None, now=None) -> list:
    """Apply the retention policy and return the ids whose audio was removed.

    The policy (queue 92), in order:
      1. A memo with `keep` set is never touched, at any age or size.
      2. Audio older than `memo_audio_retention_days` goes (0 = never).
      3. If what is left still exceeds `memo_audio_max_mb`, the OLDEST audio
         goes until it fits (0 = no size limit).
    The TRANSCRIPT is never removed -- only the WAV -- and the record keeps
    `audio_pruned` so the UI says "audio expired" instead of showing a play
    button that does nothing.
    """
    days, max_mb = _audio_settings(config)
    records = load_memos(home)
    moment = now or datetime.now()
    live = []
    for record in records:
        path = audio_path_for(record, home)
        if path is None or record.get("keep"):
            continue
        try:
            stat = path.stat()
        except OSError:
            # The WAV is gone already; mark the record so the UI is honest.
            record["audio_pruned"] = True
            continue
        live.append((record, path, stat.st_size))

    removed = []

    def drop(record, path):
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("[MEMO] Could not prune audio %s: %s", path, exc)
            return False
        record["audio_pruned"] = True
        removed.append(record.get("id"))
        return True

    remaining = []
    for record, path, size in live:
        age_days = None
        try:
            age_days = (moment - datetime.fromisoformat(record["at"])).days
        except (KeyError, TypeError, ValueError):
            age_days = None
        if days > 0 and age_days is not None and age_days > days:
            drop(record, path)
            continue
        remaining.append((record, path, size))

    if max_mb > 0:
        budget = max_mb * 1024 * 1024
        total = sum(size for _r, _p, size in remaining)
        # Oldest first: the memo you recorded this morning outlives the one
        # from six weeks ago.
        remaining.sort(key=lambda item: item[0].get("at", ""))
        for record, path, size in remaining:
            if total <= budget:
                break
            if drop(record, path):
                total -= size

    if removed or any(r.get("audio_pruned") for r in records):
        try:
            _write_index(records, home)
        except OSError as exc:
            logger.warning("[MEMO] Could not record pruning: %s", exc)
    if removed:
        logger.info("[MEMO] Pruned audio for %d memo(s)", len(removed))
    return removed


def audio_bytes(home=None) -> int:
    """Total size of retained memo audio, for the UI to show."""
    directory = memo_dir(home) / "audio"
    total = 0
    try:
        for entry in directory.iterdir():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


# ---- Optional Obsidian mirror --------------------------------------------
# The July 2026 design (samsara/voice_memo.py) wrote memos straight into an
# Obsidian vault as an ![[embed]] plus transcript, which Obsidian renders as
# an inline audio player and syncs to a phone for free. That is genuinely
# better for READING a memo later -- and a poor store of record, because the
# vault is the user's own document space: it may not exist, it may be
# reorganised, Obsidian may hold the note open, and an entry there has no id
# to address. So it is kept, and demoted from store to MIRROR: off by
# default, best-effort, and never the thing a memo depends on.


def vault_settings(config) -> dict:
    return dict((config or {}).get("voice_memo", {}) or {})


def mirror_enabled(config) -> bool:
    settings = vault_settings(config)
    return bool(settings.get("mirror_memos")) and bool(settings.get("vault_dir"))


def mirror_to_vault(record, config, home=None) -> Optional[Path]:
    """Copy one memo into the Obsidian vault, audio embed and all.

    Never raises: the memo is already stored, and a vault that has moved
    must not turn a saved memo into an error.
    """
    if not mirror_enabled(config):
        return None
    settings = vault_settings(config)
    try:
        vault = Path(settings.get("vault_dir", ""))
        note = vault / settings.get("note_relpath", "Voice Memos.md")
        embed = None
        source = audio_path_for(record, home)
        if source is not None and source.exists():
            attachments_rel = settings.get("attachments_relpath", "Attachments/Memos")
            attachments = vault / attachments_rel
            attachments.mkdir(parents=True, exist_ok=True)
            target = attachments / f"memo_{record['id']}.wav"
            target.write_bytes(source.read_bytes())
            embed = (Path(attachments_rel) / target.name).as_posix()
        note.parent.mkdir(parents=True, exist_ok=True)
        stamp = str(record.get("at", "")).replace("T", " ")[:16]
        heading = f"## {stamp}"
        if record.get("category"):
            heading += f" -- {record['category']}"
        entry = heading + "\n"
        if embed:
            entry += f"![[{embed}]]\n\n"
        entry += f"{record.get('text', '')}\n"
        with _file_lock(note):
            is_new = not note.exists()
            with note.open("a", encoding="utf-8") as handle:
                if is_new:
                    handle.write("# Voice Memos\n\n")
                else:
                    handle.write("\n")
                handle.write(entry)
        logger.info("[MEMO] Mirrored to vault: %s", note)
        return note
    except Exception as exc:
        logger.warning("[MEMO] Vault mirror failed: %s", exc)
        return None
