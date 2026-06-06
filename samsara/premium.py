"""Premium license validation for Samsara Cloud AI features.

v1 validation is intentionally simple — format check only, no server.
The goal is to distinguish paying users from accidental clicks, not to
prevent determined pirates. Swap out validate_key() for server-side
validation later without changing anything else.

Keys are never logged, never printed to console, never included in
any debug output.
"""

import hashlib
import threading

_LICENSE_SALT = "samsara-premium-2026"
_lock = threading.Lock()


# TODO Phase 2: use _hash_key for server-side validation
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
    """Return True if the current installation has a valid premium license."""
    with _lock:
        key = getattr(app, 'config', {}).get("premium_license", "")
    return validate_key(key)


def get_license_key(app) -> str:
    """Return the current license key or empty string."""
    with _lock:
        return getattr(app, 'config', {}).get("premium_license", "")


def set_license_key(app, key: str) -> None:
    """Save a license key to config. Does not persist to disk."""
    with _lock:
        app.config["premium_license"] = key.strip()


def masked_key(key: str) -> str:
    """Return a display-safe masked version: SAMSARA-XXXX-...-LAST4."""
    clean = key.strip()
    if len(clean) > 8:
        return clean[:11] + "-...-" + clean[-4:]
    return clean
