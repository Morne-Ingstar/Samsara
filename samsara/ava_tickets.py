"""Explicit, read-only Cockpit ticket recall. Ticket fields are data, never prompts."""
# PLACEHOLDER COPY (977): all spoken/visible ticket replies below.
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
import json
import re
import time
import unicodedata

from samsara.spoken_numbers import parse_spoken_number

_PORT = 8790
_TIMEOUT = 1.0
_MAX_BYTES = 2 * 1024 * 1024
_STATUSES = {"working", "waiting", "parked", "done"}


@dataclass(frozen=True)
class Answer:
    text: str
    kind: str = "success"


def request_id(utterance):
    """None means unrelated; empty string means ticket-shaped but unclear."""
    phrase = " ".join(utterance.casefold().replace("â€™", "'").split()).strip(" .?!")
    match = re.fullmatch(r"(?:resume tickets?|where were we on tickets?)(?:\s+(.*))?", phrase)
    if not match:
        return "" if phrase in {"resume that one", "where were we", "where were we on that one"} else None
    raw = (match.group(1) or "").strip()
    raw = re.sub(r"^(?:t(?=\s|[0-9])\s*|tee\s+)", "", raw)
    if len(raw) > 80:
        return ""
    number = parse_spoken_number(raw)
    return f"T{number}" if number is not None and number > 0 else ""


def _field(row, name, limit):
    value = row.get(name)
    if value is None or value == "":
        return "not recorded"
    if not isinstance(value, str):
        raise ValueError("invalid ticket field")
    value = " ".join("".join(" " if unicodedata.category(c).startswith("C") else c for c in value).split())
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0] + "â€¦"
    return value or "not recorded"


def _age(value, now):
    if not value:
        return "modification time not recorded"
    if not isinstance(value, str):
        raise ValueError("invalid updated field")
    try:
        updated = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            return "modification age unknown (time zone missing)"
        seconds = (now - updated).total_seconds()
    except (ValueError, OverflowError):
        return "modification age unknown (invalid timestamp)"
    if seconds < 0:
        return "modification age unknown (timestamp is in the future)"
    if seconds < 60:
        return "updated less than a minute ago"
    count, unit = (int(seconds // 86400), "day") if seconds >= 86400 else (
        (int(seconds // 3600), "hour") if seconds >= 3600 else (int(seconds // 60), "minute"))
    age = f"updated {count} {unit}{'s' if count != 1 else ''} ago"
    return age + ("; this record may be stale" if seconds >= 7 * 86400 else "")


def summarize(document, ticket_id, *, now=None):
    if (not isinstance(document, dict) or type(document.get("rev")) is not int
            or document["rev"] < 0 or not isinstance(document.get("tickets"), list)):
        raise ValueError("invalid response envelope")
    rows = document["tickets"]
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
           or not re.fullmatch(r"T[1-9][0-9]*", row["id"]) for row in rows):
        raise ValueError("invalid ticket identifiers")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("duplicate ticket identifiers")
    if not rows:
        return Answer("Cockpit returned no tickets. Its ticket store may be empty or missing.", "error")
    row = next((row for row in rows if row["id"] == ticket_id), None)
    if row is None:
        return Answer(f"I couldn't find ticket {ticket_id}. Say another ticket number.", "clarification")
    status = row.get("status")
    if status is not None and status not in _STATUSES:
        raise ValueError("invalid ticket status")
    text = (f"{ticket_id}: {_field(row, 'title', 100)}. Status: {status or 'not recorded'}. "
            f"Left off: {_field(row, 'left_off', 160)}. Next: {_field(row, 'next', 130)}. "
            f"Waiting on: {_field(row, 'waiting_on', 90)}. "
            f"{_age(row.get('updated'), now or datetime.now(timezone.utc))}.")
    return Answer(text)


def lookup(ticket_id):
    """One GET, direct IPv4 loopback, no proxies, redirects or attachment reads."""
    if not ticket_id:
        return Answer("Which ticket number? Say resume ticket T seven.", "clarification")
    if not re.fullmatch(r"T[1-9][0-9]*", ticket_id):
        return Answer("Which ticket number? Say resume ticket T seven.", "clarification")
    connection = HTTPConnection("127.0.0.1", _PORT, timeout=_TIMEOUT)
    try:
        connection.request("GET", "/api/tickets", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return Answer("I couldn't read Cockpit's tickets. Its local API returned an error.", "error")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("unexpected content type")
        # Bound body size and total body-read duration, including slow trickles.
        deadline = time.monotonic() + _TIMEOUT
        chunks, length = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            # HTTPResponse may own the socket after a Connection: close header.
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, _MAX_BYTES + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > _MAX_BYTES:
                raise ValueError("response too large")
        return summarize(json.loads(b"".join(chunks).decode("utf-8")), ticket_id)
    except TimeoutError:
        return Answer("Cockpit's ticket lookup timed out. Try again when Cockpit is responding.", "error")
    except (OSError, HTTPException):
        return Answer("I couldn't reach Cockpit's local ticket service. Check that Cockpit is running.", "error")
    except (ValueError, TypeError, RecursionError):
        return Answer("Cockpit returned invalid ticket data. I couldn't read that ticket.", "error")
    finally:
        connection.close()


def consume(app, remainder, generation, on_done):
    """Queue explicit local recall without provider memory or action parsing."""
    ticket_id = request_id(remainder) if remainder else None
    if ticket_id is None:
        return False
    from plugins.commands import ask_ollama as ava
    from samsara import ava_turn_queue

    def ticket_worker():
        ava._turn_local.speech = []
        ava._turn_local.spoken_text = []
        ava._turn_local.reply = None
        try:
            answer = lookup(ticket_id)
            with ava_turn_queue.ownership_lock(app):
                if not ava_turn_queue.can_deliver(app):
                    return
                ava._turn_local.literal_answer = True
                delivered = ava.speak_as(app, answer.text, source="SAMSARA")
                kind = ("success" if answer.kind == "success" else "warning") if delivered else "warning"
                ava._set_turn_outcome(app, ("Ticket record" if delivered else "Ticket answer not spoken", kind))
        finally:
            # Ticket contents must not enter conversation history or logs.
            ava._turn_local.speech = ava._turn_local.spoken_text = ava._turn_local.reply = None
            ava._turn_local.literal_answer = False
            on_done()
    ava_turn_queue.submit(app, ticket_worker, generation, on_done)
    return True
