"""Shared reading time for Full feedback and Compact's outcome chip."""
from __future__ import annotations


def routine_receipt(label: str, kind: str) -> bool:
    """Delivery state receipts keep 458's short return-to-rest contract."""
    return getattr(kind, 'value', kind) == 'success' and str(label) in ('typed', 'Captured')


def routine_progress(purpose, owner) -> bool:
    """Hold-release progress is capture state; a new queued action is a message."""
    return getattr(purpose, 'value', purpose) == 'running' and owner == 'hold'


def message_duration_ms(label: str, kind: str, requested_ms=1800, *, purpose='result'):
    """Normalize legacy numeric TTLs; preserve explicit persistent lifecycles.

    Questions/running work belong to their producer, as do explicitly untimed
    faults. Routine delivery receipts are state, not a new command result.
    """
    kind, purpose = getattr(kind, 'value', kind), getattr(purpose, 'value', purpose)
    if purpose != 'result' or kind in ('pending', 'live') or requested_ms is None:
        return None
    if routine_receipt(label, kind):
        return max(0, int(requested_ms))
    duration = min(8000, 2500 + 55 * len(str(label)))
    return max(4000, duration) if kind in ('warning', 'error') else duration


def message_protected(label, kind, expires_at, now, *, purpose='result') -> bool:
    """Only a timed result can defer a routine state update or clear."""
    return (bool(label) and getattr(purpose, 'value', purpose) == 'result'
            and getattr(kind, 'value', kind) not in ('pending', 'live')
            and not routine_receipt(label, kind)
            and expires_at is not None and now < expires_at)
