"""Privacy policy for automatic Home previews and dictation History writes.

No control names, values, window titles or transcript logging. Unknown UIA
metadata is concealed. Process-list sharing detection is deliberately a cheap,
conservative hint, not an assertion that capture is (or is not) happening.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time


MEETING_PROCESSES = frozenset({
    "zoom.exe", "teams.exe", "ms-teams.exe", "discord.exe",
})
RECORDING_PROCESSES = frozenset({"obs.exe", "obs32.exe", "obs64.exe"})
_PROBE_SLOT = threading.BoundedSemaphore(1)


@dataclass(frozen=True)
class TargetPrivacy:
    reason: str = "unverified_target"
    app: str = ""

    @property
    def sensitive(self):
        return bool(self.reason)


def _read_target():
    import ctypes
    import psutil
    import uiautomation as auto
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not hwnd or not pid.value:
        return TargetPrivacy()
    process = psutil.Process(pid.value).name()
    # Executable identity only; document/window titles are never app names.
    with auto.UIAutomationInitializerInThread():
        control = auto.GetFocusedControl()
        if control is None or control.ProcessId != pid.value:
            return TargetPrivacy()
        password = control.IsPassword
        if user32.GetForegroundWindow() != hwnd:
            return TargetPrivacy()
        if password is True:
            return TargetPrivacy("password_field", process)
        if password is not False:
            return TargetPrivacy("unverified_target", process)
        return TargetPrivacy("", process)


def focused_target(timeout=.15):
    """Bound one UIA read; a hung provider never spawns additional probes."""
    if not _PROBE_SLOT.acquire(blocking=False):
        return TargetPrivacy()
    done, result = threading.Event(), []

    def read():
        try:
            result.append(_read_target())
        except Exception:
            result.append(TargetPrivacy())
        finally:
            _PROBE_SLOT.release()
            done.set()

    from samsara.runtime import thread_registry
    thread_registry.spawn("recent-privacy-uia", read, daemon=True)
    return result[0] if done.wait(timeout) else TargetPrivacy()


def show_recent_words(config):
    home = (config or {}).get("home")
    value = home.get("show_recent_words", True) if isinstance(home, dict) else (
        (config or {}).get("home.show_recent_words", True))
    return value is True


def sharing_detected(foreground=None, running=None):
    """OBS running or known meeting app foreground; Discord is conservative.

    Browser meetings, background streams and Windows Graphics Capture are not
    reliably covered. Never inspect titles or claim active-stream detection.
    """
    try:
        if foreground is None:
            from samsara.paste import foreground_process_name
            foreground = foreground_process_name()
        if running is None:
            import psutil
            running = (p.info.get("name", "") for p in psutil.process_iter(["name"]))
        return (str(foreground).casefold() in MEETING_PROCESSES or
                any(str(name).casefold() in RECORDING_PROCESSES for name in running))
    except Exception:
        # An unavailable process list cannot establish that sharing is absent.
        return True


def _target_for_history(app):
    previous = getattr(app, "_recent_delivery_privacy", None)
    current = focused_target()
    if previous and time.monotonic() - previous[0] < 5 and previous[1].sensitive:
        return previous[1]
    return current


def legacy_text(app, text, is_command=False):
    if not is_command and _target_for_history(app).sensitive:
        return ""  # Legacy history.json must not retain a second transcript.
    return text


def write_history(app, **fields):
    if fields.get("entry_type", "dictation") == "dictation":
        target = _target_for_history(app)
        fields.update(privacy_reason=target.reason, target_app=target.app)
        if target.sensitive:
            fields.update(raw_text="", display_text="", app_context="",
                          matched_command=None, audio_path=None)
    return app.history_db.add(**fields)


def before_delivery(app, text):
    """Recheck at injection, redacting History already written by hold mode.

    A receipt contains only metadata. No transcript is cached here. Failure to
    redact an existing row cancels injection rather than leaving a password in
    persisted History. The caller still owns its draft and delivery decision.
    """
    target = focused_target()
    app._recent_delivery_privacy = (time.monotonic(), target)
    if target.sensitive:
        manager = getattr(app, "history_db", None)
        if manager is not None:
            try:
                ids = manager.conceal_delivery(text, target.reason, target.app)
                concealed = getattr(app, "_recent_sensitive_ids", {})
                app._recent_sensitive_ids = {**concealed, **dict.fromkeys(ids, target.reason)}
            except Exception:
                return False
        history = getattr(app, "history", None)
        if isinstance(history, list):
            app.history[:] = [(stamp, "" if not command and str(saved).strip() == text.strip()
                               else saved, command) for stamp, saved, command in history]
            save = getattr(app, "save_history", None)
            if callable(save):
                try:
                    save()
                except Exception:
                    return False
        notify = getattr(app, "_notify_main_window", None)
        if callable(notify):
            try:
                notify("")  # Refresh cached Home/History labels without carrying speech.
            except Exception:
                pass
    return True
