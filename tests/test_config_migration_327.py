"""Migration round trips through a synthetic on-disk config."""
import ast
import copy
import json
import logging
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from samsara import wake_profiles
from samsara import cuda_detect


@pytest.fixture
def loader(tmp_path):
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "DictationApp")
    methods = {node.name: node for node in app_class.body if isinstance(node, ast.FunctionDef)}
    names = ("_migrate_wake_word_config", "_wake_word_config_already_migrated",
             "_deep_update", "_migrate_command_matching_enabled_flag")
    namespace = {"copy": copy, "wake_profiles": wake_profiles,
                 "logger": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(
        body=[copy.deepcopy(methods[name]) for name in names], type_ignores=[])),
        "<config-migration-production-ast>", "exec"), namespace)
    path = tmp_path / "config.json"
    defaults = {"wake_profiles": [], "wake_word_config": {
        "wake_abort_phrase": ["cancel", "abort"], "send_words": ["over"]}}

    def load():
        instance = SimpleNamespace(config=json.loads(path.read_text(encoding="utf-8")))
        instance.save_config = lambda: path.write_text(json.dumps(instance.config), encoding="utf-8")
        for name in names:
            setattr(instance, name, MethodType(namespace[name], instance))
        instance._migrate_wake_word_config(defaults)
        instance._migrate_command_matching_enabled_flag()
        instance.save_config()
        return instance.config

    return path, load


def test_cancel_words_rename_survives_disk_reload(loader):
    path, load = loader
    path.write_text(json.dumps({"wake_profiles": [], "wake_word_config": {
        "cancel_words": ["custom cancellation phrase"]}}), encoding="utf-8")
    migrated = load()
    reloaded = load()
    assert migrated == reloaded
    assert reloaded["wake_word_config"]["wake_abort_phrase"] == ["custom cancellation phrase"]
    assert "cancel_words" not in reloaded["wake_word_config"]


def test_explicit_new_cancel_words_win_and_legacy_removed(loader):
    path, load = loader
    path.write_text(json.dumps({"wake_profiles": [], "wake_word_config": {
        "wake_abort_phrase": ["chosen"], "cancel_words": ["stale"]}}), encoding="utf-8")
    assert load()["wake_word_config"]["wake_abort_phrase"] == ["chosen"]
    assert "cancel_words" not in load()["wake_word_config"]


def test_explicit_command_matching_choice_survives_disk_reload(loader):
    path, load = loader
    path.write_text(json.dumps({"wake_profiles": [], "wake_word_config": {},
                                "command_mode_enabled": True,
                                "command_mode": {"command_matching_enabled": False}}), encoding="utf-8")
    migrated = load()
    assert migrated["command_mode"]["command_matching_enabled"] is False
    assert "command_mode_enabled" not in migrated
    assert load() == migrated


@pytest.mark.parametrize("device_count,pack_available,expected", [
    (1, True, "cuda"), (0, True, "cpu"), (1, False, "cpu"),
])
def test_auto_device_uses_available_cuda_device(monkeypatch, device_count, pack_available, expected):
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "DictationApp")
    method = next(node for node in app_class.body if isinstance(node, ast.FunctionDef)
                  and node.name == "load_model_async")
    devices = []
    namespace = {
        "logger": logging.getLogger(__name__), "time": time,
        "thread_registry": SimpleNamespace(spawn=lambda name, fn, **kwargs: fn()),
        "_create_whisper_model": lambda *args, **kwargs: devices.append(kwargs["device"]) or object(),
        "smart_corrections_is_enabled": lambda config: False,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 "<config-migration-production-ast>", "exec"), namespace)
    monkeypatch.setattr(cuda_detect, "is_cuda_available", lambda: pack_available)
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(
        get_cuda_device_count=lambda: device_count,
        get_supported_compute_types=lambda device: {"float16", "int8_float16", "float32"}))
    instance = SimpleNamespace(
        config={"device": "auto", "model_size": "base", "wake_word_enabled": False,
                "mode": "hold", "gesture": {"enabled": False}},
        update_splash=lambda *args, **kwargs: None,
        _schedule_ui=lambda callback, *args: callback(*args),
        _show_startup_error=lambda error: None, _load_vad_model=lambda: None,
        wake_ready_state=lambda: "disabled", _write_last_known_good=lambda: None,
    )
    MethodType(namespace["load_model_async"], instance)()
    assert devices == [expected]
