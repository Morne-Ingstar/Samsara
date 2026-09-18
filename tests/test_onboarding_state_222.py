from copy import deepcopy

import pytest

from samsara.onboarding.state import (lesson_status, migrate_onboarding, record_lesson,
                                      startup_step, update_setup, validate_state)


def test_fresh_cancelled_deferred_and_ready():
    fresh = migrate_onboarding({})
    assert fresh["onboarding"] == {"version": 2, "setup_status": "in_progress",
        "next_step": "microphone", "requested_ai": "off", "lessons": {}}
    assert not fresh["first_run_complete"]
    cancelled = update_setup(fresh, setup_status="in_progress", next_step="ai")
    assert not cancelled["first_run_complete"] and startup_step(cancelled) == "ai"
    deferred = update_setup(cancelled, setup_status="deferred", next_step="home", requested_ai="local")
    assert startup_step(deferred) == "home" and not deferred["first_run_complete"]
    ready = update_setup(deferred, setup_status="ready", next_step="home")
    assert ready["first_run_complete"] and startup_step(ready) == "home"
    assert fresh["onboarding"]["next_step"] == "microphone"


@pytest.mark.parametrize("first, tutorial", [(True, True), (False, True), (True, False)])
def test_legacy_marker_never_passes_new_lessons(first, tutorial):
    old = {"first_run_complete": first, "tutorial_complete": tutorial}
    snapshot = deepcopy(old)
    migrated = migrate_onboarding(old)
    assert old == snapshot
    assert migrated["tutorial_complete"] == tutorial
    assert migrated["first_run_complete"] == first
    assert migrated["onboarding"]["lessons"] == {}
    assert migrate_onboarding(migrated) == migrated


def test_lesson_revision_invalidates_old_success_without_discarding_it():
    config = record_lesson({}, "speak_into_pad", status="completed", revision=1)
    assert lesson_status(config, "speak_into_pad", 1) == "completed"
    assert lesson_status(config, "speak_into_pad", 2) == "not_started"
    skipped = record_lesson(config, "available_commands", status="skipped", revision=1)
    assert lesson_status(skipped, "available_commands", 1) == "skipped"


@pytest.mark.parametrize("bad", [None, [], {"version": 1}, {"version": 2.0}, {"setup_status": "done"},
    {"next_step": "tour"}, {"requested_ai": "remote"}, {"transcript": "discard"},
    {"setup_status": "deferred", "next_step": "microphone"},
    {"lessons": {"a": {"status": "completed", "revision": True}}},
    {"lessons": {"a": {"status": "completed", "revision": 0}}}])
def test_validation_rejects_invalid_and_sensitive_fields(bad):
    with pytest.raises(ValueError):
        validate_state(bad)
