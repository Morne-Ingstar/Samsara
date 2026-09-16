"""ONE outcome record schema, shared by the writer and every reader (queue 109).

Before this module the writer (``DictationApp._show_outcome_chip``) appended
plain tuples ``(label, kind, time)`` while ``home_signals.recent_misses``
counted only dicts, so the command-miss diagnostic could never fire: feeding
the real writer's three MISS outcomes to the real reader returned ``(0, 3)``
(Astra F11, ``astra_review/SAMSARA_REVIEW_2026-09-15.md``). Neither side was
wrong on its own; there was no schema for either of them to be wrong about.

``OutcomeRecord`` is a NamedTuple on purpose. The ring's other reader,
``home_qt.render_outcome``, unpacks it positionally (``outcome[0]``,
``outcome[1]``), and the ring is written from a dozen call sites; a NamedTuple
is the one shape that is simultaneously the old tuple and a named schema, so
adopting it breaks no reader and leaves nothing to migrate.

``source`` is the ``DispatchOutcome`` kind the chip was made from, and it is
what makes a COMMAND outcome distinguishable from a dictation one. The chip
label and chip kind alone cannot say whether "refused: nothing staged"
concluded a spoken command or a dictation commit -- ``session_modes.outcome_chip``
maps several unrelated kinds onto the same words. Chips raised directly (a
plugin, a device error, a readiness chip) carry no source and are classified
from the label vocabulary instead, which is all those call sites ever had.
"""
from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple, Optional

# Keep this local rather than importing session_modes: command chip labels use
# the catalog resolver below, and session_modes imports that resolver lazily.
CHIP_CHECK = chr(0x2713)

#: Chip kinds that are progress, not outcomes. The writer never rings these;
#: named here so a reader can say why a kind is absent.
PROGRESS_KINDS = frozenset({"pending", "live"})

#: DispatchOutcome kinds that CONCLUDE an attempt to run a spoken command.
#: These, and only these, are the denominator of "N of your last M commands".
#:
#: Deliberately absent:
#:   command_awaiting_confirmation, command_cancel_window
#:       pending chips -- the writer never rings them, and they are questions,
#:       not outcomes.
#:   pending_reply
#:       the answer to one of those questions. An approved reply is followed
#:       by the command's own command_executed; counting both double-counts
#:       one command.
#:   dictate_commit_unavailable, dictate_commit_refused, dictate_*
#:       the dictation lane. The old reader counted dictate_commit_unavailable
#:       as a "miss", which put a dictation outcome in a sentence about
#:       commands. A staged-text commit with nothing staged is not a command
#:       the app failed to recognise.
COMMAND_SOURCES = frozenset({
    "command_miss",
    "command_executed",
    "hands_free_command_executed",
    "command_failed",
    "hands_free_command_failed",
    "hands_free_command_blocked",
    "hands_free_command_refused",
})

#: Of those, the ones where the app did not recognise what was said. A
#: ``command_miss`` carrying a ``pack`` was recognised perfectly well and
#: refused because its pack is switched off (``session_modes.outcome_chip``
#: renders it "pack off: <pack>"); that is a command outcome but not a miss,
#: and the label is what tells the two apart.
MISS_LABEL = "MISS"


class OutcomeRecord(NamedTuple):
    """One entry of ``DictationApp._outcome_ring``.

    label:  the chip text as the user saw it.
    kind:   the CHIP kind ("success" | "warning" | "error" | "accent").
    at:     ``time.time()`` when it was rung.
    source: the DispatchOutcome kind, or "" for a directly raised chip.
    canonical_id: stable catalog identity for a command, or "" when this is
                  not a command or the catalog could not resolve it.
    """
    label: str
    kind: str
    at: float
    source: str = ""
    canonical_id: str = ""


def record(label, kind, at: float, source: str = "", canonical_id: str = "") -> OutcomeRecord:
    """Build a record with every field normalised to the declared type, so a
    reader never has to defend itself against a None label or a bytes kind."""
    return OutcomeRecord(str(label or ""), str(kind or ""), float(at), str(source or ""),
                         str(canonical_id or getattr(label, "canonical_id", "") or ""))


