"""Paced SendInput dispatch for clipboard.type_text_unicode (1166s, T84 P1).

Every SendInput call carries exactly one complete key down/up pair -- one
UTF-16 code unit, or one Enter press -- and calls are never closer together
than TYPE_MIN_PAIR_INTERVAL_S. An astral character (emoji etc.) is TWO UTF-16
units, so it takes two calls in surrogate order. The spacing is a bounded
default to test against real targets, not proof that any particular editor
accepts every event.

clipboard.py keeps the INPUT structures (and their x64/x86 ABI check), the
focus capture and the settle; this module holds the receipt, the pacing rule
and the per-pair loop. The loop takes its clock, focus check, logger and error
reporter from the caller, so clipboard.py's names stay the ones that are used.
"""

TYPE_MIN_PAIR_INTERVAL_S = 0.005
# A larger explicit request (the .05 typed-target pacing) is kept; an
# infinite or absurd one is capped so it can never hang delivery.
TYPE_MAX_PAIR_INTERVAL_S = 0.25
TYPE_EVENTS_PER_CALL = 2
TYPE_SETTLE_S = 0.03


def unicode_pair_interval(key_interval) -> float:
    """Validated spacing between SendInput calls (seconds), never below the minimum."""
    try:
        value = float(key_interval)
    except (TypeError, ValueError, OverflowError):
        return TYPE_MIN_PAIR_INTERVAL_S
    if value != value:  # NaN
        return TYPE_MIN_PAIR_INTERVAL_S
    return min(max(value, TYPE_MIN_PAIR_INTERVAL_S), TYPE_MAX_PAIR_INTERVAL_S)


class TypedDispatch:
    """Receipt for one type_text_unicode call; truthy only when complete.

    ``accepted``/``total`` count INPUT events (two per UTF-16 unit or Enter).
    Acceptance means Windows inserted the events into the input stream; it is
    dispatch, never proof that the target inserted the text. ``state``:
    'complete' - every event accepted; 'none' - definitely nothing injected;
    'partial' - a prefix was accepted, or a call failed with an unknown result.
    A partial dispatch must not be followed by a whole-text paste or retype.
    """
    __slots__ = ('accepted', 'total', 'reason', 'unknown')

    def __init__(self, accepted, total, reason=None, unknown=False):
        self.accepted, self.total = accepted, total
        self.reason, self.unknown = reason, unknown

    @property
    def state(self):
        if self.reason is None and self.accepted == self.total:
            return 'complete'
        return 'partial' if self.accepted or self.unknown else 'none'

    def __bool__(self):
        return self.state == 'complete'

    def __repr__(self):
        return (f'TypedDispatch(state={self.state!r}, accepted={self.accepted}, '
                f'total={self.total}, reason={self.reason!r})')


def _wait_until(clock, deadline):
    # A few bounded sleeps: time.sleep may return early on some timers.
    for _ in range(4):
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            return
        clock.sleep(remaining)


def dispatch_pairs(send, inputs, input_type, size, *, interval, target,
                   target_matches, clock, logger, log_error):
    """Send ``inputs`` one down/up pair per call; return a TypedDispatch.

    ``send`` is user32.SendInput. Before every pair the focus captured by the
    caller (``target``) is rechecked; a change stops the loop. A short count
    stops it too, and a raising call is reported as an unknown (possibly
    partial) dispatch, never as nothing sent. Never raises, never retries.
    """
    total = len(inputs)
    accepted = 0
    in_flight = False
    last = None
    try:
        for start in range(0, total, TYPE_EVENTS_PER_CALL):
            if last is not None:
                _wait_until(clock, last + interval)
            # The send boundary: the window/control that had focus at entry.
            if not target_matches(target):
                logger.warning(
                    "[TYPE] Focus changed after %d/%d events; typed injection stopped",
                    accepted, total,
                )
                return TypedDispatch(accepted, total, 'target_changed')
            pair = inputs[start:start + TYPE_EVENTS_PER_CALL]
            last = clock.monotonic()
            in_flight = True
            sent = send(len(pair), (input_type * len(pair))(*pair), size)
            accepted += max(0, min(int(sent), len(pair)))
            in_flight = False
            if sent != len(pair):
                logger.warning(
                    "[TYPE] SendInput accepted %d/%d events; typed injection stopped",
                    accepted, total,
                )
                return TypedDispatch(accepted, total, 'short_count')
        return TypedDispatch(accepted, total)
    except Exception as e:
        log_error("type_text_unicode failed", e)
        return TypedDispatch(accepted, total, 'error', unknown=in_flight)
