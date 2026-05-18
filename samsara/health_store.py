"""Local persistent health log storage for Samsara.

Entries are stored in ~/.samsara/health_log.json.
All public functions are thread-safe.
Never leaves the machine — no cloud, no sync.

Entry types:
  - pain: {level: 1-10, location: str|None, note: str|None}
  - medication: {name: str, dose: str|None, note: str|None}
  - symptom: {text: str}

Each entry has: id, type, timestamp, data dict.
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta

_LOG_PATH = os.path.join(os.path.expanduser("~"), ".samsara", "health_log.json")
_lock = threading.Lock()
_data = {"entries": [], "next_id": 1}


def _load():
    global _data
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("health log root is not a dict")
        entries = loaded.get("entries", [])
        if not isinstance(entries, list):
            entries = []
        # Repair next_id if it's missing or invalid — derive from the highest
        # entry id so we never issue a duplicate.
        saved_next = loaded.get("next_id")
        max_id = max(
            (e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), int)),
            default=0,
        )
        if isinstance(saved_next, int) and saved_next > max_id:
            next_id = saved_next
        else:
            next_id = max_id + 1
        _data = {"entries": entries, "next_id": next_id}
    except FileNotFoundError:
        _data = {"entries": [], "next_id": 1}
    except Exception as e:
        print(f"[HEALTH] Could not load health log: {e}")
        _data = {"entries": [], "next_id": 1}


def _save():
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    tmp = _LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=2)
    os.replace(tmp, _LOG_PATH)


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def add_entry(entry_type: str, data: dict) -> dict:
    """Add a health log entry. Returns the created entry."""
    with _lock:
        entry = {
            "id": _data["next_id"],
            "type": entry_type,
            "timestamp": _now_iso(),
            "data": data,
        }
        _data["entries"].append(entry)
        _data["next_id"] += 1
        _save()
        return dict(entry)


def remove_entry(entry_id: int) -> bool:
    with _lock:
        before = len(_data["entries"])
        _data["entries"] = [e for e in _data["entries"] if e["id"] != entry_id]
        if len(_data["entries"]) < before:
            _save()
            return True
        return False


def get_all() -> list:
    with _lock:
        return [dict(e) for e in _data["entries"]]


def get_recent(hours: int = 24) -> list:
    """Return entries from the last N hours (exclusive: cutoff is not included)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_str = cutoff.isoformat().replace("+00:00", "Z")
    with _lock:
        return [dict(e) for e in _data["entries"] if e["timestamp"] > cutoff_str]


def get_by_type(entry_type: str, hours: int = None) -> list:
    """Return entries of a specific type, optionally filtered by recency."""
    with _lock:
        entries = [e for e in _data["entries"] if e["type"] == entry_type]
    if hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        cutoff_str = cutoff.isoformat().replace("+00:00", "Z")
        entries = [e for e in entries if e["timestamp"] > cutoff_str]
    return [dict(e) for e in entries]


def get_pain_average(hours: int = 24) -> float | None:
    """Average pain level over the last N hours."""
    pain_entries = get_by_type("pain", hours=hours)
    levels = [e["data"].get("level") for e in pain_entries if e["data"].get("level") is not None]
    if not levels:
        return None
    return round(sum(levels) / len(levels), 1)


def get_today() -> list:
    """Return all entries from today (local time)."""
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = today_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    with _lock:
        return [dict(e) for e in _data["entries"] if e["timestamp"] > today_utc]


def export_csv(filepath: str = None) -> str:
    """Export health log to CSV. Returns the file path."""
    import csv
    if filepath is None:
        filepath = os.path.join(os.path.expanduser("~"), ".samsara", "health_log_export.csv")
    entries = get_all()
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "type", "timestamp", "detail"])
        for e in entries:
            d = e["data"]
            if e["type"] == "pain":
                detail = f"Level {d.get('level', '?')}"
                if d.get("location"):
                    detail += f" ({d['location']})"
                if d.get("note"):
                    detail += f" - {d['note']}"
            elif e["type"] == "medication":
                detail = d.get("name", "unknown")
                if d.get("dose"):
                    detail += f" {d['dose']}"
                if d.get("note"):
                    detail += f" - {d['note']}"
            elif e["type"] == "symptom":
                detail = d.get("text", "")
            else:
                detail = json.dumps(d)
            writer.writerow([e["id"], e["type"], e["timestamp"], detail])
    return filepath


def clear_all() -> int:
    """Clear all entries. Returns count removed."""
    with _lock:
        count = len(_data["entries"])
        _data["entries"] = []
        _data["next_id"] = 1
        _save()
        return count


_load()
