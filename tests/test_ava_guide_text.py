"""Guidance audit 2026-09-13 fixes in samsara/ui/ava_guide_qt.py (rows 341,
345, 462, 616, 125, 126): the text builders, no widgets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import session_modes  # noqa: E402
from samsara.ui import ava_guide_qt as ag  # noqa: E402


def test_intro_follows_authoritative_provider_policy():
    off = ag._ava_intro_text({"cloud_llm": {"enabled": False}})
    selected = ag._ava_intro_text({"ava": {"provider_policy": "cloud"},
                                   "ava_command_session": {"enabled": True}})
    assert "Ava is off" in off
    assert "Ava is off" not in selected
    assert "Cloud mode is ON" not in selected


def test_confirmation_text_names_the_real_answers():
    text = ag._confirmation_text()
    assert '"yes"' in text and '"ava cancel"' in text
    assert f'"{session_modes.SCRATCH_THAT_PHRASE}"' in text


def test_keep_warm_note_follows_the_setting():
    on = ag._keep_warm_note({"ava_command_session": {"keep_warm": True}})
    off = ag._keep_warm_note({"ava_command_session": {"keep_warm": False}})
    default = ag._keep_warm_note({})
    assert "requests warming" in on and "Keep model warm" in on
    assert "off" in off and "frees it" not in off
    assert default == on, "keep_warm defaults to True (config_schema)"
    assert "idles silently" not in on


def test_legacy_provider_switch_is_not_promised():
    lines = ag._usage_lines({"ava_command_session": {"enabled": False, "key": "left_alt"}}, "jarvis")
    assert not any("Ava local" in line or "turns cloud mode off" in line for line in lines)


def test_usage_lines_mention_ava_mode_switch_and_second_key():
    lines = ag._usage_lines({"ava_mode_key": "f13"}, "jarvis")
    ava_mode = next(p for p, m in session_modes._WHOLE_UTTERANCE_SWITCHES.items()
                    if m is session_modes.SessionMode.AVA)
    assert any(f'"{ava_mode}"' in l for l in lines)
    assert any("F13" in line and "Settings > Ava" in line for line in lines)
    off_lines = ag._usage_lines({"ava_command_session": {"enabled": False}}, "jarvis")
    assert any("Right Alt" in line for line in off_lines)


def test_mouse_rows_are_not_offered_as_ava_keys():
    assert not any(v in ("mouse4", "mouse5") for _label, v in ag._AVA_KEY_OPTIONS)
