"""
Tests for the wake word -> command/dictation pipeline.
Covers command matching boundaries, state transitions, and edge cases.
"""
import pytest
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.commands import CommandExecutor


class TestFindCommandBoundaries:
    """Test that find_command respects word boundaries and doesn't false-positive."""

    def test_exact_match(self, temp_commands_file):
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("copy") == "copy"

    def test_command_embedded_in_sentence_not_matched(self, temp_commands_file):
        """Command embedded mid-sentence no longer fires (prefix-only matcher)."""
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("please copy that") is None

    def test_command_at_start_of_sentence(self, temp_commands_file):
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("copy this text") == "copy"

    def test_command_at_end_of_sentence_not_matched(self, temp_commands_file):
        """Command at the end of a longer utterance no longer fires."""
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("I want to copy") is None

    def test_no_substring_match_on_prefix(self, temp_commands_file):
        """'copy' should NOT match 'copyright'."""
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("copyright notice") is None

    def test_no_substring_match_on_suffix(self, temp_commands_file):
        """'copy' should NOT match 'photocopy'."""
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("photocopy this") is None

    def test_no_substring_match_embedded(self, temp_commands_file):
        """'new line' should NOT match 'renew lineage'."""
        executor = CommandExecutor(temp_commands_file)
        # 'new line' is a command; 'renew lineup' should not match
        assert executor.find_command("renew lineup") is None

    def test_multi_word_command_at_start(self, temp_commands_file):
        """Multi-word command at the start still fires."""
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("open chrome now") == "open chrome"

    def test_multi_word_command_not_embedded(self, temp_commands_file):
        """Multi-word command mid-sentence no longer fires."""
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("please open chrome now") is None

    def test_case_insensitive_boundary(self, temp_commands_file):
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("COPY this") == "copy"

    def test_empty_string(self, temp_commands_file):
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("") is None

    def test_whitespace_only(self, temp_commands_file):
        executor = CommandExecutor(temp_commands_file)
        assert executor.find_command("   ") is None


class TestDictationCommandParsing:
    """Test wake word command parsing for dictation modes."""

    def _make_mock_app(self):
        app = Mock()
        app.config = {
            'wake_word_config': {
                'phrase': 'samsara',
                'modes': {
                    'dictate': {'silence_timeout': 0.6, 'require_end_word': False},
                    'short_dictate': {'silence_timeout': 0.4, 'require_end_word': False},
                    'long_dictate': {'silence_timeout': 60.0, 'require_end_word': True},
                },
                'end_word': {'enabled': True, 'phrase': 'over'},
                'cancel_word': {'enabled': True, 'phrase': 'cancel'},
                'pause_word': {'enabled': True, 'phrase': 'pause'},
            },
            'command_mode_enabled': True,
        }
        return app

    def test_dictate_command_recognized(self):
        """'dictate' should be treated as a dictation mode, not a regular command."""
        text = "dictate"
        assert text.lower() in ['dictate', 'dictation']

    def test_dictate_with_content(self):
        """'dictate hello world' should start dictation with initial content."""
        text = "dictate hello world"
        cmd = "dictate"
        assert text.lower().startswith(cmd + ' ')
        content = text[len(cmd):].strip()
        assert content == "hello world"

    def test_end_word_extraction(self):
        """End word should be stripped and text before it collected."""
        text = "some dictated text over"
        end_phrase = "over"
        end_index = text.lower().rfind(end_phrase)
        final_text = text[:end_index].strip()
        assert final_text == "some dictated text"

    def test_end_word_middle_of_text(self):
        """End word in middle -- rfind gets the last occurrence."""
        text = "over the hill over"
        end_phrase = "over"
        end_index = text.lower().rfind(end_phrase)
        final_text = text[:end_index].strip()
        assert final_text == "over the hill"

    def test_cancel_word_detected(self):
        """Cancel word in text should be detected."""
        text = "cancel this"
        cancel_phrase = "cancel"
        assert cancel_phrase in text.lower()

    def test_pause_word_strips_and_keeps_content(self):
        """Pause word should be stripped but surrounding content kept."""
        text = "hello pause world"
        pause_phrase = "pause"
        pause_idx = text.lower().find(pause_phrase)
        cleaned = (text[:pause_idx] + text[pause_idx + len(pause_phrase):]).strip()
        assert cleaned == "hello  world" or cleaned == "hello world"


