"""Guidance facts are pure and source checks never import dictation.py."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara.onboarding.guidance_facts import facts_from_config  # noqa: E402


def test_custom_controls_are_exposed_without_mutating_config():
    config = {
        "hotkey": "alt+space",
        "command_hotkey": "ctrl+alt+v",
        "wake_word_hotkey": "ctrl+alt+k",
        "dictate_commit_hotkey": "ctrl+enter",
        "mode": "toggle",
        "command_mode": {
            "mode": "toggle",
            "dictate_commit_word": "submit",
            "stop_phrases": ["pause now"],
            "inactivity_timeout_s": 420,
        },
        "wake_word_config": {
            "phrase": "hello orbit",
            "phrase_options": ["hello orbit", "orbit"],
            "end_words": ["wrap up"],
            "opens_session": True,
        },
        "wake_profiles": [{"phrase": "write report", "send_word": "deliver", "mode": "stage_send"}],
    }
    facts = facts_from_config(config)
    assert facts.wake_phrase == "hello orbit"
    assert facts.wake_aliases == ()
    assert facts.wake_phrases == ("hello orbit", "write report")
    assert facts.wake_profile_send_words[0].send_word == "deliver"
    assert facts.end_words == ("wrap up",)
    assert facts.dictation_hotkey == "alt+space"
    assert facts.dictation_behavior == "press_to_start_press_to_stop"
    assert facts.wake_activation == "hands_free_session"
    assert facts.idle_timeout_meaning == "closes after 420 seconds without activity"
    assert facts.session_end.commit_word == "submit"
    assert facts.session_end.stop_words == ("pause now",)
    assert config["wake_word_config"]["phrase"] == "hello orbit"


def test_hold_and_invalid_wake_session_request_stays_one_command_window():
    facts = facts_from_config({"mode": "hold", "wake_word_config": {"opens_session": True}})
    assert facts.dictation_behavior == "hold_to_dictate"
    assert facts.wake_activation_requested_session is True
    assert facts.wake_activation == "one_command_window"


def test_defaults_cover_end_send_and_distinct_session_endings():
    facts = facts_from_config({})
    assert facts.end_words
    assert facts.send_words
    assert facts.session_end.commit_word == "finish"
    assert facts.session_end.stop_words
    assert facts.session_end.discard_words
    assert facts.session_end.commit_word not in facts.session_end.stop_words


def test_converted_hint_calls_do_not_embed_default_wake_or_end_phrases():
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8-sig"))
    ids = {"wake_mode_activated", "wake_dictation_end_word", "wake_word_suggestion"}
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if isinstance(node.args[0], ast.Constant) and node.args[0].value in ids:
            calls.append(ast.get_source_segment((ROOT / "dictation.py").read_text(encoding="utf-8-sig"), node))
    assert len(calls) == 3
    assert all("Jarvis" not in call and "'over'" not in call for call in calls)
