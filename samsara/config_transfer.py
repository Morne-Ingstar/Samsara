"""Portable import/export helpers for Samsara configuration backups."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


EXPORT_FORMAT = "samsara-config-backup"
EXPORT_VERSION = 1
MAX_IMPORT_BYTES = 5 * 1024 * 1024


class ConfigTransferError(ValueError):
    """Raised when a configuration backup is unsafe or malformed."""


def _require_object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigTransferError(f"{label} must be a JSON object.")
    return value


def build_export(config: Mapping[str, Any]) -> dict:
    """Return a versioned, self-describing backup payload."""
    snapshot = copy.deepcopy(_require_object(dict(config), "Configuration"))
    # Round-trip now so a broken runtime value cannot create an unusable file.
    try:
        json.dumps(snapshot)
    except (TypeError, ValueError) as exc:
        raise ConfigTransferError(f"Configuration contains unsupported data: {exc}") from exc
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "contains_private_values": True,
        "config": snapshot,
    }


def export_config(path: str | Path, config: Mapping[str, Any]) -> Path:
    """Write a complete backup atomically and return its final path."""
    destination = Path(path)
    payload = build_export(config)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(destination)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigTransferError(f"Could not write backup: {exc}") from exc
    return destination


def load_config_export(path: str | Path) -> dict:
    """Read and validate either a versioned backup or legacy raw config JSON."""
    source = Path(path)
    try:
        if source.stat().st_size > MAX_IMPORT_BYTES:
            raise ConfigTransferError("Backup is larger than the 5 MB safety limit.")
        payload = json.loads(source.read_text(encoding="utf-8"))
    except ConfigTransferError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigTransferError(f"Could not read backup: {exc}") from exc

    payload = _require_object(payload, "Backup")
    if payload.get("format") == EXPORT_FORMAT:
        version = payload.get("version")
        if version != EXPORT_VERSION:
            raise ConfigTransferError(
                f"Unsupported backup version {version!r}; expected {EXPORT_VERSION}."
            )
        config = _require_object(payload.get("config"), "Backup configuration")
    else:
        # Accept a copied config.json so existing users are not locked out of
        # the feature merely because their backup predates the wrapper.
        config = payload

    try:
        json.dumps(config)
    except (TypeError, ValueError) as exc:
        raise ConfigTransferError(f"Backup contains unsupported data: {exc}") from exc
    return copy.deepcopy(config)


def merge_import(current: Mapping[str, Any], imported: Mapping[str, Any]) -> dict:
    """Overlay imported settings recursively while preserving newer unknown keys."""
    result = copy.deepcopy(_require_object(dict(current), "Current configuration"))
    incoming = copy.deepcopy(
        _require_object(dict(imported), "Imported configuration")
    )

    imported_cloud = incoming.get("cloud_llm")
    if isinstance(imported_cloud, dict):
        # Custom cloud endpoints have never been exposed or confirmed in the
        # UI. Do not allow a backup to smuggle one in and reuse a key already
        # present on this machine.
        imported_cloud.pop("providers", None)

        imported_provider = imported_cloud.get("provider")
        if imported_provider is not None:
            from samsara.cloud_llm import SUPPORTED_PROVIDERS
            if imported_provider not in SUPPORTED_PROVIDERS:
                raise ConfigTransferError(
                    f"Unsupported cloud LLM provider {imported_provider!r}."
                )

            current_cloud = result.get("cloud_llm")
            current_provider = (
                current_cloud.get("provider", "deepseek")
                if isinstance(current_cloud, dict) else "deepseek"
            )
            if (
                imported_provider != current_provider
                and "api_key" not in imported_cloud
                and isinstance(current_cloud, dict)
            ):
                # A key is provider-specific secret material. Never preserve
                # it implicitly while an import changes where it will be sent.
                current_cloud.pop("api_key", None)

    def _merge(target: dict, incoming: dict) -> None:
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                _merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    _merge(result, incoming)
    return result
