"""345a immutable subject and onset records."""

from dataclasses import FrozenInstanceError

import pytest

from samsara.subjects import InteractState, OnsetContext, SubjectRef, WindowIdentity


def identity(hwnd=101):
    return WindowIdentity(hwnd, 42, r"c:\windows\explorer.exe", 123.5, "CabinetWClass")


def subject(hwnd=101, *, ref_id="controller-1", revision=1):
    return SubjectRef("window", identity(hwnd), ref_id, revision, 7,
                      "Explorer — Downloads", "title-hash", "pointer", 123456)


def test_two_windows_in_same_process_are_distinct_subjects():
    first, second = subject(101), subject(102, ref_id="controller-2")
    assert first != second
    assert first.identity != second.identity
    assert first.identity.pid == second.identity.pid


def test_title_is_not_part_of_ordinary_window_identity():
    before = subject()
    renamed = SubjectRef(before.kind, before.identity, before.ref_id, before.revision,
                         before.session_epoch, "Explorer — Renamed", "new-title-hash",
                         before.origin, before.bound_at_ns)
    assert before.identity == renamed.identity
    assert before != renamed


def test_reference_identity_includes_controller_and_session_authority():
    original = subject()
    assert original != SubjectRef("window", original.identity, "new-token", 1, 7,
                                  original.display_name, original.title_fingerprint,
                                  original.origin, original.bound_at_ns)
    assert original != SubjectRef("window", original.identity, original.ref_id, 1, 8,
                                  original.display_name, original.title_fingerprint,
                                  original.origin, original.bound_at_ns)


@pytest.mark.parametrize("bad", [
    (0, 42, "explorer.exe", 123.5, "CabinetWClass"),
    (101, 0, "explorer.exe", 123.5, "CabinetWClass"),
    (101, 42, "", 123.5, "CabinetWClass"),
    (101, 42, "explorer.exe", 0, "CabinetWClass"),
    (101, 42, "explorer.exe", float("nan"), "CabinetWClass"),
    (101, 42, "explorer.exe", 123.5, ""),
])
def test_partial_window_identity_is_rejected(bad):
    with pytest.raises(ValueError):
        WindowIdentity(*bad)


def test_onset_freezes_subject_pointer_and_epochs():
    ref = subject()
    onset = OnsetContext("u1", 100, 105, 7, 2, 9, ref, ref.revision,
                         (12, 34), identity(102))
    assert onset.subject_ref is ref
    assert onset.pointer_identity != ref.identity
    assert onset.subject_revision == 1
    with pytest.raises(FrozenInstanceError):
        onset.pointer_xy = (55, 66)


def test_onset_can_carry_explicit_pointer_failure_without_late_target():
    onset = OnsetContext("u2", 100, 200, 7, 2, 9, None, None,
                         (12, 34), None, "own_overlay")
    assert onset.pointer_identity is None
    assert onset.pointer_failure == "own_overlay"


def test_onset_rejects_partial_or_conflicting_target_snapshots():
    ref = subject()
    with pytest.raises(ValueError, match="pointer identity or explicit failure"):
        OnsetContext("u2", 100, 105, 7, 2, 9, ref, 1, (1, 2), None)
    with pytest.raises(ValueError, match="subject revision mismatch"):
        OnsetContext("u2", 100, 105, 7, 2, 9, ref, 2, (1, 2), identity())


def test_interact_candidates_are_immutable_and_phase_checked():
    state = InteractState("CHOOSING", 2, 1000, None, (subject(), subject(102)))
    assert len(state.candidates) == 2
    with pytest.raises(FrozenInstanceError):
        state.phase = "ARMED"
    with pytest.raises(TypeError):
        InteractState(candidates=[subject()])
    with pytest.raises(ValueError):
        InteractState(phase="UNKNOWN")
