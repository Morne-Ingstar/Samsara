"""Real preview launch plumbing and missing-key defaults, without app bootstrap."""

import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import dictation
from samsara import app_restart
from samsara.alarms import AlarmManager, get_default_alarm_config
from samsara.config_defaults import DEFAULTS
from samsara.hotkeys import HotkeyCluster
from samsara.live_surface.partials import cpu_final_only
from samsara.onboarding.guidance_facts import facts_from_config
from samsara.smart_actions_bridge import SmartActionsBridge
from samsara.support_bundle import _version_text
from samsara.transcription_params import get_transcription_params


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_preview_calls_real_relaunch_builder(tmp_path, monkeypatch, frozen):
    executable = str(tmp_path / ("Samsara.exe" if frozen else "python.exe"))
    monkeypatch.setattr(app_restart.sys, "executable", executable)
    monkeypatch.setattr(app_restart.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(app_restart.sys, "argv", ["launcher", "--restart-helper", "ignored"])
    primary = tmp_path / "primary"
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(primary))
    monkeypatch.setattr(dictation, "_reap_old_preview_profiles", Mock())
    popen = Mock()
    monitor = Mock()
    monkeypatch.setattr(dictation.subprocess, "Popen", popen)
    monkeypatch.setattr(dictation.thread_registry, "spawn", monitor)
    failure = Mock(side_effect=AssertionError("preview launch failed"))
    monkeypatch.setattr(dictation, "_show_preview_failure", failure)

    # No patch of the builder or import: this catches the original ImportError.
    inspect.unwrap(dictation.DictationApp.preview_first_run)(
        object.__new__(dictation.DictationApp)
    )

    popen.assert_called_once()
    args, kwargs = popen.call_args
    script = Path(dictation.__file__).resolve()
    assert args == ([executable] if frozen else [executable, str(script)],)
    assert kwargs["cwd"] == str(Path(executable).parent if frozen else script.parent)
    assert os.environ["SAMSARA_HOME_DIR"] == str(primary)
    profile = Path(kwargs["env"]["SAMSARA_HOME_DIR"])
    assert profile != primary and profile.is_dir()
    assert (profile / dictation._PREVIEW_DIAGNOSTIC_NAME).is_file()
    assert kwargs["stdout"] is kwargs["stderr"] and kwargs["stdout"].closed
    monitor.assert_called_once()
    failure.assert_not_called()


@pytest.mark.parametrize("config", [{}, {"intent": {}}, {"intent": {"shadow_enabled": False}}])
def test_absent_shadow_setting_stays_off(config):
    observer = Mock()
    app = SimpleNamespace(config=config, _intent_shadow=observer)
    dictation.DictationApp._intent_shadow_observe(app, "", SimpleNamespace(kind="dictate_staged"))
    observer.observe.assert_not_called()


def test_explicit_shadow_setting_still_enables_observer():
    observer = Mock()
    app = SimpleNamespace(config={"intent": {"shadow_enabled": True}}, _intent_shadow=observer)
    outcome = SimpleNamespace(kind="dictate_staged")
    dictation.DictationApp._intent_shadow_observe(app, "", outcome)
    observer.observe.assert_called_once_with("", "dictate_staged")


def test_absent_performance_mode_uses_canonical_decode_profile():
    kwargs = dict(language=None, initial_prompt="", no_speech_threshold=0.6, log_prob_threshold=-1)
    assert get_transcription_params({}, **kwargs) == get_transcription_params(
        {"performance_mode": DEFAULTS["performance_mode"]}, **kwargs
    )
    assert get_transcription_params({}, **kwargs)["beam_size"] == 5


def test_alarm_scaffolding_and_reader_default_off():
    assert get_default_alarm_config()["enabled"] is False
    manager = object.__new__(AlarmManager)
    manager.get_config = lambda: {"alarms": {}}
    assert manager.enabled is False


def test_enabling_alarm_with_absent_flag_resets_interval(monkeypatch):
    config = {"alarms": {"items": [{"id": "sample", "enabled": True}]}}
    manager = object.__new__(AlarmManager)
    manager.get_config = lambda: config
    manager.save_config = Mock()
    manager.running = True
    manager.last_triggered = {}
    monkeypatch.setattr("samsara.alarms.time.time", lambda: 123.0)
    manager.set_global_enabled(True)
    assert manager.last_triggered == {"sample": 123.0}
    assert config["alarms"]["enabled"] is True
    manager.save_config.assert_called_once()


def test_cpu_policy_uses_canonical_device_and_honours_resolved_cpu():
    app = SimpleNamespace(config={})
    assert cpu_final_only(app) == cpu_final_only(SimpleNamespace(config={"device": DEFAULTS["device"]}))
    assert cpu_final_only(app) is False
    app.device_type = "cpu"
    assert cpu_final_only(app) is True
    assert cpu_final_only(SimpleNamespace(config={"device": "cpu"})) is True


def test_mouse_bindings_follow_canonical_hotkey_without_mutating_config(monkeypatch):
    monkeypatch.setitem(DEFAULTS, "hotkey", "mouse5")
    app = SimpleNamespace(config={})
    assert HotkeyCluster._mouse_hook_bindings(app) == (frozenset({"mouse5"}), frozenset({"mouse5"}))
    assert app.config == {}
    app.config["hotkey"] = ""
    assert HotkeyCluster._mouse_hook_bindings(app) == (frozenset(), frozenset())


def test_missing_wake_profiles_agree_in_guidance_and_matching():
    implicit = facts_from_config({})
    explicit = facts_from_config({"wake_profiles": DEFAULTS["wake_profiles"]})
    assert implicit.wake_profile_send_words == explicit.wake_profile_send_words
    # 940: 919 ships no personal profiles; explicit profiles still match.
    assert implicit.wake_profile_send_words == ()
    assert DEFAULTS["wake_profiles"] == []
    profile = {"id": "synthetic", "phrase": "okay helper", "enabled": True}
    app = SimpleNamespace(config={})
    assert dictation.DictationApp._check_wake_profiles(
        app, profile["phrase"], session_open=True
    ) is None
    app.config["wake_profiles"] = [profile]
    assert dictation.DictationApp._check_wake_profiles(
        app, profile["phrase"], session_open=True
    ) == profile
    app.config["wake_profiles"] = []
    assert dictation.DictationApp._check_wake_profiles(app, profile["phrase"], session_open=True) is None
    assert facts_from_config(app.config).wake_profile_send_words == ()


def test_absent_support_facts_stay_unknown():
    text = _version_text({})
    assert "Speech model: unknown\n" in text
    assert "Speech compute type: unknown\n" in text
    explicit = _version_text({"model_size": "base", "compute_type": "int8"})
    assert "Speech model: base\n" in explicit
    assert "Speech compute type: int8\n" in explicit


def test_webhook_and_cli_timeout_defaults_keep_separate_contracts():
    assert DEFAULTS["agent_channel.timeout_s"] == 240
    assert DEFAULTS["smart_actions.timeout_s"] == 30
    assert SmartActionsBridge({})._timeout_s == DEFAULTS["smart_actions.timeout_s"]
    assert SmartActionsBridge({"timeout_s": 7})._timeout_s == 7
