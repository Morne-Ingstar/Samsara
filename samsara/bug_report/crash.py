"""Carry the previous marker's evidence forward without rereading the new one."""
from .capture import Failure, snapshot
from .redact import redact_fault


class CrashCatch:
    def __init__(self, previous, fault_lines=()):
        self.fault = redact_fault(fault_lines)
        from .session_exit import classify
        self.kind, self.explanation = classify(previous, self.fault)
        self.pending = self.kind == "crash"

    def take(self):
        if not self.pending:
            return None
        self.pending = False
        return self.fault  # Only a positively recorded fault reaches this point.


def offer(app, catch):
    app._bug_report_previous_exit = catch.explanation or "clean exit"
    evidence = catch.take()
    if evidence is None:
        return None
    from .coordinator import open_preview
    from .preview_qt import CrashOffer
    # Evidence is already scrubbed; snapshot accepts the same canonical lines.
    def report():
        body = snapshot(app, Failure("crash", "unavailable", "Previous session recorded a fault."), fault=evidence)
        open_preview(app, body)
    app._bug_report_crash_offer = CrashOffer(report)
    app._bug_report_crash_offer.show()
    return app._bug_report_crash_offer
