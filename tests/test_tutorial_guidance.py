"""Guidance audit 2026-09-13 fixes in samsara/ui/tutorial_qt.py: rows 335
(mode-aware wording), 403 (command count never a literal), 165 (hands-free /
undo / cancel). Pure helpers, no widgets."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import session_modes  # noqa: E402
from samsara.ui import tutorial_qt as tut  # noqa: E402


class TestDictationInstruction:
    def test_hold_mode_says_hold_and_release(self):
        text = tut._dictation_instruction("ctrl+shift", "hold")
        assert text.startswith("Hold  CTRL+SHIFT") and "Release to transcribe" in text

    def test_toggle_and_continuous_do_not_say_hold(self):
        toggle = tut._dictation_instruction("ctrl+shift", "toggle")
        cont = tut._dictation_instruction("ctrl+shift", "continuous")
        assert "Hold" not in toggle and "press it again" in toggle
        assert "Hold" not in cont and "continuous" in cont


class TestCommandCount:
    def test_count_comes_from_the_live_registry(self):
        matcher = types.SimpleNamespace(list_commands=lambda: [{"phrase": "a"}, {"phrase": "b"}, {"phrase": "c"}])
        app = types.SimpleNamespace(command_executor=types.SimpleNamespace(_matcher=matcher))
        assert tut._command_count(app) == 3
        assert "There are 3 of them" in tut._commands_banner_text(3)

    def test_no_literal_count_in_the_banner_when_unknown(self):
        assert "150" not in tut._commands_banner_text(None)
        assert "150+" not in Path(tut.__file__).read_text(encoding="utf-8")

    def test_registry_loader_without_an_app_matches_the_dump_tool(self):
        from tools.dump_command_metadata import build_dump
        n = tut._command_count(types.SimpleNamespace())
        assert n == build_dump()["summary"]["commands"] and n > 100


class TestHandsFreeText:
    def test_reads_config_and_constants(self):
        text = tut._hands_free_text({"command_mode": {"enabled": True, "button": "f13", "mode": "toggle"},
                                     "undo_hotkey": "ctrl+alt+u", "cancel_hotkey": "escape"})
        assert "tap F13" in text
        # 378: absent override uses the config guidance default, not legacy global.
        assert f'"{session_modes.DEFAULT_DICTATE_COMMIT_PHRASE}"' in text
        assert f'"{session_modes.SCRATCH_THAT_PHRASE}"' in text
        assert "Ctrl+Alt+U" in text and "Escape" in text
        assert "(turn it on" not in text
        assert "(turn it on" in tut._hands_free_text({})

    def test_done_keeps_recovery_without_repeating_start_and_paste(self):
        text = tut._hands_free_text(
            {"command_mode": {"enabled": True, "button": "f13", "mode": "toggle"},
             "undo_hotkey": "ctrl+alt+u", "cancel_hotkey": "f14"}, recovery_only=True,
        )
        assert f'"{session_modes.SCRATCH_THAT_PHRASE}"' in text
        assert "Ctrl+Alt+U" in text and "F14" in text
        assert "F13" not in text and "to start" not in text and "to paste" not in text


class TestDoneCopy934:
    def test_command_help_uses_catalog_phrase_and_configured_key(self):
        records = [{'canonical_id': tut._HELP_COMMAND_ID, 'phrase': 'available commands'}]
        # Catalog phrase format is shared with all other tutorial command hints.
        phrase = tut._catalog_phrase(records, tut._HELP_COMMAND_ID)
        text = tut._more_text(records, {'command_hotkey': 'ctrl+alt+shift+f12'})
        assert phrase and f'"{phrase}"' in text
        assert '"available commands"' in text
        assert 'Ctrl+Alt+Shift+F12' in text
        assert 'command route' not in text
        assert '"' not in tut._more_text([])

    def test_unavailable_number_command_is_not_promised(self):
        assert tut._pointer_text([]) == 'Manage Ava in Settings > Ava.'
        records = [{'canonical_id': tut._NUMBERS_COMMAND_ID, 'phrase': 'number controls'}]
        phrase = tut._catalog_phrase(records, tut._NUMBERS_COMMAND_ID)
        assert phrase and f'"{phrase}"' in tut._pointer_text(records)
        assert '"number controls"' in tut._pointer_text(records)
