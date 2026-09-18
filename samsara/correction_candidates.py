"""Pure, proposal-only local correction candidates.

The caller supplies a snapshot of personal data.  This service never reads or
writes a correction store, performs recognition, or applies a replacement.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Iterable, Mapping
from typing import Any

from samsara.command_catalog import WHISPER_CONFUSIONS
from samsara.correction_queue import HOMOPHONE_SETS, phonetic_neighbours
from samsara.correction_variants import split_join_variants, written_variants


Span = tuple[int, int]
MAX_CANDIDATES = 12
MAX_PHONETIC_TERMS = 64


@dataclass(frozen=True)
class ApprovedCorrection:
    wrong: str
    right: str
    language: str = "en"
    approved_at: Any = 0


@dataclass(frozen=True)
class CorrectionSnapshot:
    approved_mappings: tuple[ApprovedCorrection, ...] = ()
    taught_vocabulary: tuple[str, ...] = ()
    reviewed_vocabulary: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectionCandidate:
    replacement: str
    span: Span
    source: str
    tier: int

    @property
    def source_tier(self) -> str:
        return self.source

    @property
    def replacement_span(self) -> Span:
        return self.span


Candidate = CorrectionCandidate


def _record_value(record, *names, default=None):
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
    else:
        for name in names:
            if hasattr(record, name):
                return getattr(record, name)
    return default


def _coerce_approved(record, default_language: str) -> ApprovedCorrection | None:
    if isinstance(record, ApprovedCorrection):
        return record
    if isinstance(record, Mapping):
        wrong = record.get("wrong", record.get("source"))
        right = record.get("right", record.get("replacement", record.get("target")))
        language = record.get("language", default_language)
        approved_at = record.get("approved_at", record.get("approval_time", record.get("timestamp", 0)))
    elif isinstance(record, (tuple, list)) and len(record) >= 2:
        wrong, right = record[:2]
        language = record[2] if len(record) > 2 else default_language
        approved_at = record[3] if len(record) > 3 else 0
    else:
        wrong = _record_value(record, "wrong", "source")
        right = _record_value(record, "right", "replacement", "target")
        language = _record_value(record, "language", default=default_language)
        approved_at = _record_value(record, "approved_at", "approval_time", "timestamp", default=0)
    if wrong is None or right is None:
        return None
    return ApprovedCorrection(str(wrong), str(right), str(language or default_language), approved_at)


def build_snapshot(
    approved_mappings=None,
    taught_vocabulary: Iterable[str] = (),
    reviewed_corrections=None,
    *,
    default_language: str = "en",
) -> CorrectionSnapshot:
    """Build an immutable hot-path snapshot from caller-owned personal data.

    ``approved_mappings`` may be a ``wrong -> right`` mapping, records with
    ``wrong/right/language/approved_at`` keys, or ``ApprovedCorrection``
    instances.  No file or application object is accessed here.
    """
    if isinstance(approved_mappings, CorrectionSnapshot):
        return approved_mappings
    records = []
    if isinstance(approved_mappings, Mapping):
        for wrong, value in approved_mappings.items():
            if isinstance(value, Mapping):
                record = dict(value)
                record.setdefault("wrong", wrong)
            else:
                record = {"wrong": wrong, "right": value}
            item = _coerce_approved(record, default_language)
            if item is not None:
                records.append(item)
    else:
        for record in approved_mappings or ():
            item = _coerce_approved(record, default_language)
            if item is not None:
                records.append(item)

    def terms(values) -> tuple[str, ...]:
        if isinstance(values, str):
            values = (values,)
        return tuple(dict.fromkeys(str(value).strip() for value in (values or ()) if str(value).strip()))

    reviewed_terms = []
    if isinstance(reviewed_corrections, Mapping):
        reviewed_terms.extend(reviewed_corrections.keys())
        reviewed_terms.extend(reviewed_corrections.values())
    else:
        reviewed_terms.extend(
            (reviewed_corrections,)
            if isinstance(reviewed_corrections, str)
            else (reviewed_corrections or ())
        )
    return CorrectionSnapshot(tuple(records), terms(taught_vocabulary), terms(reviewed_terms))


snapshot_from = build_snapshot


def _english(language: str) -> bool:
    value = str(language or "").casefold().replace("_", "-")
    return value == "en" or value.startswith("en-") or value == "english"


def _same_language(stored: str, draft: str) -> bool:
    stored_value = str(stored or "").casefold().replace("_", "-")
    draft_value = str(draft or "").casefold().replace("_", "-")
    if stored_value == draft_value:
        return True
    return stored_value.split("-", 1)[0] == draft_value.split("-", 1)[0]


def _approval_key(value: Any) -> tuple[int, float | str]:
    if isinstance(value, datetime):
        return (2, value.timestamp())
    if isinstance(value, (int, float)):
        return (2, float(value))
    text = str(value or "")
    try:
        return (2, datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return (1, text)


def _span(value) -> Span:
    if len(value) != 2:
        raise ValueError("span must be (start, end)")
    start, end = int(value[0]), int(value[1])
    if start < 0 or end < start:
        raise ValueError("span must have 0 <= start <= end")
    return start, end


def _group_candidates(text: str, group: Iterable[str]) -> tuple[str, ...]:
    folded = text.casefold()
    members = tuple(str(member) for member in group)
    if folded not in {member.casefold() for member in members}:
        return ()
    return tuple(member for member in members if member.casefold() != folded)


def suggest_candidates(
    text: str,
    span: Span,
    *,
    adjacent_tokens=None,
    language: str = "en",
    snapshot: CorrectionSnapshot | None = None,
) -> list[CorrectionCandidate]:
    """Return at most twelve ordered proposals for the selected span."""
    selected = str(text or "")
    selected_span = _span(span)
    snapshot = snapshot or CorrectionSnapshot()
    english = _english(language)
    staged: list[tuple[int, tuple, CorrectionCandidate]] = []
    seen: set[tuple[str, Span]] = set()

    def add(replacement: str, replacement_span: Span, source: str, tier: int, order=()) -> None:
        replacement = str(replacement)
        replacement_span = _span(replacement_span)
        key = (replacement, replacement_span)
        if not replacement or key in seen or replacement == selected:
            return
        seen.add(key)
        staged.append((tier, order, CorrectionCandidate(replacement, replacement_span, source, tier)))

    # Tier 1: explicit personal approvals, newest first.
    personal = [
        item for item in snapshot.approved_mappings
        if _same_language(item.language, language) and item.wrong == selected
    ]
    personal.sort(key=lambda item: (item.right.casefold(), item.right))
    personal.sort(key=lambda item: _approval_key(item.approved_at), reverse=True)
    for item in personal:
        add(item.right, selected_span, "personal", 1, (0,))

    if english:
        # Tier 2: existing bundled groups.  The provenance label matters:
        # Whisper's contextual groups are not all homophones.
        tier_two: list[tuple[str, str]] = []
        for group in HOMOPHONE_SETS:
            tier_two.extend((candidate, "homophone") for candidate in _group_candidates(selected, group))
        for group in WHISPER_CONFUSIONS:
            tier_two.extend((candidate, "confusion") for candidate in _group_candidates(selected, group))
        for candidate, source in sorted(tier_two, key=lambda item: (item[0].casefold(), item[0], item[1])):
            add(candidate, selected_span, source, 2, (candidate.casefold(), candidate, source))

        # Tier 3: the supplied, bounded personal term set only.
        terms = tuple(dict.fromkeys(
            list(snapshot.taught_vocabulary) + list(snapshot.reviewed_vocabulary)
            + [item.right for item in snapshot.approved_mappings]
        ))[:MAX_PHONETIC_TERMS]
        for index, candidate in enumerate(phonetic_neighbours(selected, terms, limit=5)):
            add(candidate, selected_span, "phonetic", 3, (index,))

        # Tier 4: finite spelling/case/number alternatives.
        for candidate in written_variants(selected):
            add(candidate, selected_span, "variant", 4, (candidate.casefold(), candidate))

        # Tier 5: a complete span is returned for a two-word replacement.
        for candidate, candidate_span in split_join_variants(selected, selected_span, adjacent_tokens):
            add(candidate, candidate_span, "split_join", 5, (candidate.casefold(), candidate))

    staged.sort(key=lambda item: (item[0], item[1], item[2].replacement.casefold(), item[2].replacement, item[2].span))
    return [candidate for _tier, _order, candidate in staged[:MAX_CANDIDATES]]


get_candidates = suggest_candidates


__all__ = [
    "ApprovedCorrection",
    "Candidate",
    "CorrectionCandidate",
    "CorrectionSnapshot",
    "MAX_PHONETIC_TERMS",
    "build_snapshot",
    "get_candidates",
    "snapshot_from",
    "suggest_candidates",
]
