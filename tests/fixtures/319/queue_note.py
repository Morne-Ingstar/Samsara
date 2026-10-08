"""Synthetic queue_note interface fixture, authored 2026-10-08 for 408.

Interface derived from samsara/agent_channel/queue_voice.py and the 319 tests.
This is not the owner's queue parser; only the test's simple prompt headers,
report statuses and ownership paths are represented. No queue-note generation.
"""
from pathlib import Path
import re

QUEUE = Path(__file__).parent / "queue"
REPORTS = Path(__file__).parent / "reports"
_STATUS_LINE = re.compile(r"^STATUS:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


def label(path):
    return path.name.split("_", 1)[0]


def parse(path):
    text = path.read_text(encoding="utf-8")
    headers = dict(re.findall(r"^([A-Z]+):[ \t]*([^\n]*)$", text, re.MULTILINE))
    title = next((line.lstrip("# ") for line in text.splitlines() if line.startswith("# ")), "")
    return headers, title


def owned(headers):
    return set(re.findall(r"[\w./\\-]+\.[\w]+", headers.get("OWNS", "")))


def stated_ids(headers):
    return set(re.findall(r"\b\d{2,3}[a-z]?\b", headers.get("AFTER", ""), re.IGNORECASE))


def _overlap(left, right):
    def normalize(path):
        return path.replace("\\", "/").rstrip("/").lower()
    return any(normalize(a) == normalize(b) or normalize(a).startswith(normalize(b) + "/")
               or normalize(b).startswith(normalize(a) + "/") for a in left for b in right)


def report_state(pid, prompt=None):
    final = REPORTS / pid / "FINAL.md"
    if not final.is_file():
        return None, "no report"
    match = _STATUS_LINE.search(final.read_text(encoding="utf-8"))
    if match and "BLOCKED" in match.group(1).upper():
        return "blocked", "blocked report"
    return "reported", "report exists"


def _redispatch_reason(path, pid):
    return None


if __name__ == "__main__":
    print("queue note refreshed")

