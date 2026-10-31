"""Spoken Stremio seek -> exact AHK body. All key execution is stubbed."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import stremio
from samsara import plugin_commands
from samsara.command_registry import CommandMatcher, DispatchState
from samsara.command_scope import MatchContext

sc = stremio.stremio_control
PLAYER = MatchContext.for_app("stremio-shell-ng.exe")


@pytest.fixture
def matcher():
    plugin_commands._reinstall_module_commands(stremio)
    registry = {key: entry for key, entry in plugin_commands._REGISTRY.items()
                if entry["source"] == stremio.__name__}
    matcher = CommandMatcher()
    matcher.load_plugins(registry)
    matcher.freeze()
    return matcher


@pytest.fixture
def run_ahk(monkeypatch):
    run = Mock(return_value=True)
    monkeypatch.setattr(sc, "_run_ahk", run)
    return run


@pytest.fixture
def app():
    return SimpleNamespace(_show_outcome_chip=Mock(),
                           audio_coordinator=SimpleNamespace(speak=Mock()))


@pytest.mark.parametrize("phrase,body,seconds", [
    ("skip forward 30 seconds", "{Right 3}", 30),
    ("skip back two minutes", "{Left 12}", 120),
    ("forward ten", "{Right 1}", 10),
    ("fast forward by 10 seconds", "{Right 1}", 10),
    ("jump ahead a couple minutes", "{Right 12}", 120),
    ("skip ahead half a minute", "{Right 3}", 30),
    ("rewind thirty seconds", "{Left 3}", 30),
    ("skip back a minute", "{Left 6}", 60),
    ("back a minute", "{Left 6}", 60),
    ("back 30", "{Left 3}", 30),
    ("forward 30", "{Right 3}", 30),
    ("skip forward", "{Right 1}", 10),
    ("skip back", "{Left 1}", 10),
    ("skip forward stremio", "{Right 1}", 10),
    ("forward twenty-five seconds", "{Right 3}", 30),
    ("back one second", "{Left 1}", 10),
    ("forward 999999 minutes", "{Right 180}", 1800),
    ("back thirty one minutes", "{Left 180}", 1800),
    ("forward one hundred and twenty seconds", "{Right 12}", 120),
])
def test_phrase_to_single_ahk_body(matcher, run_ahk, app, phrase, body, seconds):
    entry, remainder = matcher.match(phrase, PLAYER)
    assert entry is not None
    assert entry.handler(app, remainder) is True
    run_ahk.assert_called_once()
    assert [line for line in run_ahk.call_args.args[0].splitlines()
            if line.startswith("Send,")] == [f"Send, {body}"]
    direction = "Forward" if "Right" in body else "Back"
    app.audio_coordinator.speak.assert_called_once_with(
        f"{direction} {seconds} seconds", category="command")


@pytest.mark.parametrize("suffix", ["zero", "-10 seconds", "ten bananas", "ten two",
                                        "a minute and open browser", "nan", "inf", "by"])
def test_invalid_amount_never_sends_keys(suffix, run_ahk, app):
    result = stremio.handle_skip_forward(app, suffix)
    assert result.state is DispatchState.FAILED
    run_ahk.assert_not_called()


@pytest.mark.parametrize("phrase", ["next episode", "next video", "play the next episode", "skip episode"])
def test_next_episode_verified_shift_n(matcher, run_ahk, app, phrase):
    entry, remainder = matcher.match(phrase, PLAYER)
    assert entry.handler(app, remainder) is True
    run_ahk.assert_called_once()
    assert "Send, +{n}" in run_ahk.call_args.args[0]


def test_no_guessed_previous_shortcut(matcher, run_ahk):
    assert matcher.match("previous episode", PLAYER)[0] is None
    run_ahk.assert_not_called()


@pytest.mark.parametrize("phrase", ["forward 30", "back a minute", "next episode", "skip back two minutes"])
@pytest.mark.parametrize("exe", ["notepad.exe", "spotify.exe"])
def test_new_phrases_require_stremio_foreground(matcher, phrase, exe):
    assert matcher.match(phrase, MatchContext.for_app(exe))[0] is None


def test_legacy_player_process_is_supported(matcher):
    assert matcher.match("forward ten", MatchContext.for_app("stremio.exe"))[0] is not None


@pytest.mark.parametrize("handler", [stremio.handle_skip_forward, stremio.handle_skip_back,
                                     stremio.handle_next_episode])
def test_ahk_failure_is_failed_not_a_routing_miss(handler, run_ahk, app):
    run_ahk.return_value = False
    assert handler(app, "").state is DispatchState.FAILED
    assert app.audio_coordinator.speak.call_args.kwargs["category"] == "error"


def test_declared_arguments_pass_whole_sentence_gate(matcher):
    from samsara.commands import CommandExecutor
    executor = CommandExecutor.__new__(CommandExecutor)
    executor._matcher = matcher
    executor._matcher_lock = threading.RLock()
    for phrase in ("skip forward", "skip back"):
        entry, _ = matcher.match(phrase, PLAYER)
        assert executor._takes_argument(entry)
    entry, _ = matcher.match("next episode", PLAYER)
    assert not executor._takes_argument(entry)


def test_direct_control_caps_cannot_be_bypassed(run_ahk):
    sc.skip_forward(1000000)
    assert "Send, {Right 180}" in run_ahk.call_args.args[0]
    with pytest.raises(ValueError):
        sc.skip_back(float("nan"))


def test_brief_feedback_uses_existing_policy():
    from samsara.tts.coordinator import _feedback_decision
    assert _feedback_decision("brief", "command") == "action_complete"
    assert _feedback_decision("silent", "command") == "skip"
    assert _feedback_decision("full", "command") == "speak"


@pytest.fixture(scope="module")
def production_matcher():
    from tools.dump_command_metadata import build_executor
    matcher = build_executor()._matcher
    # Stremio is opt-in; enable it like the owner's configured pack.
    matcher.set_enabled_packs(set(matcher._enabled_packs or ()) | {"stremio"})
    return matcher


@pytest.mark.parametrize("phrase,canonical", [
    ("back a minute", "skip back"), ("back 30", "skip back"),
    ("skip back two minutes", "skip back"),
    ("jump ahead thirty seconds", "skip forward"),
    ("play the next episode", "next episode"),
])
def test_production_registry_stremio_wins_in_foreground(production_matcher, phrase, canonical):
    entry, _ = production_matcher.match(phrase, PLAYER)
    assert entry is not None and entry.phrase == canonical


def test_production_registry_preserves_browser_back(production_matcher):
    entry, _ = production_matcher.match("back", MatchContext.for_app("brave.exe"))
    assert entry is not None and entry.phrase == "go back"


@pytest.mark.parametrize("phrase", ["back a minute", "back 30"])
def test_production_amount_forms_send_exact_keys(production_matcher, run_ahk, app, phrase):
    entry, remainder = production_matcher.match(phrase, PLAYER)
    assert entry.phrase == "skip back"
    assert entry.handler(app, remainder) is True
    body = "{Left 6}" if phrase == "back a minute" else "{Left 3}"
    run_ahk.assert_called_once()
    assert f"Send, {body}" in run_ahk.call_args.args[0]


@pytest.mark.parametrize("phrase", ["back zero", "back -30", "back ten bananas",
                                   "back a minute and open browser"])
def test_back_amount_prefix_rejects_invalid_durations(matcher, run_ahk, phrase):
    assert matcher.match(phrase, PLAYER)[0] is None
    run_ahk.assert_not_called()


def test_back_amount_prefix_respects_disabled_pack(matcher):
    matcher.set_enabled_packs({"core"})
    assert matcher.match("back a minute", PLAYER)[0] is None


@pytest.mark.parametrize("phrase", ["go back", "go forward"])
def test_browser_navigation_aliases_are_not_stremio_commands(production_matcher, phrase):
    assert production_matcher.match(phrase, PLAYER)[0] is None
    entry, _ = production_matcher.match(phrase, MatchContext.for_app("brave.exe"))
    assert entry is not None and entry.phrase == phrase
