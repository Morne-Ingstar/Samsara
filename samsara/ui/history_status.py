"""Pure History footer formatting, shared with its status restoration."""

SCOPE_LAST_7_DAYS = "Last 7 days"


def summary_text(count: int, scope: str, query: str, has_more: bool) -> str:
    """The line under the list: how many entries, and over what range."""
    more = "+" if has_more else ""
    if query:
        noun = "match" if count == 1 and not has_more else "matches"
        return f"{count}{more} {noun} for \u201c{query}\u201d in all history"
    noun = "entry" if count == 1 and not has_more else "entries"
    where = "in the last 7 days" if scope == SCOPE_LAST_7_DAYS else "in all history"
    return f"{count}{more} {noun} {where}"
