"""Early, restart-applied interface scaling for the whole Qt application."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import MutableMapping


UI_SCALE_OPTIONS = {
    "Standard (100%)": 1.0,
    "Large (115%)": 1.15,
    "Extra large (130%)": 1.30,
}


def normalize_ui_scale(value) -> float:
    """Return a bounded supported scale; malformed values become standard."""
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(UI_SCALE_OPTIONS.values(), key=lambda option: abs(option - candidate))


def ui_scale_label(value) -> str:
    normalized = normalize_ui_scale(value)
    for label, scale in UI_SCALE_OPTIONS.items():
        if scale == normalized:
            return label
    return next(iter(UI_SCALE_OPTIONS))


def apply_early_ui_scale(
    config_path: str | Path,
    environ: MutableMapping[str, str] | None = None,
) -> float:
    """Read only ``ui_scale`` and set Qt's scale before QApplication exists."""
    target = os.environ if environ is None else environ
    scale = 1.0
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if isinstance(config, dict):
            scale = normalize_ui_scale(config.get("ui_scale", 1.0))
    except (OSError, json.JSONDecodeError, TypeError):
        scale = 1.0
    target["QT_SCALE_FACTOR"] = f"{scale:g}"
    return scale
