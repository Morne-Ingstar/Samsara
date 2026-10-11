"""Shared logger factory for the fail-loud pass.

dictation.py (the app entry point) already builds the real handler chain on
the root logger before anything else runs: a RotatingFileHandler at
~/.samsara/logs/samsara.log (5MB x 3 backups, DEBUG) plus a console handler
(INFO+ when a console exists / not frozen). get_logger() here never
duplicates that -- it just returns a logger under the "Samsara" hierarchy,
which propagates up to whatever is already attached to the root logger.

The one case this module DOES configure handlers itself is a module being
imported standalone (a test, a tools/ script) before dictation.py's own
bootstrap has run -- without this, log calls would be silently dropped
rather than reaching the same file. That fallback mirrors dictation.py's
config exactly (same path, same rotation, same format) so the two paths are
never distinguishable in the log itself.

Handlers installed here are tagged with SAMSARA_LOG_HANDLER_TAG so
dictation.py's own (later) setup can find and remove exactly this fallback
pair before installing its real one, instead of either double-attaching
(every line logged twice) or blanket-clearing the root logger (which would
also rip out anything unrelated already attached there, e.g. pytest's own
log-capture handler).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from enum import Enum
from logging.handlers import RotatingFileHandler

from samsara.paths import samsara_home_dir

_configure_lock = threading.Lock()
_configured = False

# Shared marker attribute set on every handler this module or dictation.py's
# own setup attaches to the root logger -- lets either side identify and
# remove exactly "our" handlers without touching anything else on root.
SAMSARA_LOG_HANDLER_TAG = "_samsara_log_handler"

# Same path dictation.py's own bootstrap resolves to (its LOG_DIR/LOG_FILE
# module constants) -- exported here so other modules (e.g. the log viewer
# window) can import one shared constant instead of recomputing or
# hardcoding a second copy of "logs" / "samsara.log".
SAMSARA_LOG_FILE = samsara_home_dir() / "logs" / "samsara.log"


def describe_text(text):
    """Describe private content without including it in ordinary logs.

    Structured outcomes retain numeric measurements while string fields
    are described. Unknown objects are named by type, never stringified.
    """
    try:
        if isinstance(text, BaseException):
            try:
                result = f"{type(text).__name__}: {len(str(text))} chars"
            except BaseException:
                result = f"{type(text).__name__}: <str failed>"
            if isinstance(text, OSError):
                result += f" errno={describe_text(text.errno)} winerror={describe_text(getattr(text, 'winerror', None))}"
                filename = text.filename
                if filename is not None:
                    try:
                        basename = os.path.basename(os.fspath(filename))
                        result += f" file={basename}"
                    except BaseException:
                        result += f" file=<{type(filename).__name__}>"
            return result
        if isinstance(text, Enum):
            return text.value
        if isinstance(text, str):
            return f"{len(text)} chars, {len(text.split())} words"
        if isinstance(text, dict):
            metadata = {"mode", "state", "process", "target_process", "exe", "expected_exe", "actual_exe",
                        "phrase", "reason", "pack", "source", "kind", "lane", "origin",
                        "stage", "where", "reverted_to", "commit_outcome"}
            # Plugin refusals can put a spoken app/destination in reason.
            reasons = {"stale", "pack_disabled", "dispatch_unresolved", "unknown_command", "out_of_scope",
                       "not_allowed_for_model", "unvalidated", "empty", "unavailable", "invalid_selection",
                       "not_dictating", "no_word", "no_draft", "not_found", "draft_changed", "draft_pending",
                       "focus_changed", "session changed", "no_handler", "unknown_type", "cancelled",
                       "user_speech_interrupt", "tts_complete", "cancel_speech", "refused", "applied",
                       "focus moved", "the window changed", "nothing to edit", "nothing to apply", "no change"}
            return {key: value if key in metadata and isinstance(value, str) and not isinstance(value, Enum)
                    and (key != "reason" or value in reasons)
                    else describe_text(value) for key, value in text.items()}
        if isinstance(text, (list, tuple)):
            return [describe_text(value) for value in text]
        if text is None or isinstance(text, (bool, int, float)):
            return text
        return f"<{type(text).__name__}>"
    except BaseException:
        return f"<{type(text).__name__}>"


def safe_exception(logger, msg, *args, level=logging.ERROR, exc_info=None):
    """Retain exception types and frames without private exception messages.

    A detached exception carries only safe metadata; both the ordinary file
    formatter and other handlers (including capture handlers) see that copy.
    No original exception, traceback frame, or handler is changed.
    """
    try:
        if not logger.isEnabledFor(level):
            return
    except BaseException:
        return
    info = sys.exc_info() if exc_info is None else exc_info
    seen = set()
    def sanitized(error):
        if error is None or id(error) in seen:
            return None
        seen.add(id(error))
        base = BaseExceptionGroup if isinstance(error, BaseExceptionGroup) else BaseException
        proxy_type = type(type(error).__name__, (base,),
                          {"__module__": type(error).__module__})
        summary = describe_text(error).split(": ", 1)[1]
        proxy = (proxy_type(summary, [sanitized(child) for child in error.exceptions])
                 if isinstance(error, BaseExceptionGroup) else proxy_type(summary))
        proxy.__traceback__ = error.__traceback__
        proxy.__cause__ = sanitized(error.__cause__)
        proxy.__context__ = sanitized(error.__context__)
        proxy.__suppress_context__ = error.__suppress_context__
        if getattr(error, "__notes__", None):
            proxy.add_note("notes=" + str(describe_text(error.__notes__)))
        seen.remove(id(error))
        return proxy
    try:
        error = sanitized(info[1])
        safe_info = (type(error), error, info[2]) if error is not None else None
        logger.log(level, msg, *args, exc_info=safe_info, stacklevel=2)
    except BaseException:
        # Logging must never replace the failure or prevent recovery. Preserve
        # its type and frames even if a custom exception rejects inspection.
        try:
            proxy = type(type(info[1]).__name__, (BaseException,), {})("<metadata unavailable>")
            logger.log(level, msg, *args, exc_info=(type(proxy), proxy, info[2]), stacklevel=2)
        except BaseException:
            pass


def _fallback_configure() -> None:
    """Attach the same rotating-file + console handlers dictation.py sets
    up, but ONLY if the root logger has no handlers yet (i.e. dictation.py's
    own bootstrap hasn't run in this process) -- never double-attach."""
    global _configured
    if _configured:
        return
    with _configure_lock:
        if _configured:
            return
        root = logging.getLogger()
        if root.handlers:
            # dictation.py (or an earlier get_logger call) already configured
            # the root logger -- reuse it.
            _configured = True
            return

        log_file = SAMSARA_LOG_FILE
        log_file.parent.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        setattr(file_handler, SAMSARA_LOG_HANDLER_TAG, True)
        root.addHandler(file_handler)

        # Console handler only when a console actually exists (frozen
        # windowed builds have sys.stdout is None) -- matches dictation.py's
        # own "not frozen" console-mirroring rule.
        if sys.stdout is not None and hasattr(sys.stdout, "fileno"):
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
            setattr(console_handler, SAMSARA_LOG_HANDLER_TAG, True)
            root.addHandler(console_handler)

        root.setLevel(logging.DEBUG)
        _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the "Samsara" hierarchy, bound to the shared
    rotating-file handler config. Pass __name__ (or a short tag) -- module
    names outside the "Samsara" tree are nested under it so third-party
    noise suppression (PIL/httpx/urllib3/comtypes, set in dictation.py) and
    any future "Samsara".setLevel() call still apply uniformly."""
    _fallback_configure()
    if name == "Samsara" or name.startswith("Samsara."):
        return logging.getLogger(name)
    return logging.getLogger(f"Samsara.{name}")
