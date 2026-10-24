"""Source labels for History rows and their detail cards."""


def _elide_plain(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def row_context(row: dict) -> str:
    """Agent commands name their channel, rather than the foreground app."""
    if row.get("entry_type") in ("command", "wake_command"):
        phrase = str(row.get("matched_command") or "").strip().casefold()
        agent = {"ask gpt": "GPT", "ask codex": "GPT", "ask claude": "Claude",
                 "ask hermes": "Hermes", "tell hermes": "Hermes"}.get(phrase)
        if agent:
            return agent
    context = str(row.get("app_context") or "").strip()
    if context and context.casefold() != "unknown":
        return "in " + context
    return context
