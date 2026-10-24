"""Small explicit host seams; UI work stays on Samsara's existing Qt loop."""
from collections import OrderedDict
from hashlib import sha256
import threading
import weakref
from .capture import Failure, failure, snapshot

_active = None
_lock = threading.RLock()


def _key(label, kind):
    return sha256((str(kind) + "\0" + str(label)).encode("utf-8")).digest()


def install(app, log_dir=None):
    global _active
    _active = weakref.ref(app)
    if log_dir is not None:
        app._bug_report_log_dir = log_dir
    indicator = getattr(app, "listening_indicator", None)
    if indicator is not None:
        indicator._report_problem = lambda label, kind: report_problem(app, label, kind)


def remember(app, label, kind, source="", detail=None):
    """Retain only safe context, keyed by a digest rather than private UI text."""
    if kind not in ("error", "warning"):
        return
    with _lock:
        cache = getattr(app, "_bug_report_failures", None)
        if cache is None:
            cache = app._bug_report_failures = OrderedDict()
        key = _key(label, kind)
        if source or key not in cache:
            cache[key] = failure(label, kind, source, detail)
        cache.move_to_end(key)
        while len(cache) > 64:
            cache.popitem(last=False)
        app._bug_report_latest = cache[key]
    indicator = getattr(app, "listening_indicator", None)
    if indicator is not None:
        indicator._report_problem = lambda label, kind: report_problem(app, label, kind)


def context_for(app, label, kind):
    with _lock:
        return getattr(app, "_bug_report_failures", {}).get(_key(label, kind)) or failure(label, kind)


def open_preview(app, report):
    from .preview_qt import ReportPreview
    # Keep it alive without blocking the event loop or opening a nested loop.
    window = ReportPreview(report)
    windows = getattr(app, "_bug_report_windows", None)
    if windows is None:
        windows = app._bug_report_windows = []
    windows.append(window)
    window.destroyed.connect(lambda: windows.remove(window) if window in windows else None)
    window.show()
    window.raise_()
    window.activateWindow()
    return window


def report_problem(app, label="", kind="error", *, failed=None):
    return open_preview(app, snapshot(app, failed or context_for(app, label, kind)))


def report_notice(app, notice):
    if notice is None:
        return
    from samsara.ui.home_signals import DICTATION, HANDS_FREE, AVA
    capability = notice.capability if notice.capability in (DICTATION, HANDS_FREE, AVA) else "Capability"
    failed = Failure("notice", "unavailable", capability + " unavailable.")
    app._bug_report_latest = failed
    return report_problem(app, failed=failed)


def report_latest():
    """Command lane entry: marshal both visible-state lookup and capture to Qt."""
    app = _active() if _active is not None else None
    if app is None:
        return False
    from samsara.ui import qt_runtime

    def show():
        indicator = getattr(app, "listening_indicator", None)
        # Test type before access: Full's legacy adapter supplies a no-op __getattr__.
        if callable(getattr(type(indicator), "_active_chip", None)):
            chip = indicator._active_chip()
            if chip and chip[1] in ("warning", "error"):
                report_problem(app, *chip)
                return
        controller = getattr(indicator, "controller", None)
        if controller is not None:
            from samsara.live_surface.feedback import FeedbackKind
            view = controller.view()
            for notice in (view.notice, view.secondary_notice):
                if notice and notice.feedback in (FeedbackKind.ERROR, FeedbackKind.WARNING):
                    report_problem(app, notice.message, notice.feedback.value)
                    return
        from samsara.ui.home_signals import problem_notice
        notice = problem_notice(app)
        if notice is not None:
            report_notice(app, notice)
            return
        failed = getattr(app, "_bug_report_latest", None)
        report_problem(app, failed=failed or Failure())

    qt_runtime.post(show)
    return True
