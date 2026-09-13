"""Guidance audit 2026-09-13 fixes in samsara/ui/first_run_wizard_qt.py:
rows 336 (switch words), 336-338 (stop/sleep words), 371/372 (nested wake
keys instead of the dead flat ones), 879 (wake options), and the built-in
hotkeys note. Pure helpers only -- no widgets."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import session_modes  # noqa: E402
from samsara.constants import DEFAULT_WAKE_PHRASE, DEFAULT_WAKE_PHRASE_OPTIONS  # noqa: E402
from samsara.ui import first_run_wizard_qt as frw  # noqa: E402


class TestWakeConfigKeys:
    def test_defaults_carry_the_live_nested_keys_not_the_dead_flat_ones(self):
        assert "wake_word_timeout" not in frw._DEFAULTS
        assert "wake_word" not in frw._DEFAULTS
        assert frw._DEFAULTS["wake_word_config"]["audio"]["wake_command_timeout"] == 5.0
        assert frw._DEFAULTS["wake_word_config"]["phrase"] == DEFAULT_WAKE_PHRASE

    def test_finalize_writes_nested_key_and_drops_legacy_ones(self):
        cfg = copy.deepcopy(frw._DEFAULTS)
        cfg["wake_word"] = "computer"           # a stale caller still setting the legacy keys
        cfg["wake_word_timeout"] = 7.5
        out = frw._finalize_config(cfg)
        assert "wake_word" not in out and "wake_word_timeout" not in out
        assert out["wake_word_config"]["phrase"] == "computer"
        assert out["wake_word_config"]["audio"]["wake_command_timeout"] == 7.5

    def test_merge_never_replaces_wake_word_config_wholesale(self):
        cfg = {"wake_word_config": {"phrase": "jarvis", "enabled": True,
                                    "audio": {"speech_threshold": 0.03}, "end_words": ["over"]}}
        frw._merge_wake_word_config(cfg, wake_command_timeout=9)
        wwc = cfg["wake_word_config"]
        assert wwc["enabled"] is True and wwc["end_words"] == ["over"]
        assert wwc["audio"] == {"speech_threshold": 0.03, "wake_command_timeout": 9.0}


class TestHandsFreeTip:
    def test_switch_words_are_the_real_ones(self):
        cfg = copy.deepcopy(frw._DEFAULTS)
        frw._apply_use_case_defaults_for_test = None  # sentinel: helper is pure
        cfg["command_mode"] = dict(frw._USE_CASE_CONFIGS["chronic_pain"]["command_mode"])
        tip = frw._hands_free_tip(cfg)
        switches = session_modes._WHOLE_UTTERANCE_SWITCHES
        command_words = [p for p, m in switches.items() if m is session_modes.SessionMode.COMMAND]
        dictate_words = [p for p, m in switches.items() if m is session_modes.SessionMode.DICTATE]
        assert any(f"'{w}'" in tip for w in command_words)
        assert any(f"'{w}'" in tip for w in dictate_words)
        assert "'command'," not in tip and "'command' " not in tip, "bare 'command' is not a switch word"
        assert f"'{session_modes.resolve_ava_invocations(cfg)[0]}'" in tip
        assert f"'{session_modes.DICTATE_COMMIT_PHRASE}'" in tip

    def test_stop_and_sleep_words_are_named(self):
        cfg = copy.deepcopy(frw._DEFAULTS)
        cfg["command_mode"] = dict(frw._USE_CASE_CONFIGS["chronic_pain"]["command_mode"])
        tip = frw._hands_free_tip(cfg)
        assert f"'{session_modes.SESSION_STOP_PHRASES[0]}'" in tip
        assert f"'{session_modes.SESSION_SLEEP_PHRASES[0]}'" in tip
        assert f"'{session_modes.GLOBAL_SESSION_EXIT_PHRASES[0]}'" in tip
        assert "15-minute" in tip                       # inactivity_timeout_s 900

    def test_static_table_no_longer_hardcodes_the_chronic_pain_tip(self):
        assert "chronic_pain" not in frw._USE_CASE_TIPS


class TestWakeOptionsAndBuiltins:
    def test_wake_options_text_names_the_existing_alternatives(self):
        text = frw._wake_options_text(DEFAULT_WAKE_PHRASE)
        for alt in DEFAULT_WAKE_PHRASE_OPTIONS:
            if alt != DEFAULT_WAKE_PHRASE:
                assert alt in text
        assert "coming soon" not in text.lower()

    def test_builtin_hotkeys_note_reads_config(self):
        text = frw._builtin_hotkeys_text({"undo_hotkey": "ctrl+alt+u", "memo_hotkey": "ctrl+alt+q"})
        assert "Ctrl+Alt+U" in text and "Ctrl+Alt+Q" in text and "Escape" in text
        assert "Ctrl+Alt+X" in text          # capture_correction fallback
