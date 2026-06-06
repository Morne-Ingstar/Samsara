"""
Residual coverage for samsara live modules not already covered elsewhere.

All classes that imported from samsara._stale.* have been removed — _stale is
dead code and its tests gave false signal. The remaining test verifies a
behaviour of the live CommandExecutor that is not explicitly covered in
test_command_executor.py: a command that appears mid-sentence (not at the
start) must NOT match.
"""

import json

import pytest

from samsara.commands import CommandExecutor


@pytest.fixture
def _commands_file(tmp_path):
    commands = {
        "commands": {
            "copy":  {"type": "hotkey", "keys": ["ctrl", "c"]},
            "paste": {"type": "hotkey", "keys": ["ctrl", "v"]},
            "period": {"type": "text",   "text": "."},
            "enter": {"type": "press",   "key": "enter"},
        }
    }
    p = tmp_path / "commands.json"
    p.write_text(json.dumps(commands))
    return p


class TestCommandMatchingBoundary:
    """Edge-cases for CommandExecutor.find_command not covered in test_command_executor.py."""

    def test_command_must_be_at_start_of_text(self, _commands_file):
        """A command embedded after leading words must not match.

        'copy that' -> 'copy' (command is the prefix -- valid)
        'please copy' -> None (command follows other words -- invalid)

        This documents the deliberate semantic of the unified token-prefix
        matcher introduced to prevent false triggers on dictated sentences
        that happen to contain a command word.
        """
        executor = CommandExecutor(_commands_file)

        assert executor.find_command("copy that") == "copy"
        assert executor.find_command("please copy") is None
