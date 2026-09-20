"""Small, explicit Todoist API v1 client; never called without an owner token."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

QUICK_ADD_URL = "https://api.todoist.com/api/v1/tasks/quick"
FILTER_URL = "https://api.todoist.com/api/v1/tasks/filter"
TIMEOUT_SECONDS = 10


def _request(token: str, url: str, *, data: bytes | None = None) -> object:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def quick_add(token: str, text: str) -> str:
    payload = json.dumps({"text": text}).encode("utf-8")
    result = _request(token, QUICK_ADD_URL, data=payload)
    task_id = (result or {}).get("id") if isinstance(result, dict) else None
    if task_id is None:
        raise RuntimeError("Todoist returned no task id")
    return str(task_id)


def today(token: str) -> list[str]:
    url = FILTER_URL + "?" + urllib.parse.urlencode({"query": "today"})
    result = _request(token, url)
    items = result.get("results", []) if isinstance(result, dict) else []
    return [str(item.get("content", "")).strip() for item in items if isinstance(item, dict) and item.get("content")]
