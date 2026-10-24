"""Selection verbs: real dispatch/proposals with fake clipboard, keys, voice and model."""
from types import SimpleNamespace
from pathlib import Path
import ast
import threading
import sys

import pytest

from samsara import ava_pending, execution_policy
from samsara.ava_edit import intent, model, sanitize, selection, session
from tests.test_clean_up_selection_632 import TARGET, SOURCE, app, executor, rig

TRANSLATED = "Compramos leche en la tienda."
SUMMARY = "Bought milk."


@pytest.fixture(autouse=True)
def verbs(rig, app, monkeypatch):
    monkeypatch.setattr(session, "enabled", lambda _: True)
    monkeypatch.setattr(session, "is_available", lambda _: False)
    monkeypatch.setattr(model, "resolve_backend", lambda _: "test")
    rig.timers = []
    rig.presses = []
    monkeypatch.setattr(sys.modules["pyautogui"], "press", rig.presses.append, raising=False)

    def timer(_name, _delay, callback, **_):
        entry = SimpleNamespace(callback=callback, cancelled=False)
        entry.cancel = lambda: setattr(entry, "cancelled", True)
        rig.timers.append(entry)
        return entry

    monkeypatch.setattr(session.thread_registry, "timer", timer)
    app.tts_engine = SimpleNamespace(speak=app.spoken.append)
    app.listening_indicator = SimpleNamespace(show_outcome=lambda *args: app.chips.append(args))
    app._schedule_ui = lambda fn, *args: fn(*args)
    yield
    question = getattr(app, "_ava_question", None)
    if question is not None:
        ava_pending._clear(app, question)


@pytest.mark.parametrize("utterance", ["read that back", "read the selection",
                                     "read selected text", "read this", "um, Ava, read this."])
def test_read_without_model_proposal_or_paste_restores_clipboard(rig, app, monkeypatch, utterance):
    monkeypatch.setattr(session, "enabled", lambda _: False)
    monkeypatch.setattr(model, "resolve_backend", lambda _: pytest.fail("read probed model"))
    assert session.handle_utterance(app, utterance)
    assert app.spoken == [SOURCE]
    assert session.pending() is None and rig.pasted == []
    assert rig.text == "original clipboard" and rig.restored == rig.snapshots


@pytest.mark.parametrize("utterance", ["read this", "translate this to french", "summarize this"])
@pytest.mark.parametrize("selected", ["", "   "])
def test_nothing_selected(rig, app, utterance, selected):
    rig.selected = selected
    assert session.handle_utterance(app, utterance)
    # 823: actionable edit wording; preserve 822's empty readback fallback.
    expected = "There's nothing to read back yet." if utterance == "read this" else "Select some text first"
    assert app.spoken[-1] == expected
    assert session.pending() is None and rig.pasted == []
    assert rig.text == "original clipboard"


@pytest.mark.parametrize("cancel", [False, True])
def test_sentence_chunks_use_coordinator_and_its_existing_cancel_generation(rig, app, cancel):
    rig.selected = "First sentence. Second sentence!\nThird sentence?"
    calls = []
    coordinator = SimpleNamespace(_speech_generation=4)
    coordinator.speak = lambda text, **kwargs: calls.append((text, kwargs))
    app.audio_coordinator = coordinator
    assert session.handle_utterance(app, "read this")
    assert len(calls) == 1 and calls[0][0] == "First sentence. "
    assert calls[0][1]["category"] == "dictation_readback"
    assert "voice_id" not in calls[0][1]  # Coordinator resolves the current voice.
    if cancel:
        coordinator._speech_generation += 1  # cancel_speech / speech interrupt.
        calls[0][1]["on_done"]()  # Edge also completes cancelled jobs.
        assert len(calls) == 1
    else:
        calls[0][1]["on_done"]()
        calls[1][1]["on_done"]()
        calls[2][1]["on_done"]()
        assert "".join(text for text, _ in calls) == rig.selected
    assert rig.text == "original clipboard" and not rig.pasted