def as_record(item) -> Optional[OutcomeRecord]:
    """Coerce whatever is in the ring to an OutcomeRecord, or None.

    Tolerates the two pre-109 shapes -- the writer's 3-tuple and the dict the
    old reader expected -- so a ring that survived a hot reload, or a test
    that hand-builds entries, still reads. New entries are always records.
    """
    if isinstance(item, OutcomeRecord):
        return item
    if isinstance(item, dict):
        return record(item.get("label"), item.get("kind"),
                      item.get("at", item.get("time", 0.0)) or 0.0,
                      item.get("source", ""), item.get("canonical_id", ""))
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        at = item[2] if len(item) > 2 else 0.0
        source = item[3] if len(item) > 3 else ""
        canonical_id = item[4] if len(item) > 4 else ""
        try:
            at = float(at)
        except (TypeError, ValueError):
            at = 0.0
        return record(item[0], item[1], at, source, canonical_id)
    return None


@lru_cache(maxsize=1)
def _catalog_indexes() -> tuple[dict[str, str], dict[str, str]]:
    """(normalised spoken phrase -> id, id -> canonical spoken phrase).

    ``command_catalog.load_catalog_json`` intentionally validates and reads
    its JSON on each call. Outcome chips render on the UI path, so the ring
    owns this one-process cache rather than making each chip perform disk I/O.
    An unavailable catalog is a normal degraded state for old history.
    """
    aliases: dict[str, str] = {}
    canonical: dict[str, str] = {}
    try:
        from samsara import command_catalog
        for entry in command_catalog.load_catalog_json() or ():
            command_id = str(entry.get("canonical_id") or "")
            if not command_id:
                continue
            canonical[command_id] = command_catalog.canonical_phrase(entry)
            names = [entry.get("phrase"), *(entry.get("aliases") or ())]
            for name in names:
                if name:
                    aliases.setdefault(command_catalog.normalize_phrase(str(name)), command_id)
    except Exception:
        # A chip must never make command dispatch or Home fail because a
        # catalog file is absent during an update or a test fixture.
        pass
    return aliases, canonical


def canonical_id_for_phrase(phrase) -> str:
    """Catalog id for a spoken command phrase, or "" when unknown."""
    if not phrase:
        return ""
    try:
        from samsara.command_catalog import normalize_phrase
        return _catalog_indexes()[0].get(normalize_phrase(str(phrase)), "")
    except Exception:
        return ""


class _CommandChipLabel(str):
    """String-compatible chip text that carries identity to ``record``."""

    def __new__(cls, text: str, canonical_id: str):
        obj = super().__new__(cls, text)
        obj.canonical_id = canonical_id
        return obj


def command_chip_label(text: str, phrase) -> str:
    """Keep the visible chip text while attaching its catalog identity."""
    return _CommandChipLabel(text, canonical_id_for_phrase(phrase))


def canonical_command_label(canonical_id, stored_label: str) -> str:
    """Full canonical spoken form, falling back to the text already stored."""
    command_id = str(canonical_id or "")
    if not command_id:
        return str(stored_label or "")
    return _catalog_indexes()[1].get(command_id, str(stored_label or ""))


def canonical_command_label_for_phrase(phrase, stored_label: str) -> str:
    """As above for legacy persistent History rows that store a phrase."""
    return canonical_command_label(canonical_id_for_phrase(phrase), stored_label)


def is_command_outcome(item) -> bool:
    """True when this record is the CONCLUSION of an attempt to run a spoken
    command -- the only thing a sentence about commands may count.

    A ``command_executed`` whose chip kind is "accent" is the "working on it"
    chip for a queued or matched command (``session_modes.outcome_chip``): it
    is an acknowledgement, and its real outcome arrives as a later record.
    Counting it would inflate the denominator with half-finished work.
    """
    rec = as_record(item)
    if rec is None or rec.kind in PROGRESS_KINDS:
        return False
    if rec.source:
        if rec.source not in COMMAND_SOURCES:
            return False
        if rec.source in ("command_executed", "hands_free_command_executed"):
            return rec.kind != "accent"
        return True
    # No source: the label vocabulary is all these call sites ever carried.
    return rec.label.upper() == MISS_LABEL or rec.label.startswith(CHIP_CHECK)


def is_command_miss(item) -> bool:
    """True when the app did not recognise a spoken command. Implies
    ``is_command_outcome``; a refusal, a failure and a pack-off are command
    outcomes that were understood."""
    rec = as_record(item)
    if rec is None or not is_command_outcome(rec):
        return False
    return rec.label.upper() == MISS_LABEL


def command_sample(items, limit: int) -> list:
    """The most recent `limit` COMMAND outcomes, oldest first.

    Filtering before taking the window is the point: an 8-entry ring holding
    six dictations and two commands describes two commands, not eight.
    """
    sample = [rec for rec in (as_record(i) for i in items or ())
              if rec is not None and is_command_outcome(rec)]
    return sample[-limit:] if limit and limit > 0 else sample
