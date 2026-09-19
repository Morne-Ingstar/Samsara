"""Create a small, scrubbed support bundle without importing Qt.

The archive is deliberately allowlisted: recent application logs, scrubbed
configuration, version facts, and hardware names/layout only. User content
stores are never walked or copied.
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import json
import os
import platform
import re
import tempfile
import zipfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from samsara import __version__
from samsara.paths import samsara_config_path, samsara_home_dir


LOG_TAIL_LINES = 1000
_REDACTED = "<redacted>"
_SENSITIVE_KEY = re.compile(
    r"(?:token|key|secret|password|credential|authorization|auth)", re.IGNORECASE,
)
_PRIVATE_STORE_KEY = re.compile(
    r"(?:transcript|utterance|dictat|memo|ava[_ -]?memory|vault)", re.IGNORECASE,
)
_PATH_KEY = re.compile(
    r"(?:path|directory|filename|filepath|(?:^|_)dir(?:_|$))", re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w.-]*(?:token|key|secret|password|credential|authorization)"
    r"[\w.-]*\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:Bearer\s+)?[^\s,;]+)"
)
_PRIVATE_LOG_LINE = re.compile(
    r"""(?ix)
    \b(?:transcript(?:ion)?|utterance|dictat(?:ion|ed)?|memo\s+content|ava\s+memory|vault(?:\s+path)?)\b
    | \b(?:raw|text|heard|recognized)\s*[:=]
    | \[(?:TEXT|HEAR|DICTATE(?:-PREVIEW)?|AVA-CMD-UTT|CMD-UTT|CORRECT|PARSE|VERBATIM|OK|MEMO
        |SMART|SNIPPET|GUARD|SKIP|PAUSED|CMD MODE|CMD|AVA)\]
    | \[STREAM\]\s*Partial:
    | \[DICTATE-PREVIEW\].*(?:correction choice|fixed in the draft)
    | User said:|No command matched:|is a command in the disabled pack|not spoken|trailing text
    """
)
_VAULT_PATH_VALUE = re.compile(r"(?i)(?:[a-z]:[\\/]|/)[^\r\n]*\bvault\b[^\r\n]*")

_PACKAGES = (
    ("PySide6", "PySide6"),
    ("faster-whisper", "faster-whisper"),
    ("ctranslate2", "ctranslate2"),
    ("sounddevice", "sounddevice"),
    ("numpy", "numpy"),
)


def _scrub_config(value: Any, *, key: str = "") -> Any:
    """Preserve ordinary config shape, redact credentials/paths, omit stores."""
    if key and _PRIVATE_STORE_KEY.search(key):
        return _OMIT
    if key and (_SENSITIVE_KEY.search(key) or _PATH_KEY.search(key)):
        return _REDACTED
    if isinstance(value, Mapping):
        clean = {}
        for child_key, child_value in value.items():
            name = str(child_key)
            scrubbed = _scrub_config(child_value, key=name)
            if scrubbed is not _OMIT:
                clean[name] = scrubbed
        return clean
    if isinstance(value, list):
        return [item for item in (_scrub_config(item) for item in value) if item is not _OMIT]
    if isinstance(value, tuple):
        return [item for item in (_scrub_config(item) for item in value) if item is not _OMIT]
    if isinstance(value, str) and _VAULT_PATH_VALUE.search(value):
        return _REDACTED
    return value


class _OmitValue:
    pass


_OMIT = _OmitValue()


def scrub_config(config: Any) -> Any:
    """Return the safe, JSON-compatible config representation."""
    result = _scrub_config(config)
    return {} if result is _OMIT else result


def _read_config(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as source:
            parsed = json.load(source)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Could not read config.json safely") from exc
    if not isinstance(parsed, (dict, list)):
        raise ValueError("config.json must contain a JSON object or list")
    return scrub_config(parsed)


def _scrub_log_line(line: str) -> str:
    if _PRIVATE_LOG_LINE.search(line):
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        replacement = "[redacted: private content or path]"
        return replacement + ending
    return _SECRET_ASSIGNMENT.sub(r"\1" + _REDACTED, line)


def _read_log(path: Path, *, tail_lines: int | None = None) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            lines = source.readlines()
    except FileNotFoundError:
        return "[No log file was available when this report was created.]\n"
    if tail_lines is not None:
        lines = lines[-tail_lines:]
    return "".join(_scrub_log_line(line) for line in lines)


def _desktop_directory() -> Path:
    """Use the Windows Desktop known folder, with a portable fallback."""
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
            if result == 0 and buffer.value:
                return Path(buffer.value)
        except (AttributeError, OSError, TypeError):
            pass
    return Path.home() / "Desktop"


def _monitor_layout() -> list[str]:
    if os.name != "nt":
        return ["Unavailable on this platform"]

    class Rect(ctypes.Structure):
        _fields_ = [(name, ctypes.c_long) for name in ("left", "top", "right", "bottom")]

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", Rect), ("rcWork", Rect),
                    ("dwFlags", ctypes.c_ulong)]

    monitors: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Rect), ctypes.c_ssize_t,
    )

    def add_monitor(handle, _dc, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            monitors.append(
                f"Monitor {len(monitors) + 1}: {rect.right - rect.left}x{rect.bottom - rect.top} "
                f"at ({rect.left}, {rect.top})"
            )
        return 1

    try:
        callback = callback_type(add_monitor)
        ok = ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    except (AttributeError, OSError, TypeError):
        return ["Unavailable"]
    return monitors if ok and monitors else ["Unavailable"]


def _audio_device_names() -> list[str]:
    try:
        import sounddevice

        devices = sounddevice.query_devices()
    except Exception:
        return ["Unavailable"]
    names = []
    seen = set()
    for device in devices:
        name = str(device.get("name", "")).replace("\r", " ").replace("\n", " ").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names or ["No devices reported"]


def _version_text(config: Any) -> str:
    python_version = platform.python_version()
    if not isinstance(config, Mapping):
        config = {}
    configured_device = config.get("device", "auto")
    if configured_device in ("cuda", "gpu"):
        compute_mode = "GPU requested (CUDA)"
    elif configured_device == "auto":
        compute_mode = "Automatic GPU/CPU selection"
    elif configured_device == "cpu":
        compute_mode = "CPU"
    else:
        compute_mode = "Unknown"
    model_size = config.get("model_size", "unknown")
    model_sizes = {
        "tiny", "tiny.en", "base", "base.en", "small", "small.en",
        "medium", "medium.en", "large-v3",
    }
    if not isinstance(model_size, str) or model_size not in model_sizes:
        model_size = "unknown"
    compute_type = config.get("compute_type", "unknown")
    if not isinstance(compute_type, str) or compute_type not in {"float16", "int8", "float32"}:
        compute_type = "unknown"
    lines = [
        f"Samsara: {__version__}",
        f"Python: {python_version}",
        f"Speech model: {model_size}",
        f"Speech compute type: {compute_type}",
        f"Compute mode: {compute_mode}",
        "Key packages:",
    ]
    for distribution, label in _PACKAGES:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = "not installed"
        lines.append(f"  {label}: {version}")
    return "\n".join(lines) + "\n"


def _system_text() -> str:
    os_name = platform.system() or "Unknown"
    release = platform.release() or "Unknown"
    build = platform.version() or "Unknown"
    lines = [
        f"Operating system: {os_name} {release}",
        f"OS build: {build}",
        "Monitor layout:",
        *(f"  {item}" for item in _monitor_layout()),
        "Audio devices (names only):",
        *(f"  {item}" for item in _audio_device_names()),
        "Excluded: transcripts, memos, Ava memory stores, and vault files/paths.",
    ]
    return "\n".join(lines) + "\n"


def _unique_target(desktop: Path, stamp: datetime) -> Path:
    base = f"samsara-report_{stamp:%Y-%m-%d_%H-%M}"
    candidate = desktop / f"{base}.zip"
    index = 2
    while candidate.exists():
        candidate = desktop / f"{base}_{index}.zip"
        index += 1
    return candidate


def build_bundle(
    *,
    home_dir: str | Path | None = None,
    desktop_dir: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Build the allowlisted support zip and return its path.

    ``home_dir``, ``desktop_dir`` and ``now`` are injection points for tests;
    the interactive caller uses SAMSARA_HOME_DIR, the user's Desktop, and the
    local clock. No Qt, app startup, or profile mutation is required.
    """
    home = Path(home_dir) if home_dir is not None else samsara_home_dir()
    config_path = Path(home_dir) / "config.json" if home_dir is not None else samsara_config_path()
    logs = home / "logs"
    desktop = Path(desktop_dir) if desktop_dir is not None else _desktop_directory()
    desktop.mkdir(parents=True, exist_ok=True)

    config = _read_config(config_path)
    stamp = now or datetime.now()
    target = _unique_target(desktop, stamp)

    entries: list[tuple[str, str]] = [
        ("samsara.log", _read_log(logs / "samsara.log", tail_lines=LOG_TAIL_LINES)),
    ]
    rotated = logs / "samsara.log.1"
    if rotated.is_file():
        entries.append(("samsara.log.1", _read_log(rotated)))
    entries.extend([
        ("config.json", json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"),
        ("versions.txt", _version_text(config)),
        ("system.txt", _system_text()),
    ])

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".samsara-report-", suffix=".tmp", dir=desktop,
                                         delete=False) as temp_file:
            temp_path = Path(temp_file.name)
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
        os.replace(temp_path, target)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return target


__all__ = ["LOG_TAIL_LINES", "build_bundle", "scrub_config"]
