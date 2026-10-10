"""Transient PC clock facts for Ava; never retained in conversation memory."""

from datetime import datetime


def local_clock_line(clock=None):
    """Read local time for this send; tests may inject an aware local clock."""
    now = datetime.now().astimezone() if clock is None else clock()
    offset = now.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    return (f"Local clock facts (PC, not user text): {now:%A}, {now:%Y-%m-%d}; "
            f"{now:%H:%M}; {now.tzname()} (UTC{offset}).")
