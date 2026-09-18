"""Learn descriptors. ALL prose is PLACEHOLDER COPY awaiting owner approval.

Pass current catalog records from catalog_from_registry_rows, not a saved
catalog fallback. This module performs no discovery, IO, registration or Qt work.
Success events are contracts for later packages, not claims of runtime wiring.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    prerequisites: tuple[str, ...]
    command_ids: tuple[str, ...]
    config_refs: tuple[str, ...]
    steps: tuple[str, ...]
    success_events: tuple[str, ...]
    revision: int = 1
    placeholder_copy: bool = True


LESSONS = (
    Lesson("speak_into_pad", ("microphone", "recognition", "practice_target"), (),
           ("mode", "hotkey", "language"), ("Capture a phrase.", "Inspect the delivered result."),
           ("practice.final_text_delivered",)),
    Lesson("available_commands", ("recognition", "command_catalog", "commands_view"),
           ("core_utils.what_can_i_say",), ("command_hotkey",),
           ("Ask for available commands.", "Inspect the Commands view."),
           ("practice.command_recognized", "practice.commands_view_presented")),
    Lesson("draft_then_send", ("live_surface", "practice_draft"), (),
           ("command_mode.dictate_commit_word", "command_mode.stop_phrases", "command_mode.abort_phrases"),
           ("Append two fragments.", "Pause without delivery.", "Commit the draft."),
           ("practice.two_fragments_staged", "practice.pause_without_delivery", "practice.draft_delivered")),
    Lesson("fix_a_word", ("correction", "practice_draft"), (), (),
           ("Select a stable word.", "Replace it using correction or spelling."),
           ("practice.word_revision_changed",)),
    Lesson("undo_and_listen", ("practice_undo", "playback"),
           ("builtin.scratch_that", "core_utils.read_that_back"), ("tts.engine",),
           ("Undo an eligible practice action.", "Listen to the practice receipt."),
           ("practice.undo_completed", "practice.playback_completed_or_self_reported")),
    Lesson("teach_a_word", ("practice_dictionary",), (), (),
           ("Validate a practice word.", "Find it in the practice dictionary."),
           ("practice.dictionary_entry_presented",)),
    Lesson("pauses_while_speaking", ("speech_pace", "practice_target"), (),
           ("accessibility.speech_pace",), ("Try a pace choice.", "Observe a pause in practice."),
           ("practice.pace_changed", "practice.pace_observed")),
    Lesson("optional_ava", ("ai_ava_allowed", "practice_target"),
           ("ask_ollama.hey_ava",), ("ava.provider_policy", "ava_invocations"),
           ("Request a bounded answer.", "Inspect the answer."), ("practice.ava_response_presented",)),
    Lesson("optional_editing", ("ai_editing_allowed", "practice_draft"), (),
           ("ava.provider_policy", "ava_edit.enabled"),
           ("Request an edit proposal.", "Review and explicitly apply it."),
           ("practice.edit_proposal_applied",)),
)


@dataclass(frozen=True)
class ResolvedLesson:
    descriptor: Lesson
    enabled: bool
    reason: str
    phrases: dict[str, str]
    config_values: dict


def resolve_lesson(lesson_id: str, *, catalog: Iterable[Mapping] | None,
                   capabilities: Iterable[str], config: Mapping) -> ResolvedLesson:
    """Resolve ONLY explicit live phrases; missing/ambiguous IDs disable practice."""
    lesson = next((item for item in LESSONS if item.lesson_id == lesson_id), None)
    if lesson is None:
        raise KeyError(lesson_id)
    available = set(capabilities)
    reasons = [f"Missing capability: {key}" for key in lesson.prerequisites if key not in available]
    rows: dict[str, list] = {}
    for row in catalog or ():
        rows.setdefault(row.get("canonical_id", ""), []).append(row)
    phrases = {}
    for cid in lesson.command_ids:
        matches = rows.get(cid, [])
        if len(matches) != 1 or not isinstance(matches[0].get("phrase"), str) or not matches[0]["phrase"].strip():
            reasons.append(f"Live command unavailable or ambiguous: {cid}")
        else:
            phrases[cid] = matches[0]["phrase"]
    values = {}
    for path in lesson.config_refs:
        value = config
        for part in path.split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        values[path] = deepcopy(value)
        if value is None:
            reasons.append(f"Effective config unavailable: {path}")
    return ResolvedLesson(lesson, not reasons, "; ".join(reasons), phrases, values)
