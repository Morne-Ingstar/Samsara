"""Supporter key validation for Samsara.

Samsara is free — every feature, forever. Local capability is never gated
on anything in this module, and neither is bring-your-own-key Cloud AI
(that only needs cloud_llm.enabled + an API key, see samsara.cloud_llm).

This module exists for the optional "supporter key" a patron can enter to
mark their install as a supporter. It currently unlocks nothing — it is a
placeholder slot for a future managed-key feature (a Samsara-hosted API
key for non-technical users) and possibly other patron perks. Nothing in
the codebase may branch on validate_key()/is_premium() to gate a feature;
if you're tempted to add such a check, don't — see the Ava/Cloud tab and
plugins/commands/ask_ollama.py for how BYOK cloud access is actually
gated (enabled + api_key only).

v1 validation is intentionally simple — format check only, no server.
Keys are never logged, never printed to console, never included in any
debug output.
"""

import hashlib
import threading

_LICENSE_SALT = "samsara-premium-2026"
_lock = threading.Lock()


# TODO Phase 2 (managed key): use _hash_key for server-side validation
def _hash_key(key: str) -> str:
    return hashlib.sha256(
        f"{_LICENSE_SALT}:{key.strip()}".encode()
    ).hexdigest()[:16]


def validate_key(key) -> bool:
    """Return True if key matches the SAMSARA-XXXX-XXXX-XXXX format.

    Format rules:
    - After removing dashes, must be 16+ alphanumeric characters
    - Must start with "SAMSARA" (case-insensitive)
    """
    if not key or not isinstance(key, str):
        return False
    clean = key.strip().replace("-", "").upper()
    if len(clean) < 16:
        return False
    if not clean.startswith("SAMSARA"):
        return False
    return True


def is_premium(app) -> bool:
    """Return True if a valid supporter key is stored.

    Despite the name (kept for import compatibility), this does NOT gate
    any feature — it only reports whether a supporter key is present, for
    UI display (e.g. showing the masked-key state). Equivalent to
    has_supporter_key().
    """
    with _lock:
        key = getattr(app, 'config', {}).get("premium_license", "")
    return validate_key(key)


# Clearer name for new call sites; same check as is_premium().
has_supporter_key = is_premium


def get_license_key(app) -> str:
    """Return the current supporter key or empty string."""
    with _lock:
        return getattr(app, 'config', {}).get("premium_license", "")


def set_license_key(app, key: str) -> None:
    """Save a supporter key to config. Does not persist to disk."""
    with _lock:
        app.config["premium_license"] = key.strip()


def masked_key(key: str) -> str:
    """Return a display-safe masked version: SAMSARA-XXXX-...-LAST4."""
    clean = key.strip()
    if len(clean) > 8:
        return clean[:11] + "-...-" + clean[-4:]
    return clean
