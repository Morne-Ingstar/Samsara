"""Transient PC clock facts for Ava; never retained in conversation memory."""

from datetime import datetime


_CLOCK_QUESTIONS = {
    "time": {"what time is it", "what's the time", "what is the time",
             "what time is it now", "what's the current time", "tell me the time"},
    "date": {"what's the date", "what is the date", "what day is it",
             "what's today's date", "what is today's date", "what is the date today",
             "what's the date today", "what day is it today", "what date is it",
             "tell me today's date", "tell me the date"},
}


def clock_answer(utterance, clock=None):
    """Answer only whole local clock questions, using PC regional formats."""
    phrase = " ".join(utterance.casefold().replace("’", "'").split()).strip(" .?!")
    kind = next((kind for kind, phrases in _CLOCK_QUESTIONS.items() if phrase in phrases), None)
    if kind is None:
        return None
    from PySide6.QtCore import QDate, QLocale, QTime

    now = datetime.now().astimezone() if clock is None else clock()
    locale = QLocale.system()
    value = (locale.toString(QTime(now.hour, now.minute), QLocale.ShortFormat) if kind == "time"
             else locale.toString(QDate(now.year, now.month, now.day), QLocale.LongFormat))
    value = " ".join(value.split())  # Keep locale spacing readable by the speech engine.
    return f"It's {value}."  # STAND-IN COPY (590): owner may replace the framing.


def handle_clock_intent(app, utterance, speak):
    answer = clock_answer(utterance)
    if answer is None:
        return False
    speak(app, answer)
    return True


def local_clock_line(clock=None):
    """Read local time for this send; tests may inject an aware local clock."""
    now = datetime.now().astimezone() if clock is None else clock()
    offset = now.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    return (f"Local clock facts (PC, not user text): {now:%A}, {now:%Y-%m-%d}; "
            f"{now:%H:%M}; {now.tzname()} (UTC{offset}).")
