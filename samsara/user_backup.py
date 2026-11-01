"""Local, credential-filtered user snapshots and verified restore.

Policy: only USER_FILES/TEXT_TREES are backed up. Unknown paths, history/logs,
recordings, caches, models, update payloads, browser_bridge/agent_server auth,
and old backups are excluded. JSON credential fields are omitted recursively;
text containing credential assignments/recognisable tokens is excluded whole.
This is a settings recovery archive, not a secret store or a full-home image.
External profile/plugin/vault paths are never followed. No network is used.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tempfile
import threading
import zipfile

from samsara import __version__
from samsara.paths import samsara_home_dir

logger = logging.getLogger(__name__)
FORMAT = "samsara-user-backup-1"
RETENTION = {"daily": 7, "weekly": 4, "safety": 1, "pre-change": 1}
USER_FILES = frozenset({
    "config.json", "training_data.json", "user_corrections.json",
    "user_wake_corrections.json", "ava_corrections.json", "correction_queue.json",
    "correction_candidates.json", "builtin_vocabulary_disabled.json",
    "user_aliases.json", "commands.json", "vocabulary.json", "dictionary.json",
    "ava_memory.md", "ava_memory_archive.md", "ava_memory.json", "ava_memory_pending.json", "ava_persona.md",
    "ava_profile.json", "tasks.json", "reminders.json", "health_log.json",
    "mic_scores.json", "alarm_stats.json", "command_palette.json", "hints_shown.json",
})
TEXT_TREES = frozenset({"memos", "snippets", "profiles", "dictionaries", "personal_plugins"})
TEXT_SUFFIXES = frozenset({".json", ".jsonl", ".md", ".txt", ".py"})
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 4096
_LOCK = threading.RLock()
_PRE_CHANGE = {}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|"
    r"(?:^|[_-])token(?:$|[_-])|password|passwd|credential|authorization|"
    r"auth[_-]?key|(?:^|[_-])auth(?:$|[_-])|secret)", re.I)
_SECRET_TEXT = re.compile(
    # Start an assignment at the beginning of its key. Retrying at every
    # character of a long safe value made backup verification quadratic.
    r"(?i)(?:(?<![\w.-])[\"']?[\w.-]*(?:api[ _-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|token(?!s\b)|password|passwd|secret|credential)"
    r"[\w.-]*[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+|"
    r"\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{15,}|(?:glpat-|hf_|xox[baprs]-)[\w-]{15,}|"
    r"AKIA[A-Z0-9]{16}|AIza[\w-]{30,}|Bearer\s+\S+|"
    r"eyJ[\w-]+\.[\w-]+\.[\w-]+)|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----)")


class BackupError(ValueError):
    """An archive or destination cannot be used safely."""


def _now():
    return datetime.now(timezone.utc)


def _home(home=None):
    return Path(home) if home is not None else samsara_home_dir()


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_path(home, relative, *, check_home=True):
    parts = PurePosixPath(relative).parts
    if (not parts or "\\" in relative or ":" in relative
            or str(PurePosixPath(relative)) != relative
            or any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in parts)
            or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_reserved()):
        raise BackupError("Unsafe snapshot path")
    path = home
    # Check ancestors too: Windows junctions must not redirect profile writes.
    if check_home:
        for ancestor in (home, *home.parents):
            try:
                if _linked(ancestor):
                    raise BackupError("Linked profile directories are not supported")
            except FileNotFoundError:
                pass
    for part in parts:
        path = path / part
        try:
            if _linked(path):
                raise BackupError("Linked snapshot paths are not supported")
        except FileNotFoundError:
            pass
    return path


def _eligible(name):
    parts = PurePosixPath(name).parts
    if any(_SECRET_KEY.search(p) for p in parts):
        return False
    return name in USER_FILES or (
        len(parts) > 1 and parts[0] in TEXT_TREES
        and Path(name).suffix.lower() in TEXT_SUFFIXES
        and not any(p.startswith(".") or p == "__pycache__" for p in parts))


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _SECRET_KEY.search(k)}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _filtered(name, raw):
    if len(raw) > MAX_FILE_BYTES:
        raise BackupError("A user-data file exceeds the backup size limit")
    try:
        text = raw.decode("utf-8-sig")
        if name.endswith(".json"):
            original = json.loads(text)
            cleaned = _scrub(original)
            if name == "config.json" and not isinstance(cleaned, dict):
                raise BackupError("Configuration is not an object")
            if cleaned != original:
                raw = json.dumps(cleaned, ensure_ascii=False, indent=2).encode("utf-8")
        elif name.endswith(".jsonl"):
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            cleaned = [_scrub(row) for row in rows]
            if cleaned != rows:
                raw = ("\n".join(json.dumps(row, ensure_ascii=False) for row in cleaned)
                       + "\n").encode("utf-8")
        if _SECRET_TEXT.search(raw.decode("utf-8-sig")):
            return None
        return raw
    except (UnicodeError, json.JSONDecodeError):
        # Never archive unreadable JSON that might contain unfiltered secrets.
        return None


def _collect(home):
    result = {}
    _safe_path(home, "backups")  # Check profile ancestors once per collection.
    existing = {p.name for p in home.iterdir()} if home.exists() else set()
    candidates = sorted(USER_FILES & existing)
    for tree in sorted(TEXT_TREES):
        if tree not in existing:
            continue
        root = _safe_path(home, tree, check_home=False)
        if root.is_dir():
            for current, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not _linked(Path(current) / d)]
                candidates.extend((Path(current) / f).relative_to(home).as_posix() for f in files)
    for name in sorted(candidates):
        if not _eligible(name):
            continue
        path = _safe_path(home, name, check_home=False)
        if path.is_file():
            if path.stat().st_size > MAX_FILE_BYTES:
                raise BackupError("A user-data file exceeds the backup size limit")
            raw = path.read_bytes()
            if path.read_bytes() != raw:
                raise BackupError("User data changed during backup; retry later")
            filtered = _filtered(name, raw)
            if filtered is None:
                if name == "config.json":
                    raise BackupError("Configuration could not be filtered safely")
                continue
            result[name] = filtered
            if len(result) > MAX_FILES or sum(map(len, result.values())) > MAX_TOTAL_BYTES:
                raise BackupError("User data exceeds the backup size limit")
    if len(result) > MAX_FILES or sum(map(len, result.values())) > MAX_TOTAL_BYTES:
        raise BackupError("User data exceeds the backup size limit")
    return result


def create_snapshot(home=None, *, reason="daily", now=None, prune=True, reuse=False):
    """Write, verify, then publish a ZIP; failure never publishes a partial ZIP."""
    home, now = _home(home), now or _now()
    if reason not in {"daily", "safety", "pre-change"}:
        raise BackupError("Unknown snapshot reason")
    with _LOCK:
        files = _collect(home)
        cache_key = str(home.absolute())
        if reuse and reason == "pre-change" and cache_key in _PRE_CHANGE:
            previous = _PRE_CHANGE[cache_key]
            try:
                manifest, saved = verify_snapshot(previous)
                if saved == files and manifest["version"] == __version__:
                    return previous  # Still an exact, verified copy of current data.
            except BackupError:
                pass
        folder = _safe_path(home, "backups")
        folder.mkdir(parents=True, exist_ok=True)
        manifest = {"format": FORMAT, "created_at": now.isoformat(),
                    "version": __version__, "reason": reason,
                    "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest(),
                               "size": len(raw)} for name, raw in files.items()]}
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=folder)
        os.close(fd)
        temp = Path(temporary)
        destination = folder / (now.strftime("%Y%m%dT%H%M%S%fZ") + ".zip")
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                for name, raw in files.items():
                    archive.writestr("data/" + name, raw)
            verify_snapshot(temp)
            if destination.exists():
                raise BackupError("Snapshot timestamp already exists")
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)
        if prune:
            prune_snapshots(home)
        if reason == "pre-change":
            _PRE_CHANGE[cache_key] = destination
        return destination


def verify_snapshot(path):
    """Validate all members, bounds, paths and hashes before returning any data."""
    try:
        if _linked(Path(path)):
            raise BackupError("Linked archives are not supported")
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if (len(names) != len(set(names)) or len(names) > MAX_FILES + 1
                    or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES + MAX_FILE_BYTES
                    or any(i.file_size > MAX_FILE_BYTES or stat.S_ISLNK(i.external_attr >> 16)
                           for i in infos)):
                raise BackupError("Invalid archive members or size")
            manifest = json.loads(archive.read("manifest.json"))
            if (manifest.get("format") != FORMAT
                    or manifest.get("reason") not in {"daily", "safety", "pre-change"}
                    or not isinstance(manifest.get("version"), str)):
                raise BackupError("Unknown snapshot format")
            timestamp = datetime.fromisoformat(manifest["created_at"])
            if timestamp.tzinfo is None:
                raise BackupError("Snapshot date has no timezone")
            files, folded = {}, set()
            for entry in manifest["files"]:
                name = entry["path"]
                # Reuse lexical path checks without accessing a user's home.
                if (not _eligible(name) or name.casefold() in folded
                        or "\\" in name or ":" in name
                        or PurePosixPath(name).is_absolute()
                        or PureWindowsPath(name).is_reserved()
                        or str(PurePosixPath(name)) != name
                        or any(p in {".", ".."} or p.endswith((".", " "))
                               for p in PurePosixPath(name).parts)):
                    raise BackupError("Unsafe or duplicate user-data path")
                folded.add(name.casefold())
                raw = archive.read("data/" + name)
                if (len(raw) != entry["size"]
                        or hashlib.sha256(raw).hexdigest() != entry["sha256"]
                        or _filtered(name, raw) != raw):
                    raise BackupError("Snapshot checksum or credential policy failed")
                files[name] = raw
            if set(names) != {"manifest.json", *("data/" + name for name in files)}:
                raise BackupError("Unexpected or missing snapshot members")
            return manifest, files
    except BackupError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, NotImplementedError,
            zipfile.BadZipFile, RuntimeError, EOFError, RecursionError, OverflowError) as exc:
        raise BackupError("Snapshot is damaged or has no valid manifest") from exc


def list_snapshots(home=None):
    """Newest first; invalid archives remain visible as damaged, never selectable."""
    home = _home(home)
    folder = _safe_path(home, "backups")
    entries = []
    for path in sorted(folder.glob("*.zip"), reverse=True):
        try:
            manifest, files = verify_snapshot(path)
            if "config.json" not in files:
                raise BackupError("Snapshot has no configuration")
            label = f'{manifest["created_at"]} · v{manifest["version"]} · {manifest["reason"]}'
            entries.append({"path": path, "label": label, "damaged": False,
                            "manifest": manifest})
        except BackupError:
            entries.append({"path": path, "label": path.stem + " · Damaged — skipped",
                            "damaged": True})
    return entries


def prune_snapshots(home=None):
    """7 distinct daily dates plus 4 older ISO weeks; one safety/pre-change copy."""
    with _LOCK:
        entries = list_snapshots(home)
        keep, days, weeks = set(), set(), set()
        daily = [e for e in entries if not e["damaged"] and e["manifest"]["reason"] == "daily"]
        for e in daily:
            day = datetime.fromisoformat(e["manifest"]["created_at"]).date()
            if day not in days and len(days) < RETENTION["daily"]:
                keep.add(e["path"])
                days.add(day)
        for e in daily:
            day = datetime.fromisoformat(e["manifest"]["created_at"]).date()
            week = day.isocalendar()[:2]
            if day not in days and week not in weeks and len(weeks) < RETENTION["weekly"]:
                keep.add(e["path"])
                weeks.add(week)
        for reason in ("safety", "pre-change"):
            matches = [e for e in entries if not e["damaged"] and e["manifest"]["reason"] == reason]
            keep.update(e["path"] for e in matches[:RETENTION[reason]])
        for e in entries:
            # Damaged evidence is never silently destroyed by automatic pruning.
            if not e["damaged"] and e["path"] not in keep:
                e["path"].unlink()


def ensure_daily(home=None, *, now=None):
    home, now = _home(home), now or _now()
    with _LOCK:
        for e in list_snapshots(home):
            if (not e["damaged"] and e["manifest"]["reason"] == "daily"
                    and datetime.fromisoformat(e["manifest"]["created_at"]).date() == now.date()):
                return e["path"]
        return create_snapshot(home, now=now)


def _preserve_secrets(incoming, current):
    """Keep this machine's credentials only within the same provider/endpoint."""
    if isinstance(incoming, dict) and isinstance(current, dict):
        if any(incoming.get(k) != current.get(k) for k in
               ("provider", "base_url", "endpoint", "url") if k in incoming or k in current):
            return incoming
        result = copy.deepcopy(incoming)
        for key, value in current.items():
            if _SECRET_KEY.search(key):
                result[key] = value
            elif key in result:
                result[key] = _preserve_secrets(result[key], value)
            elif isinstance(value, dict):
                secrets = _preserve_secrets({}, value)
                if secrets:
                    result[key] = secrets
        return result
    return incoming


