"""Small, fixed-vocabulary diagnostics for application exit requests."""

import inspect
import json
import logging
import traceback
from pathlib import Path

_KNOWN = {
    "_restart_samsara": "restart",
    "quit_after_install": "update",
    "_show_startup_error": "startup-error",
}
_REASONS = {"user-quit", "restart", "update", "windows-logoff", "startup-error", "unknown"}


def identify_exit(*, reason=None, logger=None):
    """Return a safe reason and source for quit_app's immediate caller."""
    log = logger or logging.getLogger("Samsara")
    try:
        frame = inspect.currentframe().f_back
        module = frame.f_globals.get("__name__", "unknown")
        function = frame.f_code.co_name
        source = f"{module}:{function}"
        inferred = _KNOWN.get(function)
        if inferred is None and (module.startswith("samsara.ui.tray_qt")
                                 or module.startswith("samsara.ui.home_qt")
                                 or module.startswith("samsara.ui.settings_qt")
                                 or module.startswith("plugins.commands")):
            inferred = "user-quit"
        if inferred is None and module.startswith("samsara.ui.update_qt"):
            inferred = "update"
        code = reason if reason in _REASONS else inferred or "unknown"
        if code == "unknown":
            log.warning("[EXIT] Unknown shutdown request stack:\n%s",
                        "".join(traceback.format_stack(frame, limit=12)))
        return code, source
    except Exception:
        log.warning("[EXIT] Unknown shutdown request stack unavailable")
        return "unknown", "unknown"


def record_exit(path, reason):
    """Persist only fixed exit metadata; never include user dictated text."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"reason": str(reason)[:64]}, ensure_ascii=True),
                          encoding="utf-8")
    except Exception:
        logging.getLogger("Samsara").debug("[EXIT] Could not persist exit reason")


def finish_exit(path, reason, *, logger=None):
    """Persist and emit the reason before the generic hard-exit breadcrumb."""
    record_exit(path, reason)
    from samsara import process_exit
    process_exit.trace_exit(reason, 0, log=logger)
    process_exit.hard_exit(0)


def read_previous_exit(path, *, logger=None):
    """Log and consume the preceding run's exit marker without user nagging."""
    log = logger or logging.getLogger("Samsara")
    try:
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        target.unlink(missing_ok=True)
        reason = data.get("reason", "unknown")
        if reason not in _REASONS:
            reason = "unknown"
        if reason not in {"user-quit", "restart", "update", "windows-logoff"}:
            log.warning("previous exit was unexpected: %s", reason)
        return reason
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("previous exit was unexpected: unknown")
        return "unknown"