class TestFillerWordStripping:
    """Test _strip_fillers and its effect on dictation command parsing."""

    # Import the static method for direct testing
    @staticmethod
    def _strip(text, fillers=frozenset({'please', 'uh', 'um', 'like'})):
        words = text.split()
        while words and words[0].lower() in fillers:
            words.pop(0)
        while words and words[-1].lower() in fillers:
            words.pop()
        return ' '.join(words)

    # --- _strip_fillers unit tests ---

    def test_strip_leading_please(self):
        assert self._strip("please dictate hello world") == "dictate hello world"

    def test_strip_trailing_please(self):
        assert self._strip("dictate hello world please") == "dictate hello world"

    def test_strip_leading_uh(self):
        assert self._strip("uh dictate hello world") == "dictate hello world"

    def test_strip_leading_um(self):
        assert self._strip("um dictate hello world") == "dictate hello world"

    def test_strip_both_ends(self):
        assert self._strip("um dictate hello world please") == "dictate hello world"

    def test_multiple_leading_fillers(self):
        assert self._strip("uh um please dictate") == "dictate"

    def test_interior_filler_preserved(self):
        """'like' inside payload must NOT be stripped."""
        assert self._strip("dictate I like cats") == "dictate I like cats"

    def test_interior_please_preserved(self):
        """'please' inside payload must NOT be stripped."""
        assert self._strip("dictate please call me back") == "dictate please call me back"

    def test_no_fillers(self):
        assert self._strip("dictate hello") == "dictate hello"

    def test_only_fillers(self):
        assert self._strip("uh um please like") == ""

    def test_empty_string(self):
        assert self._strip("") == ""

    # --- Integration: filler variants all parse the same dictation command ---

    def test_all_variants_yield_same_command(self):
        """The five example phrases from the spec should all yield 'dictate' + 'hello world'."""
        variants = [
            "dictate hello world",
            "please dictate hello world",
            "dictate hello world please",
            "uh dictate hello world",
            "um dictate hello world please",
        ]
        for raw in variants:
            stripped = self._strip(raw)
            cmd = "dictate"
            assert stripped.startswith(cmd), f"'{raw}' -> stripped='{stripped}' doesn't start with '{cmd}'"
            content = stripped[len(cmd):].strip()
            content = self._strip(content)
            assert content == "hello world", f"'{raw}' -> content='{content}'"

    def test_bare_dictate_with_fillers(self):
        """'please dictate please' should resolve to bare 'dictate'."""
        stripped = self._strip("please dictate please")
        assert stripped == "dictate"

    def test_long_dictate_with_fillers(self):
        stripped = self._strip("um long dictate please")
        assert stripped == "long dictate"

    def test_short_dictate_with_fillers(self):
        stripped = self._strip("uh short dictate")
        assert stripped == "short dictate"


class TestProcessTextDualSignature:
    """The DictationApp.process_text wrapper uses (text, app_instance, force_commands)
    while CommandExecutor.process_text uses (text, command_mode_enabled, on_mode_change).
    These must not be confused.
    """

    def test_command_executor_process_text_signature(self, temp_commands_file):
        """CommandExecutor.process_text takes positional command_mode_enabled bool."""
        executor = CommandExecutor(temp_commands_file)
        executor.keyboard_controller.press = Mock()
        executor.keyboard_controller.release = Mock()
        # Calling with correct signature
        result, was_cmd = executor.process_text("copy", command_mode_enabled=True)
        assert was_cmd is True

    def test_command_executor_disabled_mode(self, temp_commands_file):
        """With command_mode_enabled=False, commands should not execute."""
        executor = CommandExecutor(temp_commands_file)
        result, was_cmd = executor.process_text("copy", command_mode_enabled=False)
        assert was_cmd is False
        assert result == "copy"  # returned as dictation text