@pytest.mark.parametrize("language, code", list(intent.LANGUAGES.items()))
def test_translate_known_languages_uses_model_and_stages(rig, app, monkeypatch, language, code):
    calls = []
    monkeypatch.setattr(model, "rewrite", lambda original, instruction, _app:
                        (calls.append((original, instruction)) or TRANSLATED, None))
    assert session.handle_utterance(app, f"translate this to {language}")
    assert intent.LANGUAGES[language] == code
    assert calls[0][0] == SOURCE and language.title() in calls[0][1]
    assert session.pending().proposal.rewritten == TRANSLATED
    assert session.pending().selection_target == TARGET
    assert not rig.pasted and rig.text == "original clipboard"
    assert session.handle_utterance(app, "discard")
    assert session.pending() is None


@pytest.mark.parametrize("utterance, message", [
    ("translate selection to spanish", "Translation needs Ava's model"),
    ("translate this to klingon", "Translation needs Ava's model"),
    ("summarize this", "Summarization needs Ava's model"),
])
@pytest.mark.parametrize("disabled", [False, True])
def test_model_unavailable_never_calls_rewrite(rig, app, monkeypatch, utterance, message, disabled):
    if disabled:
        monkeypatch.setattr(session, "enabled", lambda _: False)
        monkeypatch.setattr(model, "resolve_backend", lambda _: pytest.fail("disabled model probe"))
    else:
        monkeypatch.setattr(model, "resolve_backend", lambda _: None)
    assert session.handle_utterance(app, utterance)
    assert app.spoken[-1] == message
    assert session.pending() is None and getattr(app, "_ava_question", None) is None
    assert rig.text == "original clipboard"


@pytest.mark.parametrize("utterance", ["translate this", "translate selection to klingon"])
def test_unknown_language_asks_once_in_bounded_reply_window(rig, app, monkeypatch, utterance):
    acquired, released = [], []
    app.app_state, app.wake_word_triggered = "asleep", False
    app._ensure_wake_consumer = acquired.append
    app._release_wake_consumer = released.append
    monkeypatch.setattr(model, "rewrite", lambda *_: (TRANSLATED, None))
    assert session.handle_utterance(app, utterance)
    assert app.spoken == ["Into which language?"]
    assert app.chips[-1] == ("Ava is waiting for your answer. Into which language?", "pending", None)
    assert acquired == ["ava_question"]
    assert app._ava_question.opened
    assert app.app_state == "command_window"
    assert session.pending() is None
    assert ava_pending.consume(app, "Spanish")
    assert not ava_pending.consume(app, "Spanish")
    assert session.pending().proposal.rewritten == TRANSLATED
    assert released == acquired
    assert (app.app_state, app.wake_word_triggered) == ("asleep", False)
    assert app.spoken.count("Into which language?") == 1
    assert rig.text == "original clipboard"


def test_invalid_language_reply_does_not_ask_again(rig, app):
    assert session.handle_utterance(app, "translate this to klingon")
    assert ava_pending.consume(app, "elvish")
    assert app.spoken.count("Into which language?") == 1
    assert getattr(app, "_ava_question", None) is None and session.pending() is None


def test_cancelled_question_does_not_open_microphone_from_completion_callback(rig, app):
    calls = []
    coordinator = SimpleNamespace(_speech_generation=1)
    coordinator.speak = lambda text, **kw: (calls.append(kw) or SimpleNamespace(utterance_id="spoken"))
    app.audio_coordinator = coordinator
    app._ensure_wake_consumer = lambda _: pytest.fail("cancelled question opened microphone")
    assert session.handle_utterance(app, "translate this")
    assert rig.timers  # Expiry is armed even before the speech callback.
    coordinator._speech_generation += 1
    calls[0]["on_done"]()
    assert getattr(app, "_ava_question", None) is None


@pytest.mark.parametrize("cause", ["expired", "stopped", "disabled"])
def test_language_reply_retires_with_shared_lifetime_controls(rig, app, monkeypatch, cause):
    assert session.handle_utterance(app, "translate this")
    reply = app._ava_question
    if cause == "expired":
        reply.deadline = 0
    elif cause == "stopped":
        monkeypatch.setattr(execution_policy, "is_current", lambda *_: False)
    else:
        monkeypatch.setattr(session, "enabled", lambda _: False)
    rig.timers[-1].callback()
    assert getattr(app, "_ava_question", None) is None and reply.timer.cancelled
    assert session.pending() is None


