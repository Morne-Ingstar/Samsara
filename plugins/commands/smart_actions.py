"""Smart Actions Phase 1: Brain dump plugin.

Voice-to-markdown capture for users with ADHD, chronic pain, or any condition
where typing friction destroys a thought before it can be written down.

"Jarvis, note to call the doctor about the prescription"
"Jarvis, brain dump pick up groceries tomorrow"

Appends a timestamped entry to the configured brain dump file. Auditory
feedback (earcons) confirms capture without requiring the user to look at
the screen.

This phase has no AI involvement -- pure voice -> timestamped markdown
append. Phase 2 wires the webhook bridge; Phase 3 layers Claude/Ollama.
The file-write contract here is the foundation those phases build on.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from samsara.plugin_commands import command


logger = logging.getLogger(__name__)


DEFAULT_BRAIN_DUMP_FILENAME = "Samsara Brain Dump.md"
FILE_HEADER = (
    "# Samsara Brain Dump\n"
    "\n"
    "Captured voice notes. Appended chronologically -- newest at the bottom.\n"
    "\n"
)

# Earcon names. These are the public contract for the Smart Actions audio
# vocabulary -- plugins and Phase 2/3 code should refer to these constants
# rather than hard-coding strings, so renames stay local.
#
# Phase 2 added dedicated per-theme WAVs (sounds/themes/<theme>/<name>.wav)
# and extended play_sound() to auto-discover them, so we no longer alias to
# the legacy four earcons.
EARCON_CAPTURE_STARTED = "capture_started"
EARCON_CAPTURE_SAVED = "capture_saved"
EARCON_ERROR = "error"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def default_brain_dump_path():
    """Return the per-user default path for the brain dump file.

    Resolves to ~/Documents/Samsara Brain Dump.md, with the user's actual
    home directory (so it works on OneDrive-redirected Documents folders
    that resolve through %USERPROFILE%).
    """
    return Path.home() / "Documents" / DEFAULT_BRAIN_DUMP_FILENAME


def get_config(app):
    """Read the smart_actions config block, applying defaults for missing keys."""
    cfg = {}
    if app is not None and hasattr(app, 'config'):
        cfg = app.config.get('smart_actions', {}) or {}
    return {
        'brain_dump_path': cfg.get('brain_dump_path', str(default_brain_dump_path())),
        'earcons_enabled': cfg.get('earcons_enabled', True),
    }


def resolve_brain_dump_path(raw_path):
    """Resolve a configured path string into an absolute Path.

    Handles:
      - ~ expansion (~/Documents/foo.md)
      - Windows backslashes and forward slashes
      - Environment variables (%USERPROFILE%, $HOME)
      - Relative paths (resolved against home directory)
    """
    if not raw_path:
        return default_brain_dump_path()

    text = str(raw_path).strip()
    text = os.path.expandvars(text)
    path = Path(text).expanduser()

    if not path.is_absolute():
        path = Path.home() / path

    return path


# ---------------------------------------------------------------------------
# File-write contract
# ---------------------------------------------------------------------------

def format_entry(content, now=None):
    """Format a single brain-dump entry.

    Returns a string with an H2 timestamp heading, the content body, and a
    trailing blank line so successive entries are separated by two blank
    lines after the previous entry's own trailing blank line.
    """
    when = now if now is not None else datetime.now()
    timestamp = when.strftime("%Y-%m-%d %H:%M")
    body = (content or "").strip()
    return f"## {timestamp}\n{body}\n\n"


def ensure_brain_dump_file(path):
    """Create the file (and parent dirs) with a header if it doesn't exist.

    Returns True on success, False if the path is unwritable.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, 'w', encoding='utf-8') as f:
                f.write(FILE_HEADER)
        return True
    except OSError as e:
        logger.exception(f"[SMART ACTIONS] Cannot prepare brain dump file at {path}: {e}")
        return False


