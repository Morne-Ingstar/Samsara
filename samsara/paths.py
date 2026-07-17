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
import shutil
from datetime import datetime
from pathlib import Path


def samsara_home_dir() -> Path:
    """Return the app's per-user data directory. Does not create it --
    callers keep whatever mkdir behavior they already had."""
    override = os.environ.get("SAMSARA_HOME_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".samsara"


def samsara_config_path() -> Path:
    """Return the one config path used by source and frozen launches.

    Isolation remains explicit through ``SAMSARA_HOME_DIR``. Keeping an
    implicit repository-local config for source launches gave one user two
    contradictory settings profiles depending on how Samsara was started.
    """
    return samsara_home_dir() / "config.json"


def migrate_legacy_source_config(legacy_path: str | Path) -> bool:
    """One-time migration from the former repository-local source config.

    Returns ``True`` only when the legacy file was copied. Explicit isolated
    profiles never participate. The marker prevents a stale repository copy
    from overwriting later settings in the unified per-user profile.
    """
    if os.environ.get("SAMSARA_HOME_DIR"):
        return False

    legacy = Path(legacy_path)
    target = samsara_config_path()
    marker = target.parent / ".source-config-migrated"
    if marker.exists():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    copied = False
    if legacy.exists() and (
        not target.exists() or legacy.stat().st_mtime > target.stat().st_mtime
    ):
        temporary = target.with_suffix(".json.source-migration.tmp")
        shutil.copy2(legacy, temporary)
        os.replace(temporary, target)
        copied = True

    marker.write_text("source config migration checked\n", encoding="utf-8")
    return copied


def quarantine_corrupt_file(path: Path, logger, error) -> Path:
    """Rename an unparseable store file out of the way, preserving its
    bytes for manual recovery instead of letting the next save silently
    bury them (2026-07-09 correction-store loss: a JSON parse failure fell
    back to empty in-memory state, and the next save wrote that empty
    state straight over the original file -- the corrupt bytes were never
    recoverable). Callers proceed with empty defaults after this.

    Shared by every store's load function (samsara/phonetic_wash.py,
    samsara/wake_corrections.py, samsara/ava_corrections.py, samsara/ui/
    voice_training_qt.py) so the rename+log pattern can't drift between
    them. `logger` is the CALLER's own logger (not this module's) so the
    ERROR line is attributed to the store that actually failed.

    Returns the quarantine path on success, or the original `path`
    unchanged if the rename itself fails (e.g. permissions) -- callers
    still proceed with empty defaults either way; this never raises.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quarantine_path = path.with_name(f"{path.name}.corrupt-{timestamp}")
    try:
        path.rename(quarantine_path)
    except OSError as rename_exc:
        logger.error(
            f"[STORE] {path.name} failed to parse ({error}) -- quarantine "
            f"rename also failed ({rename_exc}), corrupt file left in place "
            f"at {path}"
        )
        return path
    logger.error(
        f"[STORE] {path.name} failed to parse ({error}) -- quarantined to "
        f"{quarantine_path}"
    )
    return quarantine_path
