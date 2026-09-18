"""Validated, transcript-free onboarding v2 state.

Functions return new dictionaries. Callers persist them through the normal
config transaction, before defaults are merged when migrating legacy data.
"""
from copy import deepcopy
import re

VERSION = 2
SETUP_STATUSES = frozenset({"in_progress", "ready", "deferred"})
NEXT_STEPS = frozenset({"microphone", "ai", "home"})
AI_CHOICES = frozenset({"off", "local", "cloud"})
LESSON_STATUSES = frozenset({"not_started", "completed", "skipped"})


def validate_state(value: object) -> dict:
    """Validate the complete block; reject unknown fields rather than store text."""
    if not isinstance(value, dict):
        raise ValueError("onboarding must be an object")
    allowed = {"version", "setup_status", "next_step", "requested_ai", "lessons"}
    version = value.get("version", VERSION)
    if set(value) - allowed or type(version) is not int or version != VERSION:
        raise ValueError("unsupported onboarding fields/version")
    result = {"version": VERSION, "setup_status": "in_progress",
              "next_step": "microphone", "requested_ai": "off", "lessons": {}}
    result.update(deepcopy(value))
    for key, choices in (("setup_status", SETUP_STATUSES), ("next_step", NEXT_STEPS),
                         ("requested_ai", AI_CHOICES)):
        if not isinstance(result[key], str) or result[key] not in choices:
            raise ValueError(f"invalid onboarding {key}")
    if result["setup_status"] in {"ready", "deferred"} and result["next_step"] != "home":
        raise ValueError("finished/deferred setup resumes at home")
    if not isinstance(result["lessons"], dict):
        raise ValueError("lessons must be an object")
    for lesson_id, progress in result["lessons"].items():
        if not isinstance(lesson_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", lesson_id):
            raise ValueError("invalid lesson ID")
        if not isinstance(progress, dict) or set(progress) != {"status", "revision"}:
            raise ValueError("lesson progress needs only status and revision")
        if not isinstance(progress["status"], str) or progress["status"] not in LESSON_STATUSES:
            raise ValueError("invalid lesson status")
        if type(progress["revision"]) is not int or progress["revision"] < 1:
            raise ValueError("lesson revision must be a positive integer")
    return result


def migrate_onboarding(persisted: dict) -> dict:
    """Preserve legacy setup completion; never infer new lesson success."""
    result = deepcopy(persisted)
    if "onboarding" in persisted:
        block = validate_state(persisted["onboarding"])
    else:
        ready = persisted.get("first_run_complete") is True
        block = validate_state({"setup_status": "ready" if ready else "in_progress",
                                "next_step": "home" if ready else "microphone"})
    result["onboarding"] = block
    result["first_run_complete"] = block["setup_status"] == "ready"
    # tutorial_complete is deliberately left alone; it suppresses only the old tour.
    return result


def update_setup(config: dict, *, setup_status: str, next_step: str,
                 requested_ai: str = "off") -> dict:
    """Record setup progress/intent, never AI consent or activation."""
    result = migrate_onboarding(config)
    result["onboarding"] = validate_state({**result["onboarding"],
        "setup_status": setup_status, "next_step": next_step, "requested_ai": requested_ai})
    result["first_run_complete"] = setup_status == "ready"
    return result


def record_lesson(config: dict, lesson_id: str, *, status: str, revision: int) -> dict:
    """Record an observed result; the practice controller owns proof of success."""
    result = migrate_onboarding(config)
    result["onboarding"]["lessons"][lesson_id] = {"status": status, "revision": revision}
    result["onboarding"] = validate_state(result["onboarding"])
    return result


def lesson_status(config: dict, lesson_id: str, revision: int) -> str:
    progress = migrate_onboarding(config)["onboarding"]["lessons"].get(lesson_id, {})
    return progress.get("status", "not_started") if progress.get("revision") == revision else "not_started"


def startup_step(persisted: dict) -> str:
    """Advisory route: deferred v2 setup returns Home without a forced wizard."""
    return migrate_onboarding(persisted)["onboarding"]["next_step"]
