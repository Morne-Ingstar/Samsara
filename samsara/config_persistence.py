"""Stage and atomically replace the shared configuration file."""

from collections.abc import Callable
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time


logger = logging.getLogger("Samsara")


def _stage_config(path: Path, config: dict) -> Path:
    staged = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            staged = Path(handle.name)
            json.dump(config, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("[CONFIG] Could not remove incomplete config temp: %s", exc)
        raise
    return staged


def write_config(path: Path, config: dict, rotate_backup: Callable[[], None]) -> None:
    """Replace the primary only after the new JSON file is complete and closed."""
    staged = _stage_config(path, config)
    if path.exists():
        rotate_backup()
        # Copy so a Windows reader can retain its open handle to the primary.
        try:
            shutil.copy2(path, path.with_suffix(".json.bak"))
        except OSError as exc:
            logger.warning("[CONFIG] Could not back up config to .bak: %s", exc)

    # A concurrent reader can briefly block replacement on Windows.
    for attempt in range(1, 4):
        try:
            os.replace(staged, path)
            return
        except OSError as exc:
            logger.debug("[CONFIG] os.replace attempt %d failed: %s", attempt, exc)
            if attempt == 3:
                # Retain the completed draft and backups for recovery.
                raise
            time.sleep(0.02)
