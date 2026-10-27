"""943: offline discovery, real policy dispatch, catalog ownership, literal prose.

No dictation import, hardware, app bootstrap, personal files or child processes.
"""
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import help_intents as hi, execution_policy as policy
from samsara.command_registry import DispatchState
from samsara.command_scope import MatchContext
from samsara.samsara_navigation import PLACES


# Independent everyday examples, not generated from the implementation frames.
EXAMPLES = [
    ("how do I see the commands", "reference"),
    ("how can I see what I can say", "reference"),
    ("where's the list of things I can say", "reference"),
    ("what can this do", "reference"),
    ("I can't find the cheat sheet", "reference"),
    ("show me the commands", "reference"),
    ("help me find a list of commands", "reference"),
    ("is there a way to find out what this can do", "reference"),
    ("where do I change the microphone", "microphone"),
    ("how do I change my mic", "microphone"),
    ("help me choose another microphone", "microphone"),
    ("I want to test my microphone", "microphone_guide"),
    ("it can't hear me", "voice_help"),
    ("help it can't hear me", "voice_help"),
    ("how do I make it hear me", "voice_help"),
    ("how do I make it stop talking", "speech"),
    ("how can I turn off the voice", "speech"),
    ("help me stop spoken replies", "speech"),
    ("where do I turn the voice back on", "speech"),
    ("how do I start over the tutorial", "tutorial"),
    ("help me learn how to use this", "tutorial"),
    ("I want to do the tutorial again", "tutorial"),
    ("where's the beginner lessons", "tutorial"),
    ("how can I set up the wake word", "wake_word"),
    ("what's the wake word", "wake_word"),
    ("help with wake activation", "wake_word"),
    ("how do I dictate without holding a key", "hands_free"),
    ("what is hands free", "hands_free"),
    ("how can I keep it listening", "hands_free"),
    ("where do I change my recording key", "shortcuts"),
    ("help me change the hold to record keys", "shortcuts"),
    ("where's the recording keys", "shortcuts"),
    ("how do I add a word", "dictionary"),
    ("help me fix a word it gets wrong", "dictionary"),
    ("I want to teach it my name", "dictionary"),
    ("where's my custom words", "dictionary"),
    ("how do I turn Ava off", "ava"),
    ("how can I turn Ava on", "ava"),
    ("is there a way to disable the assistant", "ava"),
    ("help me set up Ava", "ava"),
    ("how do I see what I said earlier", "history"),
    ("where's my previous dictation", "history"),
    ("what's the command to find my history", "history"),
    ("show me how to change my settings", "settings"),
    ("how can I change general settings", "general"),
    ("I can't find dictation settings", "dictation"),
    ("help with sound settings", "audio"),
    ("where is the help page", "help_settings"),
    ("what are command packs", "commands"),
    ("how do I turn command packs on", "commands"),
    ("how do I choose the full display", "listening_display"),
    ("how can I choose the compact display", "listening_display"),
    ("where's the online guides", "documentation"),
    ("how can I find my saved phrases", "snippets"),
    ("how do I turn an audio file into text", "transcribe_file"),
    ("how do I get support", "support"),
    ("help change my mic", "microphone"),
    ("help turn off the voice", "speech"),
    ("how do I change my microphone", "microphone"),
]


@pytest.mark.parametrize("text,target", EXAMPLES)
def test_everyday_examples(text, target):
    assert hi.match_help(text, command_mode=True).target == target


def test_every_existing_destination_and_every_target_has_eight_phrasings():
    # Listening display's navigation action shows it; help opens its actual
    # selector in General instead. Both are existing destinations.
    assert {t.place for t in hi.TARGETS.values()} | {"listening_display"} == set(PLACES)
    counts = Counter(intent.target for intent in hi.PHRASES.values())
    assert set(counts) == set(hi.TARGETS)
    assert min(counts.values()) >= 8
    for phrase, expected in hi.PHRASES.items():
        assert hi.match_help(phrase, command_mode=True) == expected


@pytest.mark.parametrize("text", [
    "I'm lost", "I'm confused", "what do I do", "help", "what now",
    "I am lost", "I need help", "I don't know what to do",
])
def test_confusion_gets_a_short_pointer(text):
    assert hi.match_help(text, command_mode=True).target == "confusion"


@pytest.mark.parametrize("text", [
    "how do I fly a kite", "how do I delete all files", "help fly a kite",
    "show me how to destroy everything", "I want to cook a dinner",
    "how do I change my mic and delete all files", "where do I send my password",
    "what is kite flying", "what are purple kites",
])
def test_unknown_topics_never_pick_a_partial_action(text):
    assert hi.match_help(text, command_mode=True).target == "unknown"


@pytest.mark.parametrize("text", [p for p, _ in EXAMPLES] + [
    "I'm lost", "help", "how do I fly a kite",
    "I asked her how do I change my mic", 'He said "how do I see the commands"',
    "Write down how do I make it stop talking please",
])
def test_all_help_and_embedded_sentences_stay_literal_in_dictation(text):
    assert hi.match_help(text, command_mode=False) is None


