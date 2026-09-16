"""Choreography is PRESENTATION ONLY.

The `ava_edit.demo_pacing` config key picks between "instant" (daily use)
and "cinematic" (recording a demo). The locked design requires that pacing
cannot change WHAT is applied, only how fast it appears. This module is
built so that it structurally cannot:

  * Pacing contributes NOTHING but dwell. It has no say in the text, in how
    the text is split, or in how many primitive calls are made.
  * The old text is removed in exactly ONE remove call, and the new text is
    injected in exactly ONE inject call, at EVERY setting. Neither is
    sliced, so there is no setting at which a partial string can reach the
    target window, and no setting at which a different number of characters
    is deleted.
  * The deletion is still visibly progressive at both settings without any
    help from here: injection_safety.delete_backwards already sends
    backspaces in batches of DELETE_BATCH (8) with DELETE_BATCH_PAUSE_S
    (0.03 s) between them. Cinematic adds dwell AROUND that, so the viewer
    can read the chip before the old text goes and see the gap before the
    new text lands.

The test that matters -- "demo pacing changes timing only, the applied
result is byte-identical at both settings" -- is therefore true by
construction, not by luck.
"""

from __future__ import annotations

from dataclasses import dataclass

INSTANT = "instant"
CINEMATIC = "cinematic"
DEFAULT = INSTANT


@dataclass(frozen=True)
class Pacing:
    """Dwell only. Nothing here describes the text or the operations."""

    name: str
    #: Seconds to hold with the proposal on the chip before the old text
    #: starts disappearing.
    before_delete_s: float
    #: Seconds to hold on the empty gap before the new text lands.
    after_delete_s: float

    @property
    def total_dwell_s(self) -> float:
        return self.before_delete_s + self.after_delete_s


_PACINGS = {
    INSTANT: Pacing(INSTANT, before_delete_s=0.0, after_delete_s=0.0),
    CINEMATIC: Pacing(CINEMATIC, before_delete_s=0.60, after_delete_s=0.35),
}


def names() -> "tuple[str, ...]":
    return tuple(_PACINGS)


def resolve(app) -> Pacing:
    """The configured pacing. An unknown, missing or malformed value is
    INSTANT: daily use is the default, and a typo in the config must never
    make the app feel broken."""
    cfg = getattr(app, "config", None) or {}
    section = cfg.get("ava_edit")
    name = section.get("demo_pacing") if isinstance(section, dict) else None
    try:
        key = str(name or "").strip().lower()
    except Exception:
        key = ""
    return _PACINGS.get(key, _PACINGS[DEFAULT])