def append_entry(path, content, now=None):
    """Append a brain-dump entry to the file.

    Creates the file with a header if missing. Returns True on success,
    False on any filesystem failure. Never raises -- callers can rely on
    the boolean to decide which earcon to play.
    """
    if not ensure_brain_dump_file(path):
        return False

    entry = format_entry(content, now=now)
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(entry)
            f.flush()
        return True
    except OSError as e:
        logger.exception(f"[SMART ACTIONS] Failed to append to {path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Earcon helper
# ---------------------------------------------------------------------------

def _play_earcon(app, sound_type, smart_cfg):
    """Play an earcon if smart_actions earcons_enabled is True."""
    if not smart_cfg.get('earcons_enabled', True):
        return
    if app is None or not hasattr(app, 'play_sound'):
        return
    try:
        app.play_sound(sound_type)
    except Exception:
        logger.exception(f"[SMART ACTIONS] Earcon '{sound_type}' failed")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _strip_command_punctuation(text):
    """Strip Whisper punctuation noise from the captured remainder."""
    if not text:
        return ''
    return text.strip().strip(",.:;!?\"'")


def _do_capture(app, remainder):
    """Shared capture logic for note + brain dump command handlers."""
    smart_cfg = get_config(app)

    # Auditory "I heard you" -- fires before the file work so the user gets
    # immediate feedback even if disk I/O is slow.
    _play_earcon(app, EARCON_CAPTURE_STARTED, smart_cfg)

    content = _strip_command_punctuation(remainder)
    if not content:
        # Phase 1 limitation: we don't yet hook into long_dictation to wait
        # for a follow-on utterance. Flag clearly so the user knows to retry
        # with content in one breath. See handoff for Phase 2 plan.
        logger.info("[SMART ACTIONS] Brain dump invoked with no content -- skipping")
        print("[SMART ACTIONS] Say the note in one breath, e.g. 'Jarvis, note to call the doctor'")
        _play_earcon(app, EARCON_ERROR, smart_cfg)
        return False

    path = resolve_brain_dump_path(smart_cfg.get('brain_dump_path'))
    ok = append_entry(path, content)
    if ok:
        print(f"[SMART ACTIONS] Captured to {path}: {content}")
        _play_earcon(app, EARCON_CAPTURE_SAVED, smart_cfg)
        return True

    print(f"[SMART ACTIONS] Failed to write brain dump entry to {path}")
    _play_earcon(app, EARCON_ERROR, smart_cfg)
    return False


@command("note")
def handle_note(app, remainder):
    """Capture a voice note. 'Jarvis, note to call the doctor about it.'"""
    return _do_capture(app, remainder)


@command("brain dump")
def handle_brain_dump(app, remainder):
    """Capture a voice note. 'Jarvis, brain dump pick up groceries.'"""
    return _do_capture(app, remainder)


# ---------------------------------------------------------------------------
# Settings helper
# ---------------------------------------------------------------------------

def open_brain_dump_file(path):
    """Open the brain dump file in the system default editor.

    Called from the Settings UI button. Returns True on launch success.
    """
    try:
        resolved = resolve_brain_dump_path(path)
        # Make sure it exists before launching the editor -- otherwise the
        # OS just shows "file not found".
        ensure_brain_dump_file(resolved)
        if sys.platform == 'win32':
            os.startfile(str(resolved))
        elif sys.platform == 'darwin':
            import subprocess
            subprocess.Popen(['open', str(resolved)])
        else:
            import subprocess
            subprocess.Popen(['xdg-open', str(resolved)])
        return True
    except Exception:
        logger.exception(f"[SMART ACTIONS] Could not open brain dump file at {path}")
        return False


def validate_brain_dump_path(raw_path):
    """Validate that a path is suitable for the brain dump.

    Returns (ok: bool, message: str). Used by the Settings UI to surface
    a clear error before the user commits.
    """
    if not raw_path or not str(raw_path).strip():
        return False, "Path cannot be empty."
    try:
        resolved = resolve_brain_dump_path(raw_path)
    except Exception as e:
        return False, f"Invalid path: {e}"

    parent = resolved.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Cannot create parent directory {parent}: {e}"

    if not os.access(str(parent), os.W_OK):
        return False, f"Parent directory is not writable: {parent}"

    if resolved.exists() and not os.access(str(resolved), os.W_OK):
        return False, f"File exists but is not writable: {resolved}"

    return True, f"OK -> {resolved}"
