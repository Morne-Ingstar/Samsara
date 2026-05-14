"""Tests for the Smart Actions brain dump plugin (Phase 1).

Covers the file-write contract: creation, parent-directory creation,
append behavior, timestamp formatting, ~ / env-var expansion, and
graceful failure on an unwritable target.

Earcon playback can't be unit-tested meaningfully -- verified manually.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the module under test. The plugin lives outside the samsara package
# but is loaded by name at runtime; tests import it directly via the path.
import importlib.util
_PLUGIN_PATH = Path(__file__).parent.parent / "plugins" / "commands" / "smart_actions.py"
_spec = importlib.util.spec_from_file_location("smart_actions", _PLUGIN_PATH)
smart_actions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smart_actions)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestResolveBrainDumpPath:

    def test_expands_tilde(self):
        resolved = smart_actions.resolve_brain_dump_path("~/Documents/foo.md")
        assert resolved.is_absolute()
        assert str(Path.home()) in str(resolved)
        assert resolved.name == "foo.md"

    def test_expands_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_TEST_DIR", str(tmp_path))
        if sys.platform == "win32":
            resolved = smart_actions.resolve_brain_dump_path("%SAMSARA_TEST_DIR%/x.md")
        else:
            resolved = smart_actions.resolve_brain_dump_path("$SAMSARA_TEST_DIR/x.md")
        assert resolved == (tmp_path / "x.md")

    def test_empty_path_returns_default(self):
        resolved = smart_actions.resolve_brain_dump_path("")
        assert resolved == smart_actions.default_brain_dump_path()

    def test_relative_path_anchors_to_home(self):
        resolved = smart_actions.resolve_brain_dump_path("notes.md")
        assert resolved.is_absolute()
        assert resolved == Path.home() / "notes.md"

    def test_default_brain_dump_path_under_documents(self):
        # Per-user default. May resolve through OneDrive-redirected
        # Documents on Windows, which is what we want -- Path.home()
        # already reflects the redirected USERPROFILE.
        default = smart_actions.default_brain_dump_path()
        assert default.name == "Samsara Brain Dump.md"
        assert default.parent.name == "Documents"


# ---------------------------------------------------------------------------
# Entry formatting
# ---------------------------------------------------------------------------

class TestFormatEntry:

    def test_timestamp_heading_and_body(self):
        ts = datetime(2026, 5, 8, 14, 32)
        out = smart_actions.format_entry("call the doctor", now=ts)
        assert out.startswith("## 2026-05-08 14:32\n")
        assert "call the doctor" in out

    def test_trailing_blank_separates_entries(self):
        ts = datetime(2026, 5, 8, 14, 32)
        first = smart_actions.format_entry("a", now=ts)
        second = smart_actions.format_entry("b", now=ts)
        joined = first + second
        # Concatenating two formatted entries must leave a blank line between
        # them so successive ## headings aren't crammed together.
        assert "\n\n## " in joined

    def test_content_is_stripped(self):
        ts = datetime(2026, 5, 8, 14, 32)
        out = smart_actions.format_entry("   spaced thought   ", now=ts)
        assert "spaced thought" in out
        assert "   spaced thought   " not in out


# ---------------------------------------------------------------------------
# Append behavior
# ---------------------------------------------------------------------------

class TestAppendEntry:

    def test_creates_file_with_header_when_missing(self, tmp_path):
        target = tmp_path / "brain.md"
        assert not target.exists()
        ok = smart_actions.append_entry(target, "first thought",
                                        now=datetime(2026, 5, 8, 14, 32))
        assert ok is True
        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert text.startswith("# Samsara Brain Dump")
        assert "first thought" in text

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "brain.md"
        ok = smart_actions.append_entry(target, "deep thought",
                                        now=datetime(2026, 5, 8, 14, 32))
        assert ok is True
        assert target.exists()
        assert (tmp_path / "nested" / "deeper").is_dir()

    def test_appends_without_overwriting(self, tmp_path):
        target = tmp_path / "brain.md"
        ts = datetime(2026, 5, 8, 14, 32)
        smart_actions.append_entry(target, "first", now=ts)
        smart_actions.append_entry(target, "second", now=ts)
        text = target.read_text(encoding="utf-8")
        assert text.count("## 2026-05-08 14:32") == 2
        assert "first" in text
        assert "second" in text
        # Header is written exactly once
        assert text.count("# Samsara Brain Dump") == 1

    def test_preserves_existing_content(self, tmp_path):
        target = tmp_path / "brain.md"
        target.write_text("# Custom header\n\nExisting line\n", encoding="utf-8")
        smart_actions.append_entry(target, "new thought",
                                   now=datetime(2026, 5, 8, 14, 32))
        text = target.read_text(encoding="utf-8")
        assert "Existing line" in text
        assert "new thought" in text
        # Pre-existing file is not overwritten by the auto-header
        assert text.startswith("# Custom header")

    def test_graceful_failure_when_parent_unwritable(self, tmp_path, monkeypatch):
        # Force mkdir to fail to simulate an unwritable destination.
        target = tmp_path / "brain.md"

        def _raise(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(Path, "mkdir", _raise)
        ok = smart_actions.append_entry(target, "won't land",
                                        now=datetime(2026, 5, 8, 14, 32))
        assert ok is False

    def test_graceful_failure_when_write_raises(self, tmp_path, monkeypatch):
        target = tmp_path / "brain.md"
        # File creation succeeds but the append raises.
        smart_actions.ensure_brain_dump_file(target)

        real_open = open

        def _open_that_fails(path, mode="r", *args, **kwargs):
            if str(path) == str(target) and "a" in mode:
                raise OSError("disk full")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(
            "builtins.open", _open_that_fails)
        ok = smart_actions.append_entry(target, "won't land",
                                        now=datetime(2026, 5, 8, 14, 32))
        assert ok is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateBrainDumpPath:

    def test_writable_parent_ok(self, tmp_path):
        ok, msg = smart_actions.validate_brain_dump_path(str(tmp_path / "brain.md"))
        assert ok is True

    def test_empty_rejected(self):
        ok, msg = smart_actions.validate_brain_dump_path("")
        assert ok is False
        assert "empty" in msg.lower()


# ---------------------------------------------------------------------------
# Command handlers (integration with @command decorator and registry)
# ---------------------------------------------------------------------------

def _make_app(config_overrides=None):
    """Build a minimal app-like Mock the command handlers can talk to."""
    app = Mock()
    cfg = {
        'smart_actions': {
            'brain_dump_path': '',  # filled in per-test
            'earcons_enabled': True,
        }
    }
    if config_overrides:
        cfg.update(config_overrides)
    app.config = cfg
    app.play_sound = Mock()
    return app


class TestCommandHandlers:

    def test_note_handler_writes_entry_with_remainder(self, tmp_path):
        target = tmp_path / "brain.md"
        app = _make_app()
        app.config['smart_actions']['brain_dump_path'] = str(target)

        ok = smart_actions.handle_note(app, "to call the doctor")
        assert ok is True
        text = target.read_text(encoding="utf-8")
        assert "to call the doctor" in text

    def test_brain_dump_handler_writes_entry_with_remainder(self, tmp_path):
        target = tmp_path / "brain.md"
        app = _make_app()
        app.config['smart_actions']['brain_dump_path'] = str(target)

        ok = smart_actions.handle_brain_dump(app, "pick up groceries")
        assert ok is True
        text = target.read_text(encoding="utf-8")
        assert "pick up groceries" in text

    def test_handler_returns_false_when_no_content(self, tmp_path):
        target = tmp_path / "brain.md"
        app = _make_app()
        app.config['smart_actions']['brain_dump_path'] = str(target)

        ok = smart_actions.handle_note(app, "")
        assert ok is False
        # File should not have been created since there was nothing to write.
        # (capture_started earcon may have fired -- that's an Option B alias
        # of 'start', which is fine.)
        assert not target.exists()

    def test_handler_plays_capture_saved_on_success(self, tmp_path):
        target = tmp_path / "brain.md"
        app = _make_app()
        app.config['smart_actions']['brain_dump_path'] = str(target)

        smart_actions.handle_note(app, "test thought")

        played = [call.args[0] for call in app.play_sound.call_args_list]
        # Option B aliasing: capture_started -> 'start', capture_saved -> 'success'
        assert smart_actions.EARCON_CAPTURE_STARTED in played
        assert smart_actions.EARCON_CAPTURE_SAVED in played

    def test_handler_silent_when_earcons_disabled(self, tmp_path):
        target = tmp_path / "brain.md"
        app = _make_app()
        app.config['smart_actions']['brain_dump_path'] = str(target)
        app.config['smart_actions']['earcons_enabled'] = False

        ok = smart_actions.handle_note(app, "silent capture")
        assert ok is True
        app.play_sound.assert_not_called()
        # File still gets written even with earcons off.
        assert target.exists()

    def test_handler_plays_error_when_write_fails(self, tmp_path, monkeypatch):
        target = tmp_path / "brain.md"
        app = _make_app()
        app.config['smart_actions']['brain_dump_path'] = str(target)

        # Force ensure_brain_dump_file to fail so the write path falls into
        # the error branch.
        monkeypatch.setattr(smart_actions, "ensure_brain_dump_file", lambda p: False)

        ok = smart_actions.handle_note(app, "doomed thought")
        assert ok is False
        played = [call.args[0] for call in app.play_sound.call_args_list]
        assert smart_actions.EARCON_ERROR in played

    def test_strips_punctuation_from_remainder(self, tmp_path):
        target = tmp_path / "brain.md"
        app = _make_app()
        app.config['smart_actions']['brain_dump_path'] = str(target)

        smart_actions.handle_note(app, ", to remember the thing.")
        text = target.read_text(encoding="utf-8")
        # Leading punctuation Whisper sticks on the remainder is stripped.
        assert "to remember the thing" in text


# ---------------------------------------------------------------------------
# Command registry integration
# ---------------------------------------------------------------------------

class TestCommandRegistration:
    """Verify both phrases route through the plugin registry."""

    def test_note_and_brain_dump_both_registered(self):
        # Re-importing the module would re-register, but conftest clears the
        # registry per-test. Register manually via the decorator we already
        # imported so the lookup matches the runtime path.
        from samsara import plugin_commands
        plugin_commands._REGISTRY.clear()

        plugin_commands.command("note")(smart_actions.handle_note)
        plugin_commands.command("brain dump")(smart_actions.handle_brain_dump)

        entry, remainder = plugin_commands.find_command("note to call the doctor")
        assert entry is not None
        assert entry['phrase'] == 'note'
        assert remainder == 'to call the doctor'

        entry, remainder = plugin_commands.find_command("brain dump pick up groceries")
        assert entry is not None
        assert entry['phrase'] == 'brain dump'
        assert remainder == 'pick up groceries'
