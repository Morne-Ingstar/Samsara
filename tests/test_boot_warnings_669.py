"""Config startup round trips and pixel-font menu buttons, without bootstrap."""
import ast
import copy
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import statistics
import sys
import threading
import time

import pytest


@pytest.fixture(scope="module")
def config_methods():
    """Compile only the real config methods, never execute dictation imports."""
    root = Path(__file__).resolve().parents[1]
    source = root / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    names = {
        "load_config", "save_config", "_quarantine_corrupt_config", "_config_backups_dir",
        "_rotate_config_backup", "_prune_config_backups", "_deep_update",
        "_wake_word_config_already_migrated", "_migrate_wake_word_config",
        "_migrate_recording_media_mode", "_migrate_command_matching_enabled_flag",
        "_migrate_ai_command_mode_config", "_migrate_retired_ava_command_session_config",
    }
    methods = [n for n in app.body if isinstance(n, ast.FunctionDef) and n.name in names]
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_three_way_merge"]
    from samsara import wake_profiles
    from samsara.onboarding import display_placement
    from samsara.paths import samsara_home_dir
    namespace = dict(copy=copy, json=json, logger=logging.getLogger(__name__), Path=Path,
                     datetime=datetime, os=os, shutil=shutil, time=time, wake_profiles=wake_profiles,
                     display_placement=display_placement, samsara_home_dir=samsara_home_dir,
                     safe_exception=lambda log, message: log.error(message), describe_text=str,
                     _MISSING=object())
    exec(compile(ast.Module(body=functions + methods, type_ignores=[]), str(source), "exec"), namespace)
    for filename, name in (("key_macros.py", "get_default_macro_config"),
                           ("notifications.py", "get_default_notification_config")):
        helper_source = root / "samsara" / filename
        module = ast.parse(helper_source.read_text(encoding="utf-8-sig"))
        function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(helper_source), "exec"), namespace)
    cls = type("ConfigMethods", (), {n.name: namespace[n.name] for n in methods})
    cls._CONFIG_BACKUP_DIRNAME = "config_backups"
    cls._CONFIG_BACKUP_KEEP = 15
    return cls, namespace


@pytest.fixture
def loader(config_methods, tmp_path, monkeypatch):
    cls, namespace = config_methods
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    app = cls()
    app.config_path = tmp_path / "config.json"
    app._config_lock = threading.Lock()
    from samsara.config_seed import build_default_config
    defaults = build_default_config(home=tmp_path,
                                    macro_defaults=namespace["get_default_macro_config"](),
                                    notification_defaults=namespace["get_default_notification_config"]())
    app.config_path.write_text(json.dumps(defaults), encoding="utf-8")
    app.writes = []
    real_save = app.save_config

    def save():
        if not getattr(app, "_config_migration_batch", False):
            app.writes.append(copy.deepcopy(app.config))
        return real_save()

    app.save_config = save
    return app, defaults


