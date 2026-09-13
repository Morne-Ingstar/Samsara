"""Guidance audit 2026-09-13 fixes in samsara/ui/ava_guide_qt.py (rows 341,
345, 462, 616, 125, 126): the text builders, no widgets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import session_modes  # noqa: E402
from samsara.ui import ava_guide_qt as ag  # noqa: E402


def test_intro_is_conditional_on_cloud_mode():
    off = ag._ava_intro_text({"cloud_llm": {"enabled": False}})
    on = ag._ava_intro_text({"cloud_llm": {"enabled": True}})
    assert "By default" in off and "cloud mode" in off
    assert "Cloud mode is ON" in on
    assert "runs entirely on your machine —" not in on


def test_confirmation_text_names_the_real_answers():
    text = ag._confirmation_text()
    assert '"yes"' in text and '"ava cancel"' in text
    assert f'"{session_modes.SCRATCH_THAT_PHRASE}"' in text


def test_keep_warm_note_follows_the_setting():
    on = ag._keep_warm_note({"ava_command_session": {"keep_warm": True}})
    off = ag._keep_warm_note({"ava_command_session": {"keep_warm": False}})
    default = ag._keep_warm_note({})
    assert "keeps the model loaded" in on and "Keep model warm" in on
    assert "off" in off and "frees it" in off
    assert default == on, "keep_warm defaults to True (config_schema)"
    assert "idles silently" not in on


def test_ava_local_is_a_mode_switch_not_a_question():
    lines = ag._usage_lines({"ava_command_session": {"enabled": False, "key": "left_alt"}}, "jarvis")
    local = next(l for l in lines if "Ava local" in l)
    assert "turns cloud mode off" in local and "same as above" not in local


def test_usage_lines_mention_ava_mode_switch_and_second_key():
    lines = ag._usage_lines({"ava_command_session": {"enabled": True, "key": "f10"}}, "jarvis")
    ava_mode = next(p for p, m in session_modes._WHOLE_UTTERANCE_SWITCHES.items()
                    if m is session_modes.SessionMode.AVA)
    assert any(f'"{ava_mode}"' in l for l in lines)
    assert any("Hold F10 for the Ava command session." in l for l in lines)
    off_lines = ag._usage_lines({"ava_command_session": {"enabled": False}}, "jarvis")
    assert any("Left Alt" in l and "(off" in l for l in off_lines)


def test_mouse_rows_are_not_offered_as_ava_keys():
    assert not any(v in ("mouse4", "mouse5") for _label, v in ag._AVA_KEY_OPTIONS)
