"""Exercise the shared config writer against a disposable profile."""

import ast
import builtins
import copy
from datetime import datetime
import errno
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from types import MethodType, SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def writer_code():
    path = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "DictationApp")
    names = {"_three_way_merge", "save_config", "persist_config",
             "_config_backups_dir", "_rotate_config_backup", "_prune_config_backups"}
    methods = [node for node in tree.body + app_class.body
               if isinstance(node, ast.FunctionDef) and node.name in names]
    return compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec")


@pytest.fixture
def app(tmp_path, writer_code):
    # Run the real methods with an isolated profile, without bootstrapping dictation.
    namespace = dict(
        copy=copy, datetime=datetime, json=json, logger=logging.getLogger("Samsara"),
        os=os, Path=Path, shutil=shutil, time=time, samsara_home_dir=lambda: tmp_path,
        _MISSING=object(), describe_text=str,
        safe_exception=lambda log, message: log.exception(message),
    )
    exec(writer_code, namespace)
    config = {"microphone": 1, "microphone_name": "First microphone", "ui": {"hints": True}}
    app = SimpleNamespace(
        config_path=tmp_path / "config.json", config=config,
        _config_last_disk_snapshot=copy.deepcopy(config), _config_lock=threading.Lock(),
        _CONFIG_BACKUP_DIRNAME="config_backups", _CONFIG_BACKUP_KEEP=15,
    )
    app.config_path.write_text(json.dumps(config), encoding="utf-8")
    for name in ("save_config", "persist_config", "_config_backups_dir",
                 "_rotate_config_backup", "_prune_config_backups"):
        setattr(app, name, MethodType(namespace[name], app))
    return app


def read_config(path):
    return json.loads(path.read_text(encoding="utf-8"))


def deny_replacement_with_disk_full_on_overwrite(monkeypatch, app, prefix_length):
    attempts = []
    real_replace = os.replace

    def deny_replace(source, target, *args, **kwargs):
        if Path(target) == app.config_path:
            attempts.append(Path(source))
            raise PermissionError(errno.EACCES, "simulated Windows sharing violation")
        return real_replace(source, target, *args, **kwargs)

    def fail_after_open(path, mode="r", *args, **kwargs):
        if Path(path) == app.config_path and "w" in mode:
            with builtins.open(path, mode, *args, **kwargs) as handle:
                if prefix_length:
                    staged_text = attempts[-1].read_text(encoding="utf-8")
                    handle.write(staged_text[:prefix_length])
                    handle.flush()
                raise OSError(errno.ENOSPC, "simulated full disk after opening primary")
        return builtins.open(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "replace", deny_replace)
    monkeypatch.setitem(app.save_config.__globals__, "open", fail_after_open)
    return attempts


@pytest.mark.parametrize("prefix_length", [0, 19], ids=["empty", "partial"])
def test_denied_replace_preserves_primary_and_allows_retry(app, monkeypatch, prefix_length):
    original = app.config_path.read_bytes()
    last_good = copy.deepcopy(app.config)
    app.config["ui"]["hints"] = False

    with monkeypatch.context() as faults:
        attempts = deny_replacement_with_disk_full_on_overwrite(faults, app, prefix_length)
        with pytest.raises(OSError) as error:
            app.persist_config()

    assert app.config_path.read_bytes() == original
    assert read_config(app.config_path) == app._config_last_disk_snapshot == last_good
    assert error.value.errno == errno.EACCES
    assert len(attempts) == 3
    staged = attempts[0]
    assert attempts == [staged] * 3
    assert staged.parent == app.config_path.parent
    assert read_config(staged) == app.config
    assert app.config_path.with_suffix(".json.bak").read_bytes() == original
    backup, = app._config_backups_dir().glob("config-*.json")
    assert backup.read_bytes() == original

    failed_draft = staged.read_bytes()
    app.config["ui"]["theme"] = "dark"
    app.persist_config()

    assert read_config(app.config_path) == app.config == app._config_last_disk_snapshot
    assert staged.read_bytes() == failed_draft
    assert list(app.config_path.parent.glob("*.tmp")) == [staged]


def test_failed_second_save_preserves_the_committed_microphone(app, monkeypatch):
    before_apply = app.config_path.read_bytes()
    app.config.update(microphone=2, microphone_name="Second microphone")
    app.persist_config()
    mic_only = copy.deepcopy(app.config)
    saved_mic_bytes = app.config_path.read_bytes()
    app.config["ui"]["hints"] = False

    attempts = deny_replacement_with_disk_full_on_overwrite(monkeypatch, app, prefix_length=19)
    with pytest.raises(OSError):
        app.persist_config()

    assert app.config_path.read_bytes() == saved_mic_bytes
    assert read_config(app.config_path) == app._config_last_disk_snapshot == mic_only
    assert len(attempts) == 3
    assert app.config_path.with_suffix(".json.bak").read_bytes() == saved_mic_bytes
    assert {path.read_bytes() for path in app._config_backups_dir().glob("config-*.json")} == {
        before_apply, saved_mic_bytes,
    }


@pytest.mark.parametrize("fault", ["serialize", "fsync"])
def test_disk_full_during_staging_preserves_primary(app, monkeypatch, fault):
    original = app.config_path.read_bytes()

    def fail_sync(fd):
        raise OSError(errno.ENOSPC, "simulated full disk during fsync")

    def fail_dump(config, handle, **kwargs):
        handle.write('{"microphone": ')
        raise OSError(errno.ENOSPC, "simulated full disk during serialization")

    if fault == "serialize":
        monkeypatch.setattr(json, "dump", fail_dump)
    else:
        monkeypatch.setattr(os, "fsync", fail_sync)
    app.config["microphone"] = 2

    with pytest.raises(OSError, match="full disk"):
        app.persist_config()

    assert app.config_path.read_bytes() == original
    assert read_config(app.config_path) == app._config_last_disk_snapshot
    assert not list(app.config_path.parent.glob("*.tmp"))


def test_normal_save_flushes_and_syncs_before_replacement(app, monkeypatch):
    app.config["ui"]["hints"] = False
    expected = copy.deepcopy(app.config)
    real_fsync = os.fsync
    synced = []

    def check_flushed_temp(fd):
        staged, = app.config_path.parent.glob("*.tmp")
        assert read_config(staged) == expected
        assert read_config(app.config_path) == app._config_last_disk_snapshot
        real_fsync(fd)
        synced.append(staged)

    monkeypatch.setattr(os, "fsync", check_flushed_temp)
    app.persist_config()

    assert len(synced) == 1
    assert read_config(app.config_path) == app.config == app._config_last_disk_snapshot == expected
    assert not list(app.config_path.parent.glob("*.tmp"))
