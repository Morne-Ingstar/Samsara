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
import sys
import threading
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
