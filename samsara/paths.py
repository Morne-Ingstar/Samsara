"""Resolution for the app's per-user data directory (normally ~/.samsara).

Config, logs, and every per-user JSON/db file (ava profile, corrections,
command stats, tasks, health log, app-index cache) independently derive
their path from this same directory. SAMSARA_HOME_DIR overrides it wholesale
so external tooling (tools/frozen_smoke.py) can point a frozen build at an
isolated temp profile without ever touching the real ~/.samsara. Unset,
resolution is unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path


def samsara_home_dir() -> Path:
    """Return the app's per-user data directory. Does not create it --
    callers keep whatever mkdir behavior they already had."""
    override = os.environ.get("SAMSARA_HOME_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".samsara"
