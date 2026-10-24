"""Carry the previous marker's evidence forward without rereading the new one."""
from .capture import Failure, snapshot
from .redact import redact_fault


class CrashCatch:
    def __init__(self, previous, fault_lines=()):
        self.pending = previous is not None
        self.fault = redact_fault(fault_lines)

    def take(self):
        if not self.pending:
            return None
        self.pending = False
        return self.fault  # Empty evidence still means an unclean previous run.


def offer(app, catch):
    evidence = catch.take()
    if evidence is None:
        return None
    from .coordinator import open_preview
    from .preview_qt import CrashOffer
    # Evidence is already allow-listed; snapshot accepts the same canonical lines.
    def report():
        body = snapshot(app, Failure("crash", "unavailable", "Previous session ended without a clean exit."))
        body += "\n\nPrevious-session fault (unrecognized frames omitted):\n" + ("\n".join(evidence) or "No allow-listed fault lines.")
        open_preview(app, body)
    app._bug_report_crash_offer = CrashOffer(report)
    app._bug_report_crash_offer.show()
    return app._bug_report_crash_offer
