"""Queue 46 item 3: what the MIGRATE pass costs on a settled config.

Times DictationApp.load_config() on a temp copy of the live config.json (temp
SAMSARA_HOME_DIR, never the live profile) with the three migrations real vs
stubbed to no-ops, and counts save_config() writes. Prints one JSON line.

    F:\\envs\\sami\\python.exe perf_artifacts\\config_migration_cost.py
"""
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="samsara_migcost_"))
shutil.copy2(Path.home() / ".samsara" / "config.json", HOME / "config.json")
os.environ["SAMSARA_HOME_DIR"] = str(HOME)
sys.path.insert(0, str(REPO))

import dictation  # noqa: E402

N = 30


def _app():
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config_path = HOME / "config.json"
    app._config_lock = threading.Lock()
    app._config_last_disk_snapshot = {}
    app.saves = 0
    real_save = app.save_config

    def counting_save(*a, **k):
        app.saves += 1
        return real_save(*a, **k)

    app.save_config = counting_save
    return app


def _time(stub_migrations: bool):
    samples, saves = [], []
    for _ in range(N):
        app = _app()
        if stub_migrations:
            app._migrate_wake_word_config = lambda default_config: None
            app._migrate_command_matching_enabled_flag = lambda: None
            app._migrate_ai_command_mode_config = lambda: None
        t0 = time.perf_counter()
        with app._config_lock:
            app.load_config()
        samples.append((time.perf_counter() - t0) * 1000)
        saves.append(app.saves)
    return {"median_ms": round(statistics.median(samples), 2), "min_ms": round(min(samples), 2),
            "max_ms": round(max(samples), 2), "saves_per_load": max(saves)}


def _migration_only():
    app = _app()
    with app._config_lock:
        app.load_config()
    samples = []
    for _ in range(200):
        t0 = time.perf_counter()
        app._wake_word_config_already_migrated()
        app._migrate_command_matching_enabled_flag()
        app._migrate_ai_command_mode_config()
        samples.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": round(statistics.median(samples), 4), "max_ms": round(max(samples), 4),
            "wake_already_migrated": app._wake_word_config_already_migrated()}


try:
    result = {
        "legacy_keys_present": {k: k in json.loads((HOME / "config.json").read_text(encoding="utf-8"))
                                for k in ("wake_targets", "command_mode_enabled", "ai_command_mode")},
        "load_config_real_migrations": _time(False),
        "load_config_migrations_stubbed": _time(True),
        "migration_checks_only": _migration_only(),
    }
    print(json.dumps(result))
finally:
    shutil.rmtree(HOME, ignore_errors=True)
