import pytest

from samsara.onboarding.lessons import LESSONS, resolve_lesson


def test_every_descriptor_has_explicit_contract_and_placeholder_prose():
    assert len({item.lesson_id for item in LESSONS}) == len(LESSONS) == 9
    for item in LESSONS:
        assert item.prerequisites and item.steps and item.success_events
        assert item.revision == 1 and item.placeholder_copy


def test_live_phrase_change_is_reflected_without_fallback():
    config = {"command_hotkey": "ctrl+alt+c"}
    capabilities = {"recognition", "command_catalog", "commands_view"}
    row = {"canonical_id": "core_utils.what_can_i_say", "phrase": "show current commands"}
    result = resolve_lesson("available_commands", catalog=[row], capabilities=capabilities, config=config)
    assert result.enabled and result.phrases[row["canonical_id"]] == row["phrase"]
    row["phrase"] = "show available commands"
    assert resolve_lesson("available_commands", catalog=[row], capabilities=capabilities,
                          config=config).phrases[row["canonical_id"]] == row["phrase"]
    for rows in ([], None, [row, row], [{"canonical_id": row["canonical_id"], "aliases": ["fallback"]}]):
        disabled = resolve_lesson("available_commands", catalog=rows, capabilities=capabilities, config=config)
        assert not disabled.enabled and row["canonical_id"] in disabled.reason and not disabled.phrases


def test_effective_session_words_and_capabilities_are_supplied_by_caller():
    config = {"command_mode": {"dictate_commit_word": "send it", "stop_phrases": ["halt"], "abort_phrases": ["sleep"]}}
    lesson = resolve_lesson("draft_then_send", catalog=[], capabilities={"live_surface", "practice_draft"}, config=config)
    assert lesson.enabled
    assert lesson.config_values["command_mode.dictate_commit_word"] == "send it"
    lesson.config_values["command_mode.stop_phrases"].append("change")
    assert config["command_mode"]["stop_phrases"] == ["halt"]
    missing = resolve_lesson("draft_then_send", catalog=[], capabilities=set(), config={})
    assert not missing.enabled and "Missing capability" in missing.reason and "Effective config unavailable" in missing.reason


def test_unknown_lesson_never_gets_invented():
    with pytest.raises(KeyError):
        resolve_lesson("invented", catalog=[], capabilities=set(), config={})
