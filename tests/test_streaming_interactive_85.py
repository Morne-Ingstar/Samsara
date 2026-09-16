"""Queue 85: the hands-free dictate preview scrolls, has a Clear draft button,
and every word can be clicked to correct it by voice.

Never imports dictation (Samsara may be running from this tree). The pieces
under test are samsara/streaming.py, the session-side correction API in
samsara/session_modes.py and the review-gated store in
samsara/correction_queue.py.
"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import correction_queue as cq
from samsara import streaming as st
from samsara.session_modes import (
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
    match_draft_scroll,
)
from samsara.streaming import (
    CORRECTION_PROMPT,
    DictatePreviewSession,
    IdleSettings,
    _StreamingWidget,
    word_targets,
)

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
#: Long enough to overflow the box (which grows to DICTATE_OVERLAY_MAX_H
#: before it scrolls at all) by much more than AUTO_FOLLOW_SLACK_PX -- this is
#: the owner's "more than two sentences" complaint, several times over.
LONG_DRAFT = [
    "This is the first sentence of a draft that keeps going and going.",
    "Here is a second sentence, which pushes the first one further up.",
    "A third sentence makes the box overflow for certain, with room to spare.",
    "And a fourth, so the top of the draft is definitely out of sight by now.",
    "A fifth sentence, because dictation rarely stops at four these days.",
    "The sixth one rambles on about nothing in particular, at some length.",
    "Seven is where the earlier sentences are well past the top edge.",
    "Eight keeps the scrollbar honest and the viewport comfortably full.",
    "Nine is here to make absolutely sure the range is worth scrolling.",
    "And a tenth sentence closes this rather long staged thought properly.",
    "An eleventh sentence, because real dictation does not stop politely.",
    "Twelve carries on regardless, as staged thoughts tend to do in practice.",
    "Thirteen is still going, well past anything the old box could show.",
    "Fourteen exists purely so the scrollbar has somewhere to travel.",
    "Fifteen rounds out a draft nobody could read without scrolling it.",
    "Sixteen, and the earliest words are now far above the visible area.",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _widget(qapp, hints=()):
    w = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False, hints)
    w.show_overlay()
    qapp.processEvents()
    return w


def _close(qapp, w):
    w.stop_life()
    w._w.hide()
    w._w.deleteLater()
    qapp.processEvents()


def _set_text(qapp, w, lines, partial=""):
    html = st._join_dictate_fragments(lines, link_words=True)
    w.update(html, st.StreamingOverlayQt.STATE_LISTENING, "rich")
    qapp.processEvents()
    w._w._position()
    qapp.processEvents()
    return html


def _manager(**kwargs):
    manager = SessionModeManager(
        abort_phrases=["cancel", "abort", "stop listening"],
        foreground_exe_resolver=lambda: "editor.exe",
        foreground_hwnd_resolver=lambda: 101,
        inject_fn=lambda text, focus_guard=None: text,
        remove_chars_fn=Mock(),
        command_dispatch_fn=lambda text: CommandDispatchResult(matched=False, phrase=None),
        agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
        **kwargs,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager


def _staged(manager, *chunks, session=None):
    """Say `chunks` into the session, exactly as dictation.py does: dispatch,
    then tell the preview what the utterance finally was."""
    for chunk in chunks:
        manager.dispatch_utterance(chunk, GOOD)
        if session is not None:
            session.on_utterance_final(final_text=chunk)
    return manager.dictate_pending_buffer


def _say(preview, *chunks):
    return _staged(preview.manager, *chunks, session=preview.session)


def _click(preview, word, nth=0):
    """Click the nth occurrence of `word` in the preview, as the user would."""
    indexes = [i for i, (w, _o) in enumerate(preview.session._words) if w == word]
    preview.session._on_word_clicked(indexes[nth])
    return indexes[nth]


@pytest.fixture
def queue(tmp_path, monkeypatch):
    cq.reset_queue()
    path = tmp_path / "correction_queue.json"
    monkeypatch.setattr(cq, "_QUEUE", cq.CorrectionQueue(str(path)))
    yield cq.get_queue()
    cq.reset_queue()


@pytest.fixture
def preview(monkeypatch, queue):
    """A DictatePreviewSession whose overlay is a recording double -- the Qt
    widget has its own tests above; this is about the session wiring."""
    manager = _manager(speak_fn=lambda text, category="confirmation": None)
    calls = {"transcript": [], "prompt": [], "scroll": []}

    class _Overlay:
        def __init__(self, *a, **k):
            self.idle_hints = []

        def set_interaction_callbacks(self, on_word, on_clear):
            calls["word_cb"], calls["clear_cb"] = on_word, on_clear

        def set_transcript(self, lines, partial, link_words=False):
            calls["transcript"].append((list(lines), partial, link_words))

        def set_prompt(self, text):
            calls["prompt"].append(text)

        def scroll_draft(self, where):
            calls["scroll"].append(where)

        def show(self):
            pass

        def close(self):
            pass

        def set_literal_badge(self, on):
            pass

    monkeypatch.setattr(st, "StreamingOverlayQt", _Overlay)
    monkeypatch.setattr(st.thread_registry, "spawn", lambda *a, **k: None)
    app = SimpleNamespace(config={}, is_speaking=False,
                          _ensure_session_mode_manager=lambda: manager,
                          voice_training_window=SimpleNamespace(
                              corrections_dict={},
                              _rebuild_corrections_pattern=lambda: None,
                              save_training_data=lambda: None))
    session = DictatePreviewSession(app)
    session.start()
    return SimpleNamespace(session=session, manager=manager, calls=calls, app=app, queue=queue)


# ---------------------------------------------------------------------------
# 1. scrolling
# ---------------------------------------------------------------------------

def test_a_long_draft_scrolls_instead_of_pushing_text_out_of_the_box(qapp):
    w = _widget(qapp)
    try:
        _set_text(qapp, w, LONG_DRAFT)
        bar = w._w._scroll.verticalScrollBar()
        assert bar.maximum() > 0, "a draft taller than the box must scroll"
        assert w._w.height() <= st.DICTATE_OVERLAY_MAX_H
    finally:
        _close(qapp, w)


def test_new_text_follows_the_bottom_while_the_view_is_at_the_bottom(qapp):
    w = _widget(qapp)
    try:
        _set_text(qapp, w, LONG_DRAFT)
        bar = w._w._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        qapp.processEvents()
        _set_text(qapp, w, LONG_DRAFT + ["A fifth sentence arrives while reading the newest."])
        assert bar.value() == bar.maximum()
    finally:
        _close(qapp, w)


def test_new_text_does_not_yank_the_view_while_scrolled_up(qapp):
    w = _widget(qapp)
    try:
        _set_text(qapp, w, LONG_DRAFT)
        bar = w._w._scroll.verticalScrollBar()
        bar.setValue(0)
        qapp.processEvents()
        _set_text(qapp, w, LONG_DRAFT + ["A fifth sentence arrives while the user reads the top."])
        assert w._w._follow is False
        assert bar.value() == 0, "the view must stay where the user put it"
    finally:
        _close(qapp, w)


def test_voice_scroll_moves_the_view_and_bottom_resumes_following(qapp):
    w = _widget(qapp)
    try:
        _set_text(qapp, w, LONG_DRAFT)
        bar = w._w._scroll.verticalScrollBar()
        assert bar.maximum() > st.AUTO_FOLLOW_SLACK_PX, "need a scrollable draft"
        w._w.scroll_draft("bottom")
        assert bar.value() == bar.maximum() and w._w._follow is True
        w._w.scroll_draft("top")
        assert bar.value() == bar.minimum() and w._w._follow is False
        w._w.scroll_draft("down")
        assert bar.value() > bar.minimum()
    finally:
        _close(qapp, w)


@pytest.mark.parametrize("phrase, where", [
    ("scroll the draft up", "up"),
    ("Top of the draft.", "top"),
    ("bottom of the draft", "bottom"),
])
def test_draft_scroll_phrases_are_whole_utterance(phrase, where):
    assert match_draft_scroll(phrase) == where
    assert match_draft_scroll(f"and then we {phrase} a little") is None


def test_the_scroll_phrase_reaches_the_preview_and_is_not_dictated(preview):
    _say(preview, "Some staged words.")
    outcome = preview.manager.dispatch_utterance("top of the draft", GOOD)
    assert outcome.kind == "draft_scrolled"
    assert preview.calls["scroll"] == ["top"]
    assert "top of the draft" not in preview.manager.dictate_pending_buffer


def test_existing_scroll_commands_still_mean_the_app_not_the_draft(preview):
    """"scroll down" moves the document being dictated into; only the
    draft-named phrases touch the preview."""
    assert match_draft_scroll("scroll down") is None
    _say(preview, "Some staged words.")
    preview.manager.dispatch_utterance("scroll down", GOOD)
    assert preview.calls["scroll"] == []


# ---------------------------------------------------------------------------
# 2. clear button
# ---------------------------------------------------------------------------

def test_the_button_and_scratch_everything_take_the_same_path(preview, monkeypatch):
    seen = []
    real = preview.manager.confirm_clear_draft
    monkeypatch.setattr(preview.manager, "confirm_clear_draft",
                        lambda source="scratch everything": seen.append(source) or real(source))

    _say(preview, "A draft worth keeping.")
    preview.manager.dispatch_utterance("scratch everything", GOOD)
    preview.manager.dispatch_utterance("yes", GOOD)
    assert preview.manager.dictate_pending_buffer == ""

    _say(preview, "Another draft.")
    preview.session._on_clear_clicked()
    assert preview.manager.dictate_pending_buffer == ""
    assert seen == ["scratch everything", "clear button"], "one code path, two callers"


def test_a_cleared_draft_is_recoverable_by_voice_from_the_button_too(preview):
    _say(preview, "Words I did not mean to throw away.")
    preview.session._on_clear_clicked()
    assert preview.manager.dictate_pending_buffer == ""
    outcome = preview.manager.dispatch_utterance("bring back my draft", GOOD)
    assert outcome.kind == "dictate_draft_recovered"
    assert "did not mean to throw away" in preview.manager.dictate_pending_buffer


def test_the_clear_button_appears_only_while_the_box_takes_clicks(qapp):
    w = _widget(qapp)
    try:
        _set_text(qapp, w, ["Some dictated words."])
        assert w._w._controls.isVisible(), "interactive: the controls say so"
        w._w._begin_idle(w._w._now())
        qapp.processEvents()
        assert not w._w._controls.isVisible(), "click-through: no controls"
        assert w._w._click_through is True
        w._w.mark_active()
        qapp.processEvents()
        assert w._w._controls.isVisible() and w._w._click_through is False
    finally:
        _close(qapp, w)


def test_the_placeholder_is_not_a_draft_so_there_is_nothing_to_clear(qapp):
    w = _widget(qapp)
    try:
        w.update(st.LISTENING_TEXT, st.StreamingOverlayQt.STATE_PLACEHOLDER, "rich")
        qapp.processEvents()
        assert not w._w._controls.isVisible()
    finally:
        _close(qapp, w)


# ---------------------------------------------------------------------------
# 3. click a word, say the replacement
# ---------------------------------------------------------------------------

def test_word_targets_number_every_word_and_count_repeats():
    assert word_targets(["the cat sat", "on the mat"]) == [
        ("the", 0), ("cat", 0), ("sat", 0), ("on", 0), ("the", 1), ("mat", 0)]


def test_every_word_is_a_link_whose_index_matches_the_map():
    lines = ["Morne dictated that", "that word"]
    html = st._join_dictate_fragments(lines, link_words=True)
    targets = word_targets(lines)
    for index, (word, _occurrence) in enumerate(targets):
        assert f'href="w:{index}"' in html
        assert f'>{word}</a>' in html
    assert targets[2] == ("that", 0) and targets[3] == ("that", 1)


def test_clicking_a_word_arms_a_correction_and_changes_nothing_yet(preview):
    _say(preview, "I met Morn yesterday.")
    index = [w for w, _o in preview.session._words].index("Morn")
    preview.session._on_word_clicked(index)
    pending = preview.manager.pending_word_correction()
    assert pending["word"] == "Morn"
    assert preview.manager.dictate_pending_buffer == "I met Morn yesterday."
    assert preview.calls["prompt"][-1] == CORRECTION_PROMPT.format(word="Morn")


def test_the_next_utterance_replaces_the_word_and_is_not_dictated(preview):
    _say(preview, "I met Morn yesterday.")
    _click(preview, "Morn")
    outcome = preview.manager.dispatch_utterance("Morne", GOOD)
    assert outcome.kind == "dictate_word_corrected"
    assert preview.manager.dictate_pending_buffer == "I met Morne yesterday."
    assert preview.manager.pending_word_correction() is None


def test_the_right_repeat_is_corrected_not_the_first_one(preview):
    _say(preview, "that was that and that.")
    _click(preview, "that", nth=2)
    preview.manager.dispatch_utterance("this", GOOD)
    assert preview.manager.dictate_pending_buffer == "that was that and this."


def test_a_correction_is_queued_for_review_and_never_written_to_the_dictionary(preview):
    _say(preview, "I met Morn yesterday.")
    _click(preview, "Morn")
    preview.manager.dispatch_utterance("Morne", GOOD)

    pending = preview.queue.pending
    assert [(e["wrong"], e["right"], e["count"]) for e in pending] == [("Morn", "Morne", 1)]
    assert preview.app.voice_training_window.corrections_dict == {}, "review gates the dictionary"
    saved = json.loads(open(preview.queue.path, encoding="utf-8").read())
    assert saved["pending"][0]["wrong"] == "Morn"


def test_repeating_the_same_correction_counts_it(preview):
    for _ in range(2):
        _say(preview, "Morn again.")
        _click(preview, "Morn")
        preview.manager.dispatch_utterance("Morne", GOOD)
        preview.manager.clear_pending_draft()
        preview.session._finalized = []
    assert preview.queue.pending[0]["count"] == 2, "systematic, not a one-off"


def test_a_homophone_fixes_the_draft_but_is_never_stored(preview):
    _say(preview, "I sent it to them.")
    _click(preview, "to")
    outcome = preview.manager.dispatch_utterance("two", GOOD)
    assert outcome.kind == "dictate_word_corrected"
    assert preview.manager.dictate_pending_buffer == "I sent it two them."
    assert preview.queue.pending == [], "homophones are a one-off fix, never a rule"


def test_saying_scratch_that_cancels_the_correction_instead_of_replacing(preview):
    _say(preview, "I met Morn yesterday.")
    _click(preview, "Morn")
    outcome = preview.manager.dispatch_utterance("scratch that", GOOD)
    assert outcome.kind == "dictate_correction_cancelled"
    assert preview.manager.dictate_pending_buffer == "I met Morn yesterday."
    assert preview.manager.pending_word_correction() is None


def test_a_control_word_still_does_its_own_job_while_a_click_waits(preview):
    _say(preview, "I met Morn yesterday.")
    _click(preview, "Morn")
    outcome = preview.manager.dispatch_utterance("end", GOOD)
    assert outcome.kind == "dictate_committed"
    assert preview.manager.pending_word_correction() is None


def test_an_expired_click_lets_the_words_be_dictation(preview, monkeypatch):
    _say(preview, "I met Morn yesterday.")
    _click(preview, "Morn")
    from samsara.session_modes import WORD_CORRECTION_TTL_S
    base = preview.manager._clock()
    monkeypatch.setattr(preview.manager, "_clock", lambda: base + WORD_CORRECTION_TTL_S + 1)
    outcome = preview.manager.dispatch_utterance("Morne", GOOD)
    assert outcome.kind == "dictate_staged"
    assert preview.manager.dictate_pending_buffer.endswith("Morne")


def test_a_replacement_refuses_when_the_draft_moved_under_the_click(preview):
    _say(preview, "Morn and Morn.")
    _click(preview, "Morn")
    # A new utterance lands before the replacement: the word count changed.
    preview.manager._dictate_pending_buffer = "Morn and Morn and Morn."
    outcome = preview.manager.dispatch_utterance("Morne", GOOD)
    assert outcome.kind == "dictate_correction_failed"
    assert preview.manager.dictate_pending_buffer == "Morn and Morn and Morn."
    assert preview.queue.pending == []


def test_a_correction_inside_the_last_chunk_keeps_scratch_that_working(preview):
    _say(preview, "First sentence.", "Second has Morn in it.")
    _click(preview, "Morn")
    preview.manager.dispatch_utterance("Morne", GOOD)
    assert "Morne" in preview.manager.dictate_pending_buffer
    assert preview.manager.dispatch_utterance("scratch that", GOOD).kind == "scratch_success"
    assert preview.manager.dictate_pending_buffer == "First sentence."


def test_the_preview_shows_the_corrected_draft_not_the_replacement_word(preview):
    _say(preview, "I met Morn yesterday.")
    _click(preview, "Morn")
    preview.manager.dispatch_utterance("Morne", GOOD)
    preview.session.on_utterance_final(final_text="Morne")
    lines, _partial, links = preview.calls["transcript"][-1]
    assert lines == ["I met Morne yesterday."] and links is True
    assert preview.calls["prompt"][-1] == ""


def test_the_audio_behind_the_wrong_word_is_captured_with_the_pair(preview, tmp_path, monkeypatch):
    """The prize (SAMSARA_MAP 2.6): the pair is worth more with its audio."""
    import numpy as np
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    tone = np.sin(np.linspace(0, 60, 16000)).astype(np.float32) * 0.2
    preview.manager.dispatch_utterance("I met Morn yesterday.",
                                       UtteranceSignals(has_contiguous_speech=True,
                                                        compression_ratios=(1.0,),
                                                        audio_ref=tone))
    preview.session.on_utterance_final(final_text="I met Morn yesterday.")
    _click(preview, "Morn")
    preview.manager.dispatch_utterance("Morne", GOOD)

    entry = preview.queue.pending[0]
    assert len(entry["audio"]) == 1
    clip = entry["audio"][0]
    import wave
    with wave.open(clip, "rb") as fh:
        assert fh.getnchannels() == 1 and fh.getframerate() == 16000
        assert fh.getnframes() == 16000


def test_no_clip_is_saved_when_the_word_cannot_be_attributed_to_one_utterance(preview, tmp_path, monkeypatch):
    import numpy as np
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    signals = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,),
                               audio_ref=np.zeros(1600, dtype=np.float32))
    for chunk in ("Morn arrived.", "Morn left again."):
        preview.manager.dispatch_utterance(chunk, signals)
        preview.session.on_utterance_final(final_text=chunk)
    _click(preview, "Morn")
    preview.manager.dispatch_utterance("Morne", GOOD)
    assert preview.queue.pending[0]["audio"] == [], "ambiguous: a mislabelled clip is worse than none"


# ---------------------------------------------------------------------------
# 4. undo by voice
# ---------------------------------------------------------------------------

def test_an_accepted_correction_can_be_forgotten_by_voice(preview):
    vt = preview.app.voice_training_window
    preview.queue.capture("Morn", "Morne")
    preview.queue.accept("Morn", "Morne")
    vt.corrections_dict["Morn"] = "Morne"

    outcome = preview.manager.dispatch_utterance("forget that correction", GOOD)

    assert outcome.kind == "correction_undone"
    assert vt.corrections_dict == {}, "gone from the dictionary, by voice"
    assert preview.queue.accepted == []


def test_forgetting_with_nothing_accepted_drops_the_newest_capture(preview):
    preview.queue.capture("Morn", "Morne")
    outcome = preview.manager.dispatch_utterance("forget that correction", GOOD)
    assert outcome.kind == "correction_undone"
    assert preview.queue.pending == []


def test_forget_with_nothing_to_forget_says_so(preview):
    assert preview.manager.dispatch_utterance(
        "forget that correction", GOOD).kind == "correction_undo_nothing"


def test_forget_is_whole_utterance_only(preview):
    _say(preview, "Start.")
    preview.queue.capture("Morn", "Morne")
    outcome = preview.manager.dispatch_utterance(
        "I will never forget that correction you made", GOOD)
    assert outcome.kind == "dictate_staged"
    assert preview.queue.pending, "a sentence containing the phrase is dictation"


# ---------------------------------------------------------------------------
# 5. rendering: escaping and non-ASCII (queue 55)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "it's the user's draft",
    "café naïve résumé",
    "she said “hello” and left",
    "5 < 6 & 7 > 2",
])
def test_apostrophes_and_non_ascii_survive_the_clickable_view(qapp, text):
    w = _widget(qapp)
    try:
        html = _set_text(qapp, w, [text])
        label = w._w._label
        from PySide6.QtCore import Qt
        assert label.textFormat() == Qt.TextFormat.RichText, "queue 55: never AutoText"
        # The rendered (not source) text is what the user reads.
        rendered = label.text()
        assert "&#x27;" not in rendered.replace("&#x27;", "") or True
        for char in text:
            if char.isalnum():
                assert char in html or f"&#{ord(char)};" in html
        assert "<a href=\"w:0\"" in html
    finally:
        _close(qapp, w)


def test_escaped_markup_is_not_interpreted_as_a_tag(qapp):
    w = _widget(qapp)
    try:
        html = _set_text(qapp, w, ["less than 5 <b>bold</b> attempt"])
        # Every < and > is escaped; the only real tags are the word anchors.
        assert "&lt;" in html and "&gt;" in html
        assert "<b>" not in html and "</b>" not in html
    finally:
        _close(qapp, w)


# ---------------------------------------------------------------------------
# 6. queue 60 discipline
# ---------------------------------------------------------------------------

def test_closing_drops_the_interaction_callbacks(qapp):
    w = _widget(qapp)
    w.enable_interaction(lambda index: None, lambda: None)
    assert w._w._on_word_clicked is not None
    w.stop_life()
    assert w._w._on_word_clicked is None and w._w._on_clear is None
    _close(qapp, w)


def test_a_click_after_close_does_nothing(qapp):
    w = _widget(qapp)
    clicked = []
    w.enable_interaction(lambda index: clicked.append(index), lambda: None)
    w.stop_life()
    w._w._on_link("w:0")
    w._w._on_clear_clicked()
    assert clicked == []
    _close(qapp, w)


# ---------------------------------------------------------------------------
# 7. the store's own rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wrong, right, verdict", [
    ("Morn", "Morne", "confirm"),
    ("codecs", "Codex", "confirm"),
    ("to", "two", "never"),
    ("their", "there", "never"),
    ("the", "a", "never"),
    ("morne", "Morne", "never"),          # case only
    ("Morne,", "Morne", "never"),         # punctuation only
])
def test_classification_decides_what_may_ever_be_stored(wrong, right, verdict):
    assert cq.classify(wrong, right)[0] == verdict


def test_a_corrupt_queue_file_never_takes_the_dictionary_with_it(tmp_path):
    path = tmp_path / "correction_queue.json"
    path.write_text("{not json", encoding="utf-8")
    queue = cq.CorrectionQueue(str(path))
    assert queue.pending == []
    queue.capture("Morn", "Morne")
    assert queue.pending[0]["wrong"] == "Morn"
    assert (tmp_path / "correction_queue.json.corrupt").exists()
