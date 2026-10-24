"""Build a browser-only new-issue URL. This module never sends a request."""
from dataclasses import dataclass
from urllib.parse import urlencode, quote

ISSUE_URL = "https://github.com/Morne-Ingstar/Samsara/issues/new"
MAX_URL_BYTES = 2000  # Conservative browser handoff budget, measured after escaping.
PASTE_NOTICE = "Report copied — paste it into the issue."


@dataclass(frozen=True)
class IssueDraft:
    url: str
    clipboard: str | None = None


def issue_draft(body):
    title = "[Bug]: Samsara problem report"
    url = ISSUE_URL + "?" + urlencode({"title": title, "body": body}, quote_via=quote)
    if len(url.encode("utf-8")) <= MAX_URL_BYTES:
        return IssueDraft(url)
    return IssueDraft(ISSUE_URL + "?" + urlencode({"title": title}, quote_via=quote), body)
