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
_STATUSES = {"working", "waiting", "parked", "done", "prep_audit", "landed", "in_daily", "reopened"}


@dataclass(frozen=True)
class Answer:
    text: str
    kind: str = "success"
    reason: str = "ok"
    http_status: int | None = None


class _InvalidSchema(ValueError):
    def __init__(self, detail, message):
        super().__init__(message)
        self.detail = detail


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
        raise _InvalidSchema("invalid_ticket_field", "invalid ticket field")
    value = " ".join("".join(" " if unicodedata.category(c).startswith("C") else c for c in value).split())
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0] + "â€¦"
    return value or "not recorded"


def _age(value, now):
    if not value:
        return "modification time not recorded"
    if not isinstance(value, str):
        raise _InvalidSchema("invalid_updated_field", "invalid updated field")
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
    return _summarize(document, ticket_id, now=now)[0]


def _summarize(document, ticket_id, *, now=None):
    if (not isinstance(document, dict) or type(document.get("rev")) is not int
            or document["rev"] < 0 or not isinstance(document.get("tickets"), list)):
        raise _InvalidSchema("invalid_envelope", "invalid response envelope")
    rows = document["tickets"]
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
           or not re.fullmatch(r"T[1-9][0-9]*", row["id"]) for row in rows):
        raise _InvalidSchema("invalid_ticket_identifiers", "invalid ticket identifiers")
    if len({row["id"] for row in rows}) != len(rows):
        raise _InvalidSchema("duplicate_ticket_identifiers", "duplicate ticket identifiers")
    if not rows:
        return Answer("Cockpit returned no tickets. Its ticket store may be empty or missing.",
                      "error", reason="missing_id"), "empty_store"
    row = next((row for row in rows if row["id"] == ticket_id), None)
    if row is None:
        return Answer(f"I couldn't find ticket {ticket_id}. Say another ticket number.",
                      "clarification", reason="missing_id"), "id_not_found"
    status = row.get("status")
    if status is not None and (not isinstance(status, str) or status not in _STATUSES):
        raise _InvalidSchema("invalid_ticket_status", "invalid ticket status")
    text = (f"{ticket_id}: {_field(row, 'title', 100)}. Status: {status or 'not recorded'}. "
            f"Left off: {_field(row, 'left_off', 160)}. Next: {_field(row, 'next', 130)}. "
            f"Waiting on: {_field(row, 'waiting_on', 90)}. "
            f"{_age(row.get('updated'), now or datetime.now(timezone.utc))}.")
    return Answer(text), "ok"


def lookup(ticket_id):
    """One GET, direct IPv4 loopback, no proxies, redirects or attachment reads."""
    return _lookup(ticket_id)[0]


def diagnose(ticket_id) -> dict:
    """Run the same lookup and return only closed codes and scalar metrics."""
    return _lookup(ticket_id if isinstance(ticket_id, str) else "")[1]


def _lookup(ticket_id) -> tuple[Answer, dict]:
    started = time.monotonic()
    http_status = body_bytes = ticket_count = None

    def result(answer, detail):
        return answer, dict(
            reason=answer.reason,
            detail=detail,
            http_status=http_status if http_status is not None and 100 <= http_status <= 599 else None,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            body_bytes=body_bytes,
            ticket_count=ticket_count,
        )

    if not ticket_id or not re.fullmatch(r"T[1-9][0-9]*", ticket_id):
        return result(Answer("Which ticket number? Say resume ticket T seven.",
                             "clarification", reason="invalid_id"), "bad_request_id")
    connection = HTTPConnection("127.0.0.1", _PORT, timeout=_TIMEOUT)
    try:
        connection.request("GET", "/api/tickets", headers={"Accept": "application/json"})
        response = connection.getresponse()
        http_status = response.status
        if http_status != 200:
            return result(Answer("I couldn't read Cockpit's tickets. Its local API returned an error.",
                                 "error", reason="http_status", http_status=http_status), "non_200")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise _InvalidSchema("unexpected_content_type", "unexpected content type")
        # Bound body size and total body-read duration, including slow trickles.
        deadline = time.monotonic() + _TIMEOUT
        chunks, body_bytes = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            # HTTPResponse may own the socket after a Connection: close header.
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, _MAX_BYTES + 1 - body_bytes))
            if not chunk:
                break
            chunks.append(chunk)
            body_bytes += len(chunk)
            if body_bytes > _MAX_BYTES:
                raise _InvalidSchema("response_too_large", "response too large")
        try:
            document = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, RecursionError):
            raise _InvalidSchema("invalid_json", "invalid JSON") from None
        if isinstance(document, dict) and isinstance(document.get("tickets"), list):
            ticket_count = len(document["tickets"])
        return result(*_summarize(document, ticket_id))
    except TimeoutError:
        detail = "response_headers_timeout" if http_status is None else "response_body_timeout"
        return result(Answer("Cockpit's ticket lookup timed out. Try again when Cockpit is responding.",
                             "error", reason="timeout"), detail)
    except (OSError, HTTPException) as error:
        if isinstance(error, ConnectionRefusedError):
            detail = "connection_refused"
        elif isinstance(error, HTTPException):
            detail = "http_protocol_error"
        else:
            detail = "os_error"
        return result(Answer("I couldn't reach Cockpit's local ticket service. Check that Cockpit is running.",
                             "error", reason="api_unavailable"), detail)
    except (ValueError, TypeError, RecursionError) as error:
        detail = error.detail if isinstance(error, _InvalidSchema) else "invalid_envelope"
        return result(Answer("Cockpit returned invalid ticket data. I couldn't read that ticket.",
                             "error", reason="schema_error"), detail)
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