@pytest.mark.parametrize("text", [
    "I asked her how do I see the commands", '"how do I see the commands"',
    "she said help me change my mic", "write how do I change the microphone",
])
def test_embedded_or_quoted_questions_are_not_leading_requests(text):
    assert hi.match_help(text, command_mode=True) is None


def test_normalization_and_topic_slot():
    result = hi.match_help("  Please HOW do I change my mic?! please. ", command_mode=True)
    assert result == hi.HelpIntent("microphone", "change my mic")
    assert hi.match_help("Where’s the dictionary?", command_mode=True).target == "dictionary"


@pytest.fixture(scope="module")
def loaded_registry():
    from tools.dump_command_metadata import build_executor
    from samsara import plugin_commands
    executor = build_executor()
    return executor, dict(plugin_commands._REGISTRY)


@pytest.fixture
def executor(loaded_registry, monkeypatch):
    from samsara import plugin_commands
    executor, registry = loaded_registry
    # conftest intentionally clears the global registry before every test.
    # Keep the production policy's registry synchronized with its executor.
    monkeypatch.setattr(plugin_commands, "_REGISTRY", dict(registry))
    return executor


@pytest.fixture
def rig(executor, monkeypatch):
    from plugins.commands import core_utils
    from samsara import command_scope
    from samsara.ui import qt_runtime
    monkeypatch.setattr(command_scope, "capture_context", lambda: MatchContext.for_app("notepad.exe"))
    monkeypatch.setattr(policy, "pending_operation", lambda: None)
    monkeypatch.setattr(qt_runtime, "post", lambda fn, *a, **kw: fn())
    monkeypatch.setattr(core_utils, "_clickable_help", lambda hwnd, query: "")
    from samsara import handlers
    monkeypatch.setattr(handlers, "_get_foreground_hwnd", lambda: None)
    monkeypatch.setattr(handlers, "_get_foreground_exe_lower", lambda: "notepad.exe")
    app = SimpleNamespace(config={}, command_mode_active=False, command_matching_enabled=True,
                          _ava_cmd_generation=0, cheat_sheet=SimpleNamespace(show_scoped=Mock()),
                          audio_coordinator=SimpleNamespace(speak=Mock()))
    monkeypatch.setattr(executor, "_app", app)
    app.command_executor = executor
    opened = []
    monkeypatch.setattr("samsara.samsara_navigation.open_place", lambda app, place: opened.append(place))
    return executor, app, opened