def test_all_legacy_migrations_persist_once_and_preserve_original_backup(loader):
    app, original = loader
    original.pop("wake_word_config")
    original.pop("wake_profiles")
    original["mode"] = "wake_word"
    original["wake_word"] = "activate assistant"
    original["command_mode_enabled"] = True
    original["command_mode"]["command_matching_enabled"] = False
    original["ai_command_mode"] = {"enabled": True, "model": "legacy-model", "key": "f20"}
    original.pop("ava_command_session", None)
    original["ducking"].pop("recording_mode")
    original["ava_edit"] = {"timeout_s": 19}
    original["extension"] = {"custom": [1, 2, 3]}
    app.config_path.write_text(json.dumps(original), encoding="utf-8")
    with app._config_lock:
        app.load_config()
    assert len(app.writes) == 1
    disk = json.loads(app.config_path.read_text(encoding="utf-8"))
    assert disk["mode"] == "hold" and disk["wake_word_enabled"] is True
    assert disk["wake_word_config"]["phrase"] == "activate assistant"
    assert disk["command_mode"]["command_matching_enabled"] is False
    assert "command_mode_enabled" not in disk and "ai_command_mode" not in disk
    assert disk["ava_command_session"]["model"] == "legacy-model"
    assert disk["ava_command_session"]["plain_requests_migrated"] is True
    assert "key" not in disk["ava_command_session"]
    assert disk["ducking"]["recording_mode"] == "duck"
    assert disk["ava_edit"] == {"timeout_seconds": 19}
    assert "pitch" not in disk["tts"] and "enabled" not in disk["audio_coordinator"]
    assert disk["extension"] == original["extension"]
    assert json.loads(app.config_path.with_suffix(".json.bak").read_text()) == original
    backups = list((app.config_path.parent / "config_backups").glob("config-*.json"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == original


def test_settings_save_does_not_reintroduce_retired_key_or_repeat_migration(loader):
    app, _ = loader
    with app._config_lock:
        app.load_config()
        assert "streaming_hotkey" not in app.config
        app.config["language"] = "fr"
        app.save_config()
        before = app.config_path.read_bytes()
        app.writes.clear()
        app.load_config()
    assert app.writes == []
    assert app.config_path.read_bytes() == before
    assert app.config["language"] == "fr"
    assert app._config_last_disk_snapshot == app.config
    app.config["wake_word_config"]["phrase"] = "changed"
    assert app._config_last_disk_snapshot["wake_word_config"]["phrase"] != "changed"


@pytest.mark.parametrize("failure", ["migration", "write"])
def test_failed_migration_batch_restores_save_flags_and_original(loader, monkeypatch, failure):
    app, _ = loader
    before = app.config_path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic failure")

    if failure == "migration":
        monkeypatch.setattr(app, "_migrate_recording_media_mode", fail)
    else:
        monkeypatch.setattr(json, "dump", fail)
    with app._config_lock, pytest.raises(OSError, match="synthetic failure"):
        app.load_config()
    assert app._config_migration_save is False
    assert app._config_migration_batch is False
    assert app._config_migration_pending is False
    assert app.config_path.read_bytes() == before


def test_corrupt_config_stays_quarantined_and_save_latch_blocks_defaults(loader):
    app, _ = loader
    app.config_path.write_text("{invalid", encoding="utf-8")
    with app._config_lock:
        app.load_config()
        app.save_config()
    assert app._config_load_failed is True
    assert not app.config_path.exists()
    quarantined = list(app.config_path.parent.glob("config.corrupt-*.json"))
    assert len(quarantined) == 1 and quarantined[0].read_text() == "{invalid"


def test_synthetic_config_load_budget_includes_disk_migrations_and_snapshot(loader):
    app, cfg = loader
    cfg["web_shortcuts"].update({f"project {i}": f"https://example.invalid/{i}" for i in range(250)})
    cfg["extension"] = {f"entry_{i}": {"enabled": bool(i % 2), "value": "x" * 80} for i in range(250)}
    cfg["command_mode_enabled"] = True
    cfg["ai_command_mode"] = {"enabled": True, "model": "legacy-model", "key": "f20"}
    cfg["ducking"].pop("recording_mode")
    app.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    durations = []
    with app._config_lock:
        started = time.perf_counter()
        app.load_config()
        migration_ms = (time.perf_counter() - started) * 1000
        assert len(app.writes) == 1
        app.save_config()
        app.writes.clear()
        for _ in range(5):
            started = time.perf_counter()
            app.load_config()
            durations.append((time.perf_counter() - started) * 1000)
    assert statistics.median(durations) < 300, durations
    assert max([migration_ms] + durations) < 600, (migration_ms, durations)
    assert not app.writes


def test_themed_pixel_font_menu_button_renders_without_point_size_warning(qapp):
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QMenu, QPushButton
    from samsara.ui import theme
    from samsara.ui.design_scale import _button_qss

    messages = []
    previous = qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    button = QPushButton("Menu")
    try:
        theme.prepare_menu_button(button)
        if sys.platform == "win32":
            assert button.style().objectName().lower() == "fusion"
        button.setFont(theme.qfont(theme.TYPE_BODY))
        assert button.font().pointSize() == -1
        menu = QMenu(button)
        menu.addAction("Item")
        button.setMenu(menu)
        button.setStyleSheet(_button_qss(False))
        button.ensurePolished()
        button.grab()
        qapp.processEvents()
    finally:
        button.deleteLater()
        qapp.processEvents()
        qInstallMessageHandler(previous)
    assert not [message for message in messages
                if "QFont::" in message or "DpiAwareness" in message]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native menu font regression")
def test_settings_menu_buttons_all_use_pixel_safe_style(qapp, monkeypatch):
    from PySide6.QtCore import qInstallMessageHandler
    from PySide6.QtWidgets import QPushButton
    from samsara import autostart
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp

    monkeypatch.setattr(autostart, "sync_run_entry", lambda *args, **kwargs: None)
    messages = []
    previous = qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    window = None
    try:
        window = _SettingsWindow(_StubApp())
        for name in ("General", "Audio", "Help"):
            window.show_tab(name)
        buttons = [button for button in window.findChildren(QPushButton) if button.menu()]
        assert {button.text() for button in buttons} >= {"Tools", "Recover window", "Preview", "Get help"}
        for button in buttons:
            # Inherited QSS wraps the base style; exercise its metrics/paint.
            button.ensurePolished()
            button.grab()
        qapp.processEvents()
    finally:
        if window is not None:
            window.deleteLater()
        qapp.processEvents()
        qInstallMessageHandler(previous)
    assert not [message for message in messages
                if "QFont::" in message or "DpiAwareness" in message]