def _replace(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_snapshot(snapshot=None, home=None, *, fallback=False):
    """Verify before mutation, write safety copy, restore with in-flight rollback.

    fallback=True chooses the first usable snapshot at/before the requested one;
    no selection chooses the latest usable daily/pre-change snapshot. Safety
    copies are available explicitly but never become an automatic fallback.
    Existing files absent from a snapshot are preserved (additive recovery).
    """
    home = _home(home)
    with _LOCK:
        chosen = Path(snapshot) if snapshot is not None else None
        if fallback or chosen is None:
            candidates = [e["path"] for e in list_snapshots(home)
                          if not e["damaged"] and e["manifest"]["reason"] != "safety"
                          and (chosen is None or e["path"].name <= chosen.name)]
        else:
            candidates = [chosen]
        if not candidates:
            raise BackupError("No usable snapshot is available")
        chosen = candidates[0]
        _, files = verify_snapshot(chosen)
        if "config.json" not in files:
            raise BackupError("Snapshot has no restorable configuration")
        previous = {}
        for name in files:
            path = _safe_path(home, name)
            previous[name] = path.read_bytes() if path.exists() else None
        current = previous.get("config.json")
        if current is not None:
            try:
                incoming = json.loads(files["config.json"])
                merged = _preserve_secrets(incoming, json.loads(current))
                if merged != incoming:
                    files["config.json"] = json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
            except (ValueError, UnicodeError):
                pass
        safety = create_snapshot(home, reason="safety", prune=False)
        written = []
        try:
            # Config last: a watcher never sees it before the other stores exist.
            for name in sorted(files, key=lambda n: n == "config.json"):
                path = _safe_path(home, name)
                written.append(name)
                _replace(path, files[name])
        except Exception as exc:
            failed = []
            for name in reversed(written):
                try:
                    path = _safe_path(home, name)
                    if previous[name] is None:
                        path.unlink(missing_ok=True)
                    else:
                        _replace(path, previous[name])
                except (OSError, BackupError):
                    failed.append(name)
            if failed:
                raise BackupError("Restore and rollback failed; recover using the safety snapshot") from exc
            raise BackupError("Restore failed; previous files were restored") from exc
        prune_snapshots(home)
        return {"snapshot": chosen, "safety": safety}


def config_defaults_before_load(app, **defaults):
    """Protect existing user data before building defaults for config loading."""
    before_config_load(app)
    from samsara.config_seed import build_default_config
    return build_default_config(**defaults)


def launch_update_with_backup(prepared, current_pid, process_runner):
    """Protect the latest user data before launch, retaining updater cleanup."""
    from samsara import updater
    try:
        from samsara.update_installer import launch_helper
        create_snapshot(reason="pre-change")
        launch_helper(prepared, current_pid, process_runner)
    except Exception as exc:
        message = f"Could not launch the update installer: {exc}"
        try:
            updater._cleanup_pre_swap_failure(prepared, message)
        except OSError as cleanup_exc:
            exc.add_note(f"Could not record update cleanup status: {cleanup_exc}")
        raise updater.UpdateError(f"Could not launch the update installer: {exc}") from exc


def before_config_load(app):
    """Startup seam runs before any migration; never bypass existing recovery."""
    home = Path(app.config_path).parent
    config = home / "config.json"
    if not config.exists():
        return  # The service snapshots first-run data after defaults are saved.
    try:
        parsed = json.loads(config.read_bytes())
        if not isinstance(parsed, dict):
            return  # Preserve the existing corrupt-config quarantine path.
    except (OSError, ValueError):
        return
    # Do not allow migrations to write if a valid profile cannot be protected.
    create_snapshot(home, reason="pre-change", reuse=True)
    try:
        ensure_daily(home)
    except (OSError, BackupError):
        logger.warning("[BACKUP] Daily snapshot unavailable; pre-change snapshot is available")


def start_daily_service(app):
    """One daemon per app; checks dates hourly even across overnight sessions."""
    if getattr(app, "_user_backup_worker", None) is not None:
        return
    from samsara.runtime import thread_registry
    home = Path(app.config_path).parent
    stopped = threading.Event()
    app._user_backup_stop = stopped

    def run():
        while not stopped.is_set():
            try:
                with app._config_lock:
                    ensure_daily(home)
            except (OSError, BackupError):
                logger.warning("[BACKUP] Daily snapshot unavailable; will retry")
            stopped.wait(3600)

    thread_registry.on_shutdown(stopped.set)
    app._user_backup_worker = thread_registry.spawn("user-backup", run, daemon=True)


def restore_for_app(app, snapshot=None, *, fallback=False):
    """Restore while config saves are excluded, then use the live reload path."""
    with app._config_lock:
        result = restore_snapshot(snapshot, Path(app.config_path).parent, fallback=fallback)
        # Reload's three-way merge must treat restored disk values as authoritative.
        app._config_last_disk_snapshot = copy.deepcopy(app.config)
        app._config_load_failed = False
        app._config_corrupt_backup_name = None
    app.reload_config_from_disk()
    from samsara import model_guard
    model_guard.saved(app)
    guard = getattr(app, "_model_guard", None)
    if isinstance(guard, model_guard.ModelGuard):
        guard.write_checkpoint()  # This explicitly confirmed restore is a user choice.
    app.dictionary_store.load_training_data()
    from samsara.builtin_vocabulary import BuiltinPreferences
    app.dictionary_store._builtin_preferences = BuiltinPreferences(app.config_path)
    app.command_executor.reload_commands()
    from samsara import phonetic_wash, wake_corrections, ava_corrections
    phonetic_wash.reload_corrections()
    wake_corrections.reload_corrections()
    with ava_corrections._aliases_lock:
        ava_corrections._load()
        ava_corrections._dirty_count = False
    from samsara.command_catalog import install_user_aliases
    from samsara import plugin_commands
    if plugin_commands._shared_matcher is not None:
        install_user_aliases(plugin_commands._shared_matcher, Path(app.config_path).parent)
    return result


def show_restore_dialog(app, parent=None):
    """Shared Settings/voice picker; no restore runs before explicit confirmation."""
    from PySide6.QtWidgets import (
        QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem,
        QMessageBox, QVBoxLayout,
    )
    from PySide6.QtCore import Qt
    from samsara.ui import theme

    dialog = QDialog(parent)
    dialog.setWindowTitle("Restore my settings")
    dialog.setMinimumWidth(680)
    dialog.setStyleSheet(theme.build_stylesheet())
    layout = QVBoxLayout(dialog)
    description = QLabel(
        "Choose a local snapshot, newest first. Damaged copies are skipped.\n"
        "Your current data is backed up before restore. Credentials are excluded.\n"
        "Settings are reloaded immediately. Restart later to refresh other open pages.")
    description.setWordWrap(True)
    layout.addWidget(description)
    listing = QListWidget()
    listing.setAccessibleName("Local settings snapshots, newest first")
    layout.addWidget(listing)
    try:
        entries = list_snapshots(Path(app.config_path).parent)
    except (OSError, BackupError):
        entries = []
    for entry in entries:
        item = QListWidgetItem(entry["label"])
        if entry["damaged"]:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
        else:
            item.setData(Qt.ItemDataRole.UserRole, str(entry["path"]))
        listing.addItem(item)
    usable = next((i for i, e in enumerate(entries) if not e["damaged"]), None)
    if usable is not None:
        listing.setCurrentRow(usable)
    else:
        layout.addWidget(QLabel("No usable snapshots found yet."))
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.RestoreDefaults
                               | QDialogButtonBox.StandardButton.Cancel)
    restore = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
    restore.setText("Restore selected snapshot…")
    restore.setEnabled(usable is not None)
    layout.addWidget(buttons)
    buttons.rejected.connect(dialog.reject)

    def apply():
        selected = listing.currentItem()
        if selected is None or not selected.data(Qt.ItemDataRole.UserRole):
            return
        if QMessageBox.question(
            dialog, "Restore this snapshot?",
            "Replace saved settings and user data with this snapshot?\n"
            "A safety snapshot of the current data will be written first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            restore_for_app(app, selected.data(Qt.ItemDataRole.UserRole))
        except Exception:
            logger.exception("[BACKUP] Restore or reload failed")
            QMessageBox.critical(dialog, "Restore incomplete",
                                 "Restore or reload failed. Your safety snapshot is available. "
                                 "Reopen Settings before making changes.")
            if parent is not None:
                parent.close()
            dialog.reject()
            return
        QMessageBox.information(dialog, "Settings restored",
                                "Settings were restored and reloaded. Restart Samsara later "
                                "to refresh other open pages and personal plugins.")
        if parent is not None:
            parent.close()
        dialog.accept()

    restore.clicked.connect(apply)
    dialog.exec()