@pytest.mark.parametrize("target", sorted(hi.TARGETS))
def test_every_target_dispatches_through_registered_policy(rig, target, monkeypatch):
    executor, app, opened = rig
    phrase = next(p for p, intent in hi.PHRASES.items() if intent.target == target)
    authorize = Mock(wraps=policy.authorize)
    monkeypatch.setattr(policy, "authorize", authorize)
    result = executor.process_text(phrase, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    assert authorize.call_count == 1
    assert authorize.call_args.args[0].command_id == "what can i say"
    assert opened == ([] if target == "reference" else [hi.TARGETS[target].place])
    assert app.cheat_sheet.show_scoped.called == (target == "reference")


@pytest.mark.parametrize("lane", ["forced", "active", "prefix"])
def test_command_lanes_route_known_and_unknown_requests(rig, lane):
    executor, app, opened = rig
    app.command_mode_active = lane == "active"
    prefix = "command " if lane == "prefix" else ""
    force = lane == "forced"
    assert executor.process_text(prefix + "how do I change my mic", force_commands=force).succeeded
    assert opened == ["general"]
    opened.clear()
    assert executor.process_text(prefix + "how do I delete all files", force_commands=force).succeeded
    assert not opened
    assert app.cheat_sheet.show_scoped.call_args.kwargs["query"] == ""
    assert app.audio_coordinator.speak.call_args.args[0] == hi.UNKNOWN_REPLY


@pytest.mark.parametrize("text", [p for p, _ in EXAMPLES] + [
    "I asked how do I change my mic", "I'm lost", "help fly a kite",
])
@pytest.mark.parametrize("legacy", [False, True])
def test_real_executor_keeps_dictation_literal(rig, text, legacy):
    executor, app, opened = rig
    app.config = {"command_mode": {"dictation_literal_commands_only": not legacy}}
    result = executor.process_text(text)
    assert result.state is DispatchState.MISS
    assert result.result == text
    assert not opened and not app.cheat_sheet.show_scoped.called
    assert not app.audio_coordinator.speak.called


def test_hands_free_dictation_lane_is_not_a_command_lane(rig):
    executor, app, opened = rig
    app.command_mode_active = True
    app._session_mode_manager = SimpleNamespace(mode=SimpleNamespace(name="DICTATE"))
    assert executor.process_text("how do I change my mic").state is DispatchState.MISS
    assert not opened


def test_known_help_prefix_and_search_behavior(rig):
    executor, app, opened = rig
    # FINISH IT 1: an existing scoped topic wins; new action topics extend it.
    assert executor.process_text("help change my mic", force_commands=True).succeeded
    assert opened == ["general"]
    assert executor.process_text("help search purple kite", force_commands=True).succeeded
    assert app.cheat_sheet.show_scoped.call_args.kwargs["query"] == "purple kite"
    assert executor.process_text("what can I say search tabs", force_commands=True).succeeded
    assert app.cheat_sheet.show_scoped.call_args.kwargs["query"] == "tabs"


@pytest.mark.parametrize("text,query", [
    ("help tab", "tab"), ("help dictionary", "dictionary"),
    ("help search purple kite", "purple kite"),
    ("what can I say search window", "window"),
])
def test_existing_scoped_topics_and_catalog_example_keep_their_query(rig, text, query):
    executor, app, opened = rig
    assert executor.process_text(text, force_commands=True).succeeded
    assert not opened
    assert app.cheat_sheet.show_scoped.call_args.kwargs["query"] == query
    assert app.audio_coordinator.speak.call_args.args[0] == "Showing what you can say here."


def test_clickable_control_topic_wins_over_known_help_target(rig, monkeypatch):
    executor, app, opened = rig
    from plugins.commands import core_utils
    controls = "Controls found here: click the dictionary. Use show numbers for more."
    monkeypatch.setattr(core_utils, "_clickable_help", lambda hwnd, query: controls)
    assert executor.process_text("help the dictionary", force_commands=True).succeeded
    assert not opened
    assert app.cheat_sheet.show_scoped.call_args.kwargs == {
        "query": "the dictionary", "live_text": controls}


def test_unknown_help_topic_falls_back_after_scoped_search(rig):
    executor, app, opened = rig
    assert executor.process_text("help fly a kite", force_commands=True).succeeded
    assert not opened
    assert app.cheat_sheet.show_scoped.call_args.kwargs["query"] == ""
    assert app.audio_coordinator.speak.call_args.args[0] == hi.UNKNOWN_REPLY


def test_registered_argument_command_wins_over_a_help_shaped_request(rig, monkeypatch):
    executor, app, opened = rig
    entry = SimpleNamespace(phrase="show me", handler=Mock(return_value=True))
    detail = SimpleNamespace(entry=entry, phrase_tokens=("show", "me"))
    monkeypatch.setattr(executor._matcher, "match_detail", lambda text: detail)
    assert hi.route_request(executor, "show me the commands", app, command_mode=True,
                            route=policy.Route.EXACT, generation=0) is None
    assert not opened and not app.cheat_sheet.show_scoped.called


def test_confusion_shows_reference_and_speaks_pointer(rig):
    executor, app, opened = rig
    assert executor.process_text("I'm confused", force_commands=True).succeeded
    assert app.cheat_sheet.show_scoped.called and not opened
    assert app.audio_coordinator.speak.call_args.args[0] == hi.CONFUSION_REPLY


def test_policy_refusal_does_not_open_anything(rig, monkeypatch):
    executor, app, opened = rig
    monkeypatch.setattr(policy, "authorize", lambda *a, **kw: policy.Denied("stale"))
    result = executor.process_text("how do I change my mic", force_commands=True)
    assert result.state is DispatchState.REJECTED
    assert not opened and not app.cheat_sheet.show_scoped.called


@pytest.mark.parametrize("phrase,spoken", [("show me the commands", "show me the commands"),
                                          ("what's the voice", "WHAT’S THE VOICE?!")])
def test_registered_exact_command_retains_ownership_even_if_help_shaped(executor, phrase, spoken):
    # Model a personal or future catalog command. Classification must stand
    # aside even when that phrase would normally be useful discovery wording.
    old = executor._matcher._entries.get(phrase)
    executor._matcher._entries[phrase] = object()
    try:
        assert hi.route_request(executor, spoken, None, command_mode=True,
                                route=policy.Route.EXACT, generation=0) is None
    finally:
        if old is None:
            del executor._matcher._entries[phrase]
        else:
            executor._matcher._entries[phrase] = old


def test_catalog_registration_and_zero_new_collisions(executor):
    from samsara import command_catalog as cc
    specs = cc.build_catalog(executor)
    claims = cc.raw_phrase_claims(executor)
    assert not cc.collisions(claims)
    assert not cc.orphans(claims, specs)
    help_spec = next(s for s in specs if s.canonical_id == "core_utils.what_can_i_say")
    assert {cc.normalize_phrase(p) for p in hi.HELP_PREFIXES} <= set(help_spec.aliases)
    candidates = set(hi.PHRASES)
    existing = {alias for s in specs if s.canonical_id != help_spec.canonical_id for alias in s.aliases}
    assert not {p for p in candidates if cc.normalize_phrase(p) in existing}


def test_open_only_guidance_does_not_mutate_settings(rig):
    executor, app, opened = rig
    for text in ("how do I turn Ava off", "how do I turn off the voice", "how do I set up the wake word"):
        assert executor.process_text(text, force_commands=True).succeeded
    assert app.config == {}
    assert opened == ["ava", "audio", "dictation"]
