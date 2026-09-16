"""Queue 161: tap-first alternatives for a clicked dictate-preview word."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import correction_queue as cq
from samsara import streaming as st
from samsara.session_modes import CommandDispatchResult, SessionMode, SessionModeManager, UtteranceSignals


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


def _manager():
    manager = SessionModeManager(
        abort_phrases=["cancel"], foreground_exe_resolver=lambda: "editor.exe",
        foreground_hwnd_resolver=lambda: 101, inject_fn=lambda text, focus_guard=None: text,
        remove_chars_fn=Mock(), command_dispatch_fn=lambda text: CommandDispatchResult(False, None),
        agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager


@pytest.fixture
def preview(monkeypatch, tmp_path):
    cq.reset_queue()
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(cq, "_QUEUE", cq.CorrectionQueue(str(tmp_path / "correction_queue.json")))
    manager, calls = _manager(), {"choices": [], "prompt": [], "transcript": []}

    class Overlay:
        def __init__(self, *args, **kwargs): self.idle_hints = []
        def set_interaction_callbacks(self, *args): self.word, self.clear = args
        def set_correction_choice_callbacks(self, *args): self.choose, self.dismiss = args
        def set_transcript(self, *args, **kwargs): calls["transcript"].append((args, kwargs))
        def set_prompt(self, text): calls["prompt"].append(text)
        def show_correction_choices(self, word, candidates, no_match): calls["choices"].append((word, candidates, no_match))
        def hide_correction_choices(self): calls["hidden"] = calls.get("hidden", 0) + 1
        def show(self): pass
        def close(self): pass
        def set_literal_badge(self, on): pass
        def scroll_draft(self, where): pass
        def move_draft(self, where): pass

    monkeypatch.setattr(st, "StreamingOverlayQt", Overlay)
    monkeypatch.setattr(st.thread_registry, "spawn", lambda *args, **kwargs: None)
    app = SimpleNamespace(config={}, is_speaking=False, _ensure_session_mode_manager=lambda: manager,
                          voice_training_window=SimpleNamespace(custom_vocab=["Morne"], corrections_dict={},
                          _rebuild_corrections_pattern=lambda: None, save_training_data=lambda: None))
    session = st.DictatePreviewSession(app)
    session.start()
    yield SimpleNamespace(session=session, manager=manager, app=app, calls=calls)
    cq.reset_queue()


def _stage(preview, text):
    preview.manager.dispatch_utterance(text, GOOD)
    preview.session.on_utterance_final(final_text=text)


def _click_nth(preview, word, occurrence):
    index = [i for i, (current, _nth) in enumerate(preview.session._words) if current == word][occurrence]
    preview.session._on_word_clicked(index)


def test_phonetic_table_uses_personal_terms_not_a_general_word_list():
    neighbours = cq.phonetic_neighbours("Morn", ["Morne", "lamp", "morning"])
    assert "Morne" in neighbours and "lamp" not in neighbours


def test_click_offers_original_and_selecting_candidate_replaces_the_third_occurrence(preview):
    _stage(preview, "Morn was Morn and Morn.")
    _click_nth(preview, "Morn", 2)
    word, candidates, no_match = preview.calls["choices"][-1]
    assert word == "Morn" and candidates[:2] == ["Morn", "Morne"] and no_match is False
    preview.session._on_candidate_chosen("Morne")
    assert preview.manager.dictate_pending_buffer == "Morn was Morn and Morne."
    assert [(entry["wrong"], entry["right"]) for entry in cq.get_queue().pending] == [("Morn", "Morne")]


def test_dismiss_changes_nothing_and_disarms(preview):
    _stage(preview, "I met Morn yesterday.")
    _click_nth(preview, "Morn", 0)
    assert preview.calls["choices"][-1][1] == ["Morn", "Morne"]
    preview.session._on_candidates_dismissed()
    assert preview.manager.dictate_pending_buffer == "I met Morn yesterday."
    assert preview.manager.pending_word_correction() is None


def test_no_audio_still_uses_the_phonetic_table_and_no_match_explains_it(preview):
    _stage(preview, "I met Morn yesterday.")
    _click_nth(preview, "Morn", 0)
    assert preview.calls["choices"][-1] == ("Morn", ["Morn", "Morne"], False)
    preview.app.voice_training_window.custom_vocab = []
    _stage(preview, "")
    preview.session._on_word_clicked(2)
    assert preview.calls["choices"][-1] == ("Morn", ["Morn"], True)


def test_spoken_replacement_still_shares_the_apply_and_capture_route(preview):
    _stage(preview, "I met Morn yesterday.")
    _click_nth(preview, "Morn", 0)
    outcome = preview.manager.dispatch_utterance("Morne", GOOD)
    assert outcome.kind == "dictate_word_corrected"
    assert preview.manager.dictate_pending_buffer == "I met Morne yesterday."
    assert [(entry["wrong"], entry["right"]) for entry in cq.get_queue().pending] == [("Morn", "Morne")]


def test_tooltip_shows_tappable_original_candidate_and_dismiss(qapp):
    widget = st._StreamingWidget(False, st.IdleSettings(5.0, 0.25), lambda: False)
    try:
        widget.show_overlay()
        qapp.processEvents()
        widget._w.show_correction_choices("Morn", ["Morn", "Morne"], False)
        qapp.processEvents()
        assert widget._w._choices.isVisible()
        assert widget._w.height() >= 170, "the choice list must not be clipped by the preview"
        assert [button.text() for button in widget._w._choice_buttons] == [
            "Morn", "Morne", "Dismiss — change nothing"]
    finally:
        widget.stop_life()
        widget._w.hide()
        widget._w.deleteLater()
        qapp.processEvents()