@pytest.mark.parametrize("operation, raw, accepted", [
    ("translate", TRANSLATED, True), ("translate", "", False),
    ("translate", "x" * (len(SOURCE) * 3), True),
    ("translate", "x" * (len(SOURCE) * 3 + 1), False),
    ("summarize", SUMMARY, True), ("summarize", "  ", False),
    ("summarize", "x" * len(SOURCE), True),
    ("summarize", "x" * (len(SOURCE) + 1), False),
])
def test_transform_similarity_bypass_and_exact_length_bounds(app, monkeypatch, operation, raw, accepted):
    monkeypatch.setattr(model, "rewrite", lambda *_: (raw, None))
    proposal = session.propose(SOURCE, "Transform the passage", app, transform=operation)
    assert proposal.ok is accepted
    if not accepted:
        assert proposal.rewritten == SOURCE


@pytest.mark.parametrize("operation", ["translate", "summarize"])
@pytest.mark.parametrize("raw", ["<think>secret</think> text", "```text```", "Sure, here it is.",
                                "Transform the passage"])
def test_transform_keeps_output_hygiene(app, monkeypatch, operation, raw):
    monkeypatch.setattr(model, "rewrite", lambda *_: (raw, None))
    assert not session.propose(SOURCE, "Transform the passage", app, transform=operation).ok


@pytest.mark.parametrize("cleanup", [False, True])
def test_regular_edit_and_cleanup_keep_similarity_floor(app, monkeypatch, cleanup):
    monkeypatch.setattr(model, "rewrite", lambda *_: (TRANSLATED, None))
    assert sanitize.similarity(SOURCE, TRANSLATED) < sanitize.SIMILARITY_FLOOR
    assert not session.propose(SOURCE, "Fix the text", app, cleanup=cleanup).ok


@pytest.mark.parametrize("utterance, below", [
    ("summarize selection", True), ("summarize this", True), ("sum this up", True),
    ("summarize and replace", False), ("summarize this and replace", False),
])
def test_summary_applies_only_on_word_and_in_correct_place(rig, app, monkeypatch, utterance, below):
    monkeypatch.setattr(model, "rewrite", lambda *_: (SUMMARY, None))
    assert session.handle_utterance(app, utterance)
    assert not rig.pasted and not rig.presses
    assert session.pending().selection_mode == ("below" if below else "replace")
    assert session.handle_utterance(app, "apply")
    assert rig.pasted == [("\n" if below else "") + SUMMARY]
    assert rig.presses == (["right", "end"] if below else [])
    assert session.pending() is None and rig.text == "original clipboard"


@pytest.mark.parametrize("backwards", [False, True])
def test_summary_retains_document_including_reversed_selection(rig, app, monkeypatch, backwards):
    monkeypatch.setattr(model, "rewrite", lambda *_: (SUMMARY, None))
    document = SimpleNamespace(text=SOURCE + " trailing text\nNext paragraph.",
                               anchor=len(SOURCE) if backwards else 0,
                               caret=0 if backwards else len(SOURCE))
    original_hotkey = sys.modules["pyautogui"].hotkey

    def press(key):
        if key == "right":
            document.anchor = document.caret = max(document.anchor, document.caret)
        elif key == "end":
            end = document.text.find("\n", document.caret)
            document.anchor = document.caret = end if end >= 0 else len(document.text)

    def hotkey(*args):
        if args == ("ctrl", "v"):
            lo, hi = sorted((document.anchor, document.caret))
            document.text = document.text[:lo] + rig.text + document.text[hi:]
        original_hotkey(*args)

    monkeypatch.setattr(sys.modules["pyautogui"], "press", press)
    monkeypatch.setattr(sys.modules["pyautogui"], "hotkey", hotkey)
    assert session.handle_utterance(app, "summarize this")
    assert session.handle_utterance(app, "apply")
    assert document.text == SOURCE + " trailing text\n" + SUMMARY + "\nNext paragraph."


@pytest.mark.parametrize("operation", ["translate this to spanish", "summarize this"])
def test_transforms_keep_same_selection_and_clipboard_guards(rig, app, monkeypatch, operation):
    monkeypatch.setattr(model, "rewrite", lambda *_: (SUMMARY, None))
    assert session.handle_utterance(app, operation)
    staged = session.pending()
    rig.selected = "Different selection"
    assert not session.apply(app)["applied"]
    assert session.pending() is staged and not rig.pasted and not rig.presses
    assert rig.text == "original clipboard"


