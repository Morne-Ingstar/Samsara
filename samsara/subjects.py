"""Immutable Interact references. Labels are never target authority."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    hwnd: int
    pid: int
    process_name: str  # normalized executable image path
    process_start_time: float
    window_class: str

    def __post_init__(self) -> None:
        if (self.hwnd <= 0 or self.pid <= 0 or not self.process_name
                or not math.isfinite(self.process_start_time)
                or self.process_start_time <= 0 or not self.window_class):
            raise ValueError("incomplete window identity")


@dataclass(frozen=True, slots=True)
class SubjectRef:
    kind: Literal["window"]
    identity: WindowIdentity
    ref_id: str
    revision: int
    session_epoch: int
    display_name: str
    title_fingerprint: str
    origin: Literal["name", "pointer", "action_result"]
    bound_at_ns: int

    def __post_init__(self) -> None:
        if self.kind != "window" or not self.ref_id or self.revision < 0:
            raise ValueError("invalid subject reference")
        if self.origin not in ("name", "pointer", "action_result"):
            raise ValueError("invalid subject origin")


@dataclass(frozen=True, slots=True)
class OnsetContext:
    utterance_id: str
    speech_start_ns: int
    observed_at_ns: int
    session_epoch: int
    arm_epoch: int
    policy_generation: int
    subject_ref: SubjectRef | None
    subject_revision: int | None
    pointer_xy: tuple[int, int] | None
    pointer_identity: WindowIdentity | None
    pointer_failure: str | None = None

    def __post_init__(self) -> None:
        if not self.utterance_id or self.speech_start_ns < 0 or self.observed_at_ns < 0:
            raise ValueError("incomplete onset")
        if self.subject_ref is None:
            if self.subject_revision is not None:
                raise ValueError("subject revision without subject")
        elif self.subject_revision != self.subject_ref.revision:
            raise ValueError("subject revision mismatch")
        if (self.pointer_identity is None) == (self.pointer_failure is None):
            raise ValueError("pointer identity or explicit failure required")
        if self.pointer_identity is not None and self.pointer_xy is None:
            raise ValueError("pointer identity without coordinates")


@dataclass(frozen=True, slots=True)
class InteractState:
    phase: Literal["OFF", "ARMED", "CHOOSING", "CONFIRMING"] = "OFF"
    arm_epoch: int = 0
    deadline_ns: int | None = None
    pending_op_id: str | None = None
    candidates: tuple[SubjectRef, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in ("OFF", "ARMED", "CHOOSING", "CONFIRMING"):
            raise ValueError("invalid Interact phase")
        if not isinstance(self.candidates, tuple):
            raise TypeError("candidates must be an immutable tuple")
