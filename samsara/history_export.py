"""Get your dictation history back out of the app (queue 122).

Everything the user has ever said is in ~/.samsara/history.db, and until now
there was no way to read it anywhere else -- history_store.py's own docstring
said so: "there is currently no history export/import feature at all". A
local-first app that will not hand back your own data is local-first in name
only.

Two formats, for two different needs:

  JSON  -- every column of every row, plus a schema version, so the file is
           lossless and an importer can be written later without guessing.
  TEXT  -- chronological, oldest first, for somebody who just wants their
           words. Markdown is the same document with headings.

WHAT THIS MODULE DOES NOT DO. It does not choose a path, it does not know
about Qt, and it writes nothing until a caller hands it one. There is no
schedule, no automatic export and no telemetry: an export happens because a
person asked for one, once, and named the file.

ENCODING IS EXPLICIT EVERYWHERE. Every open() here passes encoding='utf-8'
and newline='\\n'. history_store.py carries an audit note about a cp1252
UnicodeEncodeError; SQLite TEXT is UTF-8 natively, so this module is the
first place in the history path where an encoding could be got wrong, and it
is the reason that audit note existed in the first place.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from samsara.log import get_logger
from samsara.paths import samsara_home_dir

logger = get_logger(__name__)

#: Bump ONLY when the meaning of a field changes or a field is removed.
#: Adding a field is backwards compatible: an importer reads what it knows
#: and ignores the rest, which is why every row is written as a full object
#: rather than a positional array.
SCHEMA_VERSION = 1

#: The document's own type tag, so a file found on its own is identifiable
#: without its name.
SCHEMA_ID = "samsara.history"

#: Every column HistoryManager creates or migrates in, in schema order.
#: Written verbatim, including nulls: a lossy export cannot be re-imported,
#: and deciding on the user's behalf which of their own columns matter is
#: exactly the posture this feature exists to correct.
COLUMNS = (
    "id",                  # the row's stable identity -- an importer's key
    "timestamp",           # ISO8601 local time, as HistoryManager.add writes it
    "app_context",         # which app had focus
    "raw_text",            # what Whisper returned, before formatting
    "display_text",        # what was actually typed
    "duration_ms",
    "mode",                # hold | toggle | hands_free
    "status",              # success | failed | transcribing
    "audio_path",          # a path on THIS machine; see the note in build_document
    "correction_source",
    "session_id",          # groups one run of the app
    "entry_type",          # dictation | command | wake_command | failed
    "log_prob",            # Whisper's confidence
    "matched_command",
)

#: Where the save dialog opens. Inside the app's own per-user directory:
#: never the repository, and never Documents, which on this owner's machine
#: and most Windows installs is redirected into OneDrive. An export is every
#: word the user has ever dictated, and putting it in a folder that syncs
#: itself to a cloud provider by default would undo the point of the feature.
EXPORT_DIRNAME = "exports"


def default_export_dir() -> Path:
    """~/.samsara/exports (or $SAMSARA_HOME_DIR/exports). Not created here --
    nothing is written to disk until the user picks a path."""
    return samsara_home_dir() / EXPORT_DIRNAME


def default_filename(fmt: str = "json", now: "_dt.datetime | None" = None) -> str:
    stamp = (now or _dt.datetime.now()).strftime("%Y-%m-%d_%H%M")
    suffix = {"json": "json", "text": "txt", "markdown": "md"}.get(fmt, "txt")
    return f"samsara-history_{stamp}.{suffix}"


# ---------------------------------------------------------------------------
# Selecting rows
# ---------------------------------------------------------------------------

def _as_dict(row) -> dict:
    """sqlite3.Row or dict -> a plain dict with every known column present."""
    out = {}
    for name in COLUMNS:
        try:
            out[name] = row[name]
        except (KeyError, IndexError, TypeError):
            out[name] = None
    return out


def collect_all(store, *, page: int = 500, max_rows: int = 500000) -> list:
    """Every entry in the store, oldest last (the store's own id DESC order).

    Paginated with before_id rather than one enormous LIMIT, because that is
    the cursor HistoryManager.recent_windowed documents and it stays correct
    if two rows share a timestamp. `max_rows` is a stop, not a budget: a
    runaway loop on a corrupt cursor should end, not fill the disk.
    """
    rows, before_id = [], None
    while len(rows) < max_rows:
        batch = store.query(limit=page, before_id=before_id)
        if not batch:
            break
        batch = [_as_dict(r) for r in batch]
        rows.extend(batch)
        ids = [r["id"] for r in batch if r["id"] is not None]
        if len(batch) < page or not ids:
            break
        nxt = min(ids)
        if before_id is not None and nxt >= before_id:
            logger.debug("[EXPORT] cursor stopped moving at id=%s", nxt)
            break
        before_id = nxt
    return rows


def in_date_range(rows, start: "str | None", end: "str | None") -> list:
    """Rows whose timestamp falls in [start, end], BOTH BOUNDARIES INCLUDED.

    `start` and `end` are "YYYY-MM-DD" dates, not instants: a user asking for
    the 3rd to the 5th means all of the 5th, so the upper bound compares
    against the end of that day. Timestamps are ISO8601, which sorts
    lexicographically, so this needs no parsing -- and a row with no
    timestamp is kept only when no range was asked for, because a row that
    cannot be placed in time cannot be proved to be in the range.
    """
    if not start and not end:
        return list(rows)
    lo = f"{start}T00:00:00" if start else ""
    hi = f"{end}T23:59:59.999999" if end else "￿"
    out = []
    for row in rows:
        ts = str(row.get("timestamp") or "")
        if not ts:
            continue
        if lo <= ts <= hi:
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def build_document(rows, *, scope: str = "all", generated_at=None) -> dict:
    """The JSON document, as a plain dict.

    Shape, and why each part is here:

        schema        "samsara.history" -- identifies the file on its own
        schema_version 1 -- an importer refuses a version it does not know
        generated_at  ISO8601, when the export ran
        scope         what the user asked for, in words, so a partial export
                      is never mistaken for a complete one
        count         rows in this file, so truncation is detectable
        columns       the column list in order, so an importer can tell a
                      missing field from a null one
        entries       one object per row, every column, nulls included

    `audio_path` is exported as recorded and is a path on the machine that
    wrote the file. It is kept because dropping it would make the export
    lossy, and an importer must treat it as a hint, not a promise.
    """
    when = generated_at or _dt.datetime.now()
    if hasattr(when, "isoformat"):
        when = when.isoformat()
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "generated_at": when,
        "scope": scope,
        "count": len(rows),
        "columns": list(COLUMNS),
        "entries": [_as_dict(r) for r in rows],
    }


def write_json(rows, path, *, scope: str = "all", generated_at=None) -> Path:
    """Write the JSON export. UTF-8, ensure_ascii=False, so dictated
    accents and emoji are stored as themselves rather than escapes."""
    path = Path(path)
    doc = build_document(rows, scope=scope, generated_at=generated_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


# ---------------------------------------------------------------------------
# Readable text
# ---------------------------------------------------------------------------

def _local_date(ts: str) -> str:
    return (ts or "")[:10] or "undated"


def _local_time(ts: str) -> str:
    return (ts or "")[11:19] or "--:--:--"


def _body(row: dict) -> str:
    """What the user would call "the words": the typed text, falling back to
    the raw transcription when nothing was typed."""
    return str(row.get("display_text") or row.get("raw_text") or "").strip()


def render_text(rows, *, scope: str = "all", generated_at=None,
                markdown: bool = False) -> str:
    """The readable export: oldest first, grouped by day, timestamps kept.

    Chronological on purpose. The list VIEW is newest-first because you are
    looking for the thing you just said; a document you are going to read or
    archive runs forwards, the way the days did.
    """
    when = generated_at or _dt.datetime.now()
    when = when.isoformat(timespec="seconds") if hasattr(when, "isoformat") else str(when)
    ordered = sorted(rows, key=lambda r: (str(r.get("timestamp") or ""), r.get("id") or 0))

    h1 = "# " if markdown else ""
    h2 = "## " if markdown else ""
    out = [f"{h1}Samsara dictation history", ""]
    out.append(f"Exported {when}")
    out.append(f"Scope: {scope}")
    out.append(f"{len(ordered)} entr{'y' if len(ordered) == 1 else 'ies'}")
    out.append("")
    if not ordered:
        out.append("_No entries._" if markdown else "No entries.")
        out.append("")
        return "\n".join(out)

    day = None
    for row in ordered:
        ts = str(row.get("timestamp") or "")
        this_day = _local_date(ts)
        if this_day != day:
            day = this_day
            out += ["", f"{h2}{day}", ""]
        text = _body(row)
        kind = str(row.get("entry_type") or "dictation")
        tag = "" if kind == "dictation" else f" [{kind}]"
        if not text:
            text = "(no text)"
        if markdown:
            # A blockquote keeps dictated text that begins with # or - from
            # being read as markup.
            out.append(f"**{_local_time(ts)}**{tag}")
            out += ["", "> " + text.replace("\n", "\n> "), ""]
        else:
            out.append(f"{_local_time(ts)}{tag}  {text}")
    out.append("")
    return "\n".join(out)


def write_text(rows, path, *, scope: str = "all", generated_at=None,
               markdown: bool = False) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_text(rows, scope=scope, generated_at=generated_at,
                             markdown=markdown))
    return path


# ---------------------------------------------------------------------------
# One entry point for a caller that has a path and a format
# ---------------------------------------------------------------------------

FORMATS = ("json", "text", "markdown")


def format_for_path(path) -> str:
    """The format a chosen filename implies. A user who types .md gets
    markdown whichever entry the dialog's filter was on."""
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in (".md", ".markdown"):
        return "markdown"
    return "text"


def export(rows, path, fmt: "str | None" = None, *, scope: str = "all",
           generated_at=None) -> Path:
    """Write `rows` to `path`. Returns the path written.

    Never guesses at content: an empty history writes an empty document with
    a valid schema, which is a true answer and re-imports cleanly. It is not
    an error, and it is not a reason to skip writing the file the user asked
    for.
    """
    fmt = (fmt or format_for_path(path)).lower()
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; expected one of {FORMATS}")
    if fmt == "json":
        return write_json(rows, path, scope=scope, generated_at=generated_at)
    return write_text(rows, path, scope=scope, generated_at=generated_at,
                      markdown=(fmt == "markdown"))