def test_summary_focus_change_while_moving_to_end_never_pastes(rig, app, monkeypatch):
    monkeypatch.setattr(model, "rewrite", lambda *_: (SUMMARY, None))
    monkeypatch.setattr(sys.modules["pyautogui"], "press", lambda _: setattr(rig, "target", None))
    assert session.handle_utterance(app, "summarize this")
    staged = session.pending()
    assert not session.apply(app)["applied"]
    assert session.pending() is staged and not rig.pasted
    assert rig.text == "original clipboard"


@pytest.mark.parametrize("utterance", ["then read that back later", "I said read this",
    "read this later", '"read that back"', "summarize this later", "we should sum this up",
    "I will translate this to spanish", '"summarize selection"'])
def test_embedded_mentions_never_capture_or_match(rig, app, executor, utterance):
    assert intent.selection_action(utterance) is None
    assert not session.handle_utterance(app, utterance)
    assert not executor.process_text(utterance, app, force_commands=True).claimed
    assert not rig.keys and session.pending() is None


@pytest.mark.parametrize("utterance", ["read this", "translate this to spanish", "sum this up",
                                     "summarize and replace"])
def test_command_mode_routes_new_verbs_and_reference_metadata(rig, app, executor, monkeypatch, utterance):
    monkeypatch.setattr(model, "rewrite", lambda *_: (SUMMARY, None))
    assert executor.process_text(utterance, app, force_commands=True).claimed
    from samsara.command_catalog import build_catalog
    canonical = selection.command_for_utterance(utterance)
    entry = next(spec for spec in build_catalog(executor)
                 if spec.plugin == "selection_edit" and canonical in spec.aliases)
    assert entry.description.startswith("Ava:")
    assert entry.whole_utterance
    if utterance == "read this":
        assert app.spoken == [SOURCE] and not rig.pasted
    else:
        assert session.pending() is not None


def test_command_mode_language_reply_reaches_captured_selection(rig, app, executor, monkeypatch):
    monkeypatch.setattr(model, "rewrite", lambda *_: (TRANSLATED, None))
    assert executor.process_text("translate this to klingon", app, force_commands=True).claimed
    # Production command-mode dispatch consumes the shared question before
    # invoking the executor. A language is never a registered global command.
    assert ava_pending.consume(app, "Spanish", capture_path="toggle_command")
    assert session.pending().proposal.rewritten == TRANSLATED
    assert executor.process_text("apply", app, force_commands=True).claimed
    assert rig.pasted == [TRANSLATED] and rig.text == "original clipboard"


