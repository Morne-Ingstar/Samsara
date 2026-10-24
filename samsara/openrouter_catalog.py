"""Lazy public OpenRouter catalogue. Importing this module does no I/O.

No API key, account configuration or authentication header is accepted here.
The shared transport fixes the host and refuses redirects/environment proxies.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import threading

from samsara import ai_transport

URL = "https://openrouter.ai/api/v1/models"
MAX_ENTRIES = 2000
MAX_BYTES = 4 * 1024 * 1024
DAY = 86400
_load_lock = threading.Lock()
_uncached_attempts = {}


def _price(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 <= number <= 1000:
            return str(number)
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def _text(value, limit):
    return value if isinstance(value, str) and 0 < len(value) <= limit and not any(
        ord(char) < 32 for char in value) else ""


def parse_models(payload):
    """Keep bounded metadata only, tolerating malformed individual entries."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("invalid catalogue")
    models, seen = [], set()
    for row in rows[:MAX_ENTRIES * 3]:
        if not isinstance(row, dict):
            continue
        model_id = _text(row.get("id"), 256)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        pricing = row.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        context = row.get("context_length")
        if type(context) is not int or not 0 < context <= 100_000_000:
            context = None
        parameters = row.get("supported_parameters")
        parameters = parameters if isinstance(parameters, list) else []
        expiration = row.get("expiration_date")
        try:
            expiration = date.fromisoformat(expiration).isoformat()
        except (ValueError, TypeError):
            expiration = None
        architecture = row.get("architecture")
        architecture = architecture if isinstance(architecture, dict) else {}
        models.append({"id": model_id, "name": _text(row.get("name"), 256) or model_id,
                       "architecture": {key: [v for v in values[:16] if isinstance(v, str) and v in {"text", "image", "audio", "video"}]
                                        for key in ("input_modalities", "output_modalities")
                                        for values in [architecture.get(key)]
                                        if isinstance(values, list)},
                       "pricing": {key: _price(pricing.get(key)) for key in ("prompt", "completion")},
                       "context_length": context, "expiration_date": expiration,
                       "supported_parameters": [_text(p, 64) for p in parameters[:128] if _text(p, 64)]})
        if len(models) == MAX_ENTRIES:
            break
    return tuple(models)


def matches(model, search="", free_only=False, capabilities=False):
    model_id = model["id"]
    if ":" in model_id and not model_id.endswith(":free"):
        return False
    output_modalities = model.get("architecture", {}).get("output_modalities")
    chat = output_modalities is None or output_modalities == ["text"]
    if output_modalities is None and re.search(
            r"lyria|(?:image|audio)[ -]?(?:generation|generator)|text-to-(?:image|audio|video)",
            model.get("name", "") + " " + model_id, re.IGNORECASE):
        # Older bundled snapshots predate the output-modality field.
        chat = False
    pricing = model["pricing"]
    free = all(pricing[key] is not None and Decimal(pricing[key]) == 0
               for key in ("prompt", "completion"))
    params = set(model["supported_parameters"])
    capable = "tools" in params and bool(params & {"response_format", "structured_outputs"})
    return (chat and search.casefold() in (model["name"] + " " + model["id"]).casefold()
            and (not free_only or free) and (not capabilities or capable))


@dataclass(frozen=True)
class Catalogue:
    models: tuple = ()
    fetched_at: float = 0
    attempted_at: float = 0
    reason: str = ""

    def age_hours(self, now=None):
        return max(0, ((time.time() if now is None else now) - self.fetched_at) / 3600)


def _timestamp(value, now):
    return value if type(value) in (int, float) and 0 < value <= now and math.isfinite(value) else 0


def read_cache(path, now):
    try:
        with Path(path).open("rb") as file:
            raw = file.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize cache")
        saved = json.loads(raw)
        models = parse_models(saved)
        # Reasons are codes, never remote error bodies or local paths.
        reason = saved.get("reason", "")
        return Catalogue(models, _timestamp(saved.get("fetched_at"), now),
                         _timestamp(saved.get("attempted_at"), now),
                         reason if reason in {"offline", "invalid", "cache"} else "")
    except (OSError, ValueError, TypeError, RecursionError):
        return Catalogue()


def _write_cache(path, result):
    raw = json.dumps({"data": result.models, "fetched_at": result.fetched_at,
                      "attempted_at": result.attempted_at, "reason": result.reason}).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("oversize cache")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = file.name
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_catalogue(path, *, refresh=False, now=None):
    """Call only from the picker worker; one automatic attempt per day/profile.

    Refresh bypasses the daily limit. Failed refreshes preserve stale models.
    """
    # Two Settings windows opening together must not make two daily requests.
    with _load_lock:
        return _load_catalogue(path, refresh=refresh, now=now)


def _load_catalogue(path, *, refresh, now):
    now = time.time() if now is None else now
    cached = read_cache(path, now)
    key = str(Path(path).absolute())
    recent = _uncached_attempts.get(key)
    if recent and cached.attempted_at < recent.attempted_at <= now:
        cached = recent
    if not refresh and cached.attempted_at and now - cached.attempted_at < DAY:
        return cached
    response = None
    try:
        # Intentionally no headers: this public GET sends no owner key.
        response = ai_transport.get_cloud(URL, timeout=8)
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            raise ValueError("oversize catalogue")
        models = parse_models(response.json())
        result = Catalogue(models, now, now)
    except (ValueError, RecursionError):
        result = Catalogue(cached.models, cached.fetched_at, now, "invalid")
    except (OSError, ai_transport.requests.exceptions.RequestException):
        result = Catalogue(cached.models, cached.fetched_at, now, "offline")
    finally:
        if response is not None:
            response.close()
    try:
        _write_cache(path, result)
    except (OSError, ValueError):
        result = Catalogue(result.models, result.fetched_at, result.attempted_at, result.reason or "cache")
        # A read-only/full disk must not turn each picker opening into a request.
        if len(_uncached_attempts) >= 32:
            _uncached_attempts.pop(next(iter(_uncached_attempts)))
        _uncached_attempts[key] = result
    else:
        _uncached_attempts.pop(key, None)
    return result