@pytest.mark.parametrize("method", ["_route_to_ava", "_ava_session_agent_dispatch_fn"])
def test_real_ava_routes_consume_language_before_global_commands(rig, app, executor, monkeypatch, method):
    # Compile the existing route itself; never import/bootstrap dictation.
    source = ast.parse((Path(__file__).resolve().parents[1] / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    route = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method)
    scope = {}
    exec(compile(ast.Module(body=[route], type_ignores=[]), "selection_reply_route", "exec"), scope)
    app.command_executor = executor
    app.app_state, app.wake_word_triggered = "asleep", False
    acquired, released, calls = [], [], []
    app._ensure_wake_consumer, app._release_wake_consumer = acquired.append, released.append

    def rewrite(*args):
        assert app._ava_question is None and released == acquired == ["ava_question"]
        calls.append(args)
        return TRANSLATED, None

    monkeypatch.setattr(model, "rewrite", rewrite)
    assert executor.process_text("translate this to klingon", app, force_commands=True).claimed
    assert app._ava_question.opened
    monkeypatch.setattr(executor, "process_text", lambda *_a, **_kw: pytest.fail("reply reached global commands"))
    monkeypatch.setattr("plugins.commands.ask_ollama.handle_ask_ava",
                        lambda *_a, **_kw: pytest.fail("deterministic reply reached model follow-up"))
    scope[method](app, "Spanish", *([None] if method == "_ava_session_agent_dispatch_fn" else []))
    assert len(calls) == 1 and calls[0][0] == SOURCE
    assert session.pending().selection_target == TARGET
    assert session.pending().proposal.rewritten == TRANSLATED
    assert (app.app_state, app.wake_word_triggered) == ("asleep", False)
    assert not rig.pasted and rig.text == "original clipboard"


def test_translation_question_admits_real_wake_polling_with_wake_off(rig, app, monkeypatch):
    from samsara.audio_engine import wake_consumer
    app.wake_word_active = app.command_mode_active = False
    app.app_state, app.wake_word_triggered = "asleep", False
    app._ensure_wake_consumer = lambda _: None
    app._release_wake_consumer = lambda _: None
    assert session.handle_utterance(app, "translate this")
    assert app._ava_question.opened
    listener = wake_consumer.WakeConsumer.__new__(wake_consumer.WakeConsumer)
    listener._app, listener._running = app, True
    listener._stop_event = threading.Event()
    frame, received = object(), []
    listener._reader = SimpleNamespace(read_next=lambda: frame)

    def process(actual):
        received.append(actual)
        listener._running = False

    listener._process_frame = process
    listener._close_capture_duck = lambda: None
    listener._handle_fatal = lambda *_: pytest.fail("wake polling failed")
    monkeypatch.setattr(wake_consumer.time, "sleep", lambda _: listener._stop_event.set())
    listener._poll_loop()
    assert received == [frame] and app.wake_word_active is False


@pytest.mark.parametrize("answer", ["stop", "stop reading", "cancel", "never mind", "discard"])
def test_shared_question_cancel_never_translates(rig, app, answer):
    assert session.handle_utterance(app, "translate this")
    assert ava_pending.consume(app, answer)
    assert app._ava_question is None and session.pending() is None
    assert app.spoken.count("Into which language?") == 1


def test_answer_before_question_finishes_does_not_run_callback(rig, app, monkeypatch):
    callbacks, calls = [], []
    app.audio_coordinator = SimpleNamespace(_speech_generation=0,
        speak=lambda _text, **kw: callbacks.append(kw["on_done"]) or SimpleNamespace(utterance_id="fake"))
    monkeypatch.setattr(model, "rewrite", lambda *_: calls.append(True) or (TRANSLATED, None))
    assert session.handle_utterance(app, "translate this")
    question = app._ava_question
    assert not question.opened
    assert ava_pending.consume(app, "Spanish")
    assert app._ava_question is question and not calls
    callbacks.pop()()
    assert question.opened and ava_pending.consume(app, "Spanish")
    assert len(calls) == 1 and app._ava_question is None


def test_stop_during_model_reply_does_not_stage_stale_translation(rig, app, monkeypatch):
    def rewrite(*_):
        execution_policy.bump_generation(app, "test stop")
        return TRANSLATED, None

    monkeypatch.setattr(model, "rewrite", rewrite)
    assert session.handle_utterance(app, "translate this")
    assert ava_pending.consume(app, "Spanish")
    assert app._ava_question is None and session.pending() is None


def test_legacy_readback_registry_and_last_dictation_aliases_survive(rig, app, executor):
    from plugins.commands import core_utils
    from samsara import plugin_commands
    plugin_commands._reinstall_module_commands(core_utils)
    selection.register_commands("selection_edit")
    entry = plugin_commands._REGISTRY["read that back"]
    assert entry["pack"] == "accessibility"
    app._last_dictation_text = "Previously committed dictation."
    app.history_store = SimpleNamespace(query=lambda **_: [])
    app.audio_coordinator = SimpleNamespace(speak=lambda text, **_: app.spoken.append(text))
    for alias in ("read it back", "what did you type", "read my last dictation"):
        assert plugin_commands._REGISTRY[alias] is entry
        assert executor.process_text(alias, app, force_commands=True).claimed
    assert app.spoken == [app._last_dictation_text] * 3
    assert not rig.keys


def test_new_verbs_reuse_teaching_selection_vocabulary(monkeypatch):
    from samsara import teach_patterns
    monkeypatch.setitem(teach_patterns._SOURCE_KEYWORD_TO_KIND, "chosen passage", "selection")
    for utterance in ("read the chosen passage", "translate chosen passage to french",
                      "summarize chosen passage"):
        assert intent.selection_request(utterance) == "selection"
        assert intent.selection_action(utterance) is not None


def test_bare_new_verbs_capture_selection_even_when_staged_draft_exists(rig, app, monkeypatch):
    app._session_mode_manager = SimpleNamespace(top_dictation_chunk=lambda: SimpleNamespace(payload="A draft"))
    monkeypatch.setattr(model, "rewrite", lambda *_: (SUMMARY, None))
    assert session.handle_utterance(app, "summarize this")
    assert session.pending().proposal.original == SOURCE
