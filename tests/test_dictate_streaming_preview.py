"""Tests for the toggle-DICTATE-lane streaming preview overlay (SPARK
2026-07-18: "hands-free streaming preview: live partials overlay for
toggle-DICTATE").

Preview-only layer: dictation.py's _handle_command_mode_utterance remains the
SOLE authoritative decode/paste path (see test_transcription_params.py's
test_handle_command_mode_utterance_gates_on_session_mode /
test_handle_command_mode_utterance_forces_english_on_every_mode, both still
passing unmodified -- that IS the "session final path params unchanged"
assertion for this feature). Everything here exercises the NEW preview code:
samsara.streaming.DictatePreviewSession's partial-decode loop and
WakeConsumer.snapshot_dictate_preview_audio's tail-window snapshot, plus
dictation.py's lifecycle wiring (_ensure_streaming_preview/
_release_streaming_preview/_update_streaming_preview), against minimal
duck-typed doubles -- same philosophy as test_wake_consumer_lifecycle.py and
test_transcription_params.py's _base_fake_app.
"""
import threading
import types

import numpy as np
import pytest

from unittest.mock import Mock

import dictation
from samsara.audio_engine.frame import FRAME_MS, SAMPLE_RATE
from samsara.audio_engine.wake_consumer import WakeConsumer, _PREVIEW_TAIL_S
from samsara.session_modes import (
    DispatchOutcome,
    GLOBAL_SESSION_EXIT_PHRASES,
    SessionMode,
    SessionModeManager,
)
from samsara.streaming import (
    DictatePreviewSession,
    DICTATE_PREVIEW_TRANSCRIPT_MAX_UTTERANCES,
    PARTIAL_BEAM,
    StreamingOverlayQt,
)


# ============================================================================
# WakeConsumer.snapshot_dictate_preview_audio -- tail-window snapshot.
# ============================================================================

class _FakeApp:
    def __init__(self, mode=SessionMode.DICTATE, command_mode_active=True):
        self.command_mode_active = command_mode_active
        self.config = {'command_mode': {'mode': 'toggle'}}
        self._session_mode_manager = types.SimpleNamespace(mode=mode)


def _bare_consumer():
    consumer = WakeConsumer.__new__(WakeConsumer)
    consumer._utterance_frames = []
    return consumer


class TestSnapshotDictatePreviewAudio:
    def test_none_outside_dictate_lane(self):
        consumer = _bare_consumer()
        consumer._app = _FakeApp(mode=SessionMode.COMMAND)
        consumer._utterance_frames = [np.ones(1600, dtype=np.float32)]
        assert consumer.snapshot_dictate_preview_audio() is None

    def test_none_when_not_in_toggle_command_mode(self):
        consumer = _bare_consumer()
        consumer._app = _FakeApp(mode=SessionMode.DICTATE, command_mode_active=False)
        consumer._utterance_frames = [np.ones(1600, dtype=np.float32)]
        assert consumer.snapshot_dictate_preview_audio() is None

    def test_none_when_buffer_empty(self):
        consumer = _bare_consumer()
        consumer._app = _FakeApp()
        consumer._utterance_frames = []
        assert consumer.snapshot_dictate_preview_audio() is None

    def test_returns_concatenated_buffer_when_short(self):
        consumer = _bare_consumer()
        consumer._app = _FakeApp()
        frames = [np.full(1600, i, dtype=np.float32) for i in range(3)]
        consumer._utterance_frames = frames
        out = consumer.snapshot_dictate_preview_audio()
        assert out is not None
        assert out.shape == (1600 * 3,)

    def test_truncates_to_tail_window_when_buffer_exceeds_it(self):
        """DICTATE is exempt from the 7s hard cap (_hard_cap_applies), so the
        buffer can grow past _PREVIEW_TAIL_S -- only the most recent slice
        must be decoded, not the whole growing buffer."""
        consumer = _bare_consumer()
        consumer._app = _FakeApp()
        tail_frame_count = int(_PREVIEW_TAIL_S * 1000 / FRAME_MS)
        total_frames = tail_frame_count + 20
        # Each frame tagged with its index so we can verify only the TAIL survived.
        frames = [np.full(1600, i, dtype=np.float32) for i in range(total_frames)]
        consumer._utterance_frames = frames
        out = consumer.snapshot_dictate_preview_audio()
        assert out.shape == (1600 * tail_frame_count,)
        # First sample of the returned tail must match the first surviving
        # (not-dropped) frame's tag, i.e. frame index `total_frames - tail_frame_count`.
        assert out[0] == total_frames - tail_frame_count

    def test_does_not_mutate_the_live_buffer(self):
        """Read-only: the poll thread's own buffer must be untouched (a
        snapshot that trims/clears it would corrupt the in-progress
        utterance the FIFO worker will eventually transcribe for real)."""
        consumer = _bare_consumer()
        consumer._app = _FakeApp()
        frames = [np.ones(1600, dtype=np.float32) for _ in range(5)]
        consumer._utterance_frames = frames
        consumer.snapshot_dictate_preview_audio()
        assert len(consumer._utterance_frames) == 5
        assert consumer._utterance_frames is frames


# ============================================================================
# DictatePreviewSession -- partial-decode loop.
# ============================================================================

class _FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        seg = types.SimpleNamespace(text=" hello world")
        return [seg], types.SimpleNamespace(language='en')


def _bare_preview(app):
    session = DictatePreviewSession.__new__(DictatePreviewSession)
    session.app = app
    session._stop_event = threading.Event()
    session._overlay = types.SimpleNamespace(
        show=lambda: None, close=lambda: None,
        update_text=lambda *a, **k: None,
        set_transcript=lambda *a, **k: None,
        flash_done_and_fade=lambda cb: cb() if cb else None,
    )
    session._closed = False
    session._finalized = []
    return session


def _base_preview_app(get_transcription_params=None):
    captured = {}

    def _default_get_transcription_params(include_vocabulary=True):
        captured['include_vocabulary'] = include_vocabulary
        return {
            'language': 'fr', 'initial_prompt': '', 'no_speech_threshold': 0.6,
            'log_prob_threshold': -1.0, 'beam_size': 3, 'vad_filter': True,
            'condition_on_previous_text': False, 'without_timestamps': True,
            'word_timestamps': False,
        }

    consumer = types.SimpleNamespace(
        snapshot_dictate_preview_audio=lambda: np.ones(16000, dtype=np.float32),
    )
    app = types.SimpleNamespace(
        model=_FakeModel(),
        model_lock=threading.Lock(),
        model_rate=16000,
        _wake_consumer=consumer,
        get_transcription_params=get_transcription_params or _default_get_transcription_params,
    )
    return app, captured


class TestTranscribePartialSkipsWhenModelBusy:
    def test_returns_none_and_never_touches_wake_consumer_when_lock_held(self):
        app, _captured = _base_preview_app()
        touched = []
        app._wake_consumer = types.SimpleNamespace(
            snapshot_dictate_preview_audio=lambda: touched.append(1) or None,
        )
        session = _bare_preview(app)

        app.model_lock.acquire()  # simulate a final/hotkey decode in flight
        try:
            result = session._transcribe_partial()
        finally:
            app.model_lock.release()

        assert result is None
        assert touched == [], "must skip the tick entirely, never queue behind the holder"
        assert app.model.calls == []

    def test_decodes_when_lock_free(self):
        app, _captured = _base_preview_app()
        session = _bare_preview(app)
        result = session._transcribe_partial()
        assert result == "hello world"  # raw join+strip -- no capitalize pass, overlay-only text
        assert len(app.model.calls) == 1


class TestSnapshotAudio:
    def test_none_when_wake_consumer_missing(self):
        app, _ = _base_preview_app()
        app._wake_consumer = None
        session = _bare_preview(app)
        assert session._snapshot_audio() is None

    def test_none_when_consumer_returns_none(self):
        app, _ = _base_preview_app()
        app._wake_consumer = types.SimpleNamespace(
            snapshot_dictate_preview_audio=lambda: None,
        )
        session = _bare_preview(app)
        assert session._snapshot_audio() is None

    def test_none_when_below_minimum_duration(self):
        app, _ = _base_preview_app()
        app._wake_consumer = types.SimpleNamespace(
            snapshot_dictate_preview_audio=lambda: np.ones(100, dtype=np.float32),
        )
        session = _bare_preview(app)
        assert session._snapshot_audio() is None


class TestPartialParamsContract:
    def test_omits_vocabulary_and_forces_english(self):
        """Mirrors _handle_command_mode_utterance's own DICTATE/AVA override
        (c98c677 / the AVA-language-forcing follow-up): free-form prose,
        never matched against the command registry."""
        app, captured = _base_preview_app()
        session = _bare_preview(app)
        params = session._partial_params()
        assert captured['include_vocabulary'] is False
        assert params['language'] == 'en'
        assert params['vad_filter'] is False
        assert params['beam_size'] == PARTIAL_BEAM
        assert params['condition_on_previous_text'] is False

    def test_get_transcription_params_failure_falls_back_safely(self):
        def _raising(include_vocabulary=True):
            raise RuntimeError("boom")

        app, _ = _base_preview_app(get_transcription_params=_raising)
        session = _bare_preview(app)
        params = session._partial_params()  # must not raise
        assert params['language'] == 'en'


# ============================================================================
# _is_control_phrase -- reuses the SAME authoritative matchers
# dispatch_utterance() itself checks. A real SessionModeManager is
# constructed (its own docstring: "unit-testable without mocking hardware")
# rather than duck-typing is_scratch_that/is_dictate_commit/match_switch_word/
# match_ava_invocation/_matches_abort_phrase by hand -- that would just
# reimplement the exact logic this predicate exists to REUSE, and could
# silently drift from the real matchers exactly like a hand-rolled dispatch
# stub would (same "call the real production code" philosophy as
# test_wake_consumer_lifecycle.py / test_transcription_params.py).
# ============================================================================

def _real_session_manager(ava_invocations=None):
    abort_phrases = list(dict.fromkeys([
        'cancel', 'cancel dictation', 'abort', *GLOBAL_SESSION_EXIT_PHRASES,
    ]))
    return SessionModeManager(
        abort_phrases=abort_phrases,
        foreground_exe_resolver=lambda: 'test.exe',
        inject_fn=lambda *a, **k: True,
        remove_chars_fn=lambda n: None,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text, ctx: None,
        ava_invocations=ava_invocations,
    )


def _bare_preview_with_real_manager(ava_invocations=None):
    manager = _real_session_manager(ava_invocations=ava_invocations)
    app = types.SimpleNamespace(_ensure_session_mode_manager=lambda: manager)
    return _bare_preview(app)


class TestIsControlPhrase:
    def test_scratch_that_is_a_control_phrase(self):
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase("scratch that") is True

    def test_literal_scratch_that_is_dictation_not_control(self):
        """match_literal_payload's escape hatch -- no special-case code
        needed, falls out of every matcher requiring whole-utterance
        equality (see _is_control_phrase's own docstring)."""
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase("literal scratch that") is False

    @pytest.mark.parametrize("text", ["end", "and", "End.", "AND"])
    def test_dictate_commit_word_and_homophone_are_control_phrases(self, text):
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase(text) is True

    @pytest.mark.parametrize("text", ["command mode", "dictate mode", "dictate"])
    def test_switch_words_are_control_phrases(self, text):
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase(text) is True

    def test_ava_invocation_is_a_control_phrase(self):
        session = _bare_preview_with_real_manager(ava_invocations=['hey ava'])
        assert session._is_control_phrase("hey ava") is True

    def test_bare_ava_is_not_a_control_phrase_when_not_configured(self):
        """Bare "ava" is deliberately excluded from DEFAULT_AVA_INVOCATIONS
        (2026-07-18 "Ava Omniscience Mode" incident) -- the preview must
        not suppress it either, matching dispatch_utterance's own
        behavior."""
        session = _bare_preview_with_real_manager(ava_invocations=['hey ava'])
        assert session._is_control_phrase("ava") is False

    @pytest.mark.parametrize("text", GLOBAL_SESSION_EXIT_PHRASES)
    def test_exit_phrases_are_control_phrases(self, text):
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase(text) is True

    def test_configured_abort_word_is_also_a_control_phrase(self):
        """_matches_abort_phrase covers the user's configured cancel/abort
        words too, not just the bare GLOBAL_SESSION_EXIT_PHRASES tuple --
        reusing the manager's real matcher (word-boundary regex, substring
        anywhere) rather than a whole-utterance-only reimplementation."""
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase("please cancel dictation now") is True

    def test_ordinary_prose_is_not_a_control_phrase(self):
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase("the quick brown fox jumps") is False

    def test_empty_text_is_not_a_control_phrase(self):
        session = _bare_preview_with_real_manager()
        assert session._is_control_phrase("") is False

    def test_missing_session_manager_fails_open_to_not_control(self):
        """Best-effort: if the control-phrase check itself can't run, this
        preview-only display path fails open (shows the text) rather than
        raising or silently going blank -- matches the file's existing
        best-effort philosophy (e.g. _partial_params' own failure
        fallback)."""
        app = types.SimpleNamespace()  # no _ensure_session_mode_manager at all
        session = _bare_preview(app)
        assert session._is_control_phrase("scratch that") is False


class TestLoopSuppressesControlPhrasePartials:
    def _run_one_tick(self, session, partial_text):
        session._transcribe_partial = lambda: partial_text
        stop_event = Mock()
        stop_event.wait.return_value = False
        # is_set() consulted twice per real tick (outer while guard, then
        # the post-transcribe cancel check) before the NEXT outer while
        # check ends the loop -- see DictatePreviewSession._loop.
        stop_event.is_set.side_effect = [False, False, True]
        session._stop_event = stop_event
        session._loop()

    def test_control_phrase_partial_is_not_rendered(self):
        session = _bare_preview_with_real_manager()
        calls = []
        session._overlay = types.SimpleNamespace(
            set_transcript=lambda *a, **k: calls.append(a))
        self._run_one_tick(session, "scratch that")
        assert calls == []

    def test_ordinary_partial_is_still_rendered(self):
        session = _bare_preview_with_real_manager()
        calls = []
        session._overlay = types.SimpleNamespace(
            set_transcript=lambda *a, **k: calls.append(a))
        self._run_one_tick(session, "the weather today")
        assert calls == [([], "the weather today")]


class TestOnUtteranceFinal:
    """Persistent rolling transcript, no hard clear (2026-07-18 follow-up:
    the original flash+clear-to-"" model punished the user for a natural
    mid-thought pause by erasing the words just spoken)."""

    def test_appends_final_text_and_does_not_clear(self):
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session.on_utterance_final("hello world")
        assert session._finalized == ["hello world"]

    def test_second_final_appends_rather_than_replacing(self):
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session.on_utterance_final("first thought")
        session.on_utterance_final("second thought")
        assert session._finalized == ["first thought", "second thought"]

    def test_renders_via_set_transcript_with_cleared_partial(self):
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        events = []
        session._overlay = types.SimpleNamespace(
            set_transcript=lambda lines, partial: events.append((list(lines), partial)),
        )
        session.on_utterance_final("hello world")
        assert events == [(["hello world"], "")]

    def test_flash_done_and_fade_is_not_called_on_the_per_utterance_path(self):
        """flash_done_and_fade stays intact on StreamingOverlayQt for
        StreamingSession's own use -- it must simply never be reached from
        this path anymore."""
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        flash_calls = []
        session._overlay = types.SimpleNamespace(
            set_transcript=lambda *a, **k: None,
            flash_done_and_fade=lambda cb: flash_calls.append(cb),
        )
        session.on_utterance_final("hello world")
        assert flash_calls == []

    def test_blank_or_whitespace_final_text_is_not_appended(self):
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session.on_utterance_final("   ")
        assert session._finalized == []

    def test_control_phrase_final_does_not_grow_the_transcript(self):
        """The bug this task fixes: "scratch that" spoken in DICTATE must
        never become a persisted transcript line -- against a REAL
        SessionModeManager (see TestIsControlPhrase's own rationale for why
        a real one, not a duck-typed stub)."""
        session = _bare_preview_with_real_manager()
        session.on_utterance_final("scratch that")  # scratch_success=False (default)
        assert session._finalized == []

    @pytest.mark.parametrize("text", ["end", "and", "command mode", "hey ava"])
    def test_other_control_phrases_final_do_not_grow_the_transcript(self, text):
        session = _bare_preview_with_real_manager(ava_invocations=['hey ava'])
        session.on_utterance_final(text)
        assert session._finalized == []

    def test_ordinary_prose_final_does_grow_the_transcript(self):
        """Same real-manager setup as the control-phrase tests above --
        confirms suppression is specific to control phrases, not a
        blanket regression."""
        session = _bare_preview_with_real_manager()
        session.on_utterance_final("the weather today")
        assert session._finalized == ["the weather today"]

    def test_literal_scratch_that_final_is_dictation_and_grows_the_transcript(self):
        session = _bare_preview_with_real_manager()
        session.on_utterance_final("literal scratch that")
        assert session._finalized == ["literal scratch that"]

    def test_scratch_success_pops_the_last_finalized_line(self):
        """scratch_success=True is the REAL dispatch_utterance outcome, not
        re-derived from text -- mirrors the actual undo by popping rather
        than merely suppressing."""
        session = _bare_preview_with_real_manager()
        session.on_utterance_final("first thought")
        session.on_utterance_final("second thought")
        session.on_utterance_final("scratch that", scratch_success=True)
        assert session._finalized == ["first thought"]

    def test_scratch_success_with_nothing_to_pop_is_a_safe_no_op(self):
        session = _bare_preview_with_real_manager()
        session.on_utterance_final("scratch that", scratch_success=True)
        assert session._finalized == []

    def test_refused_scratch_that_neither_pops_nor_appends(self):
        """scratch_success=False (the default) for a scratch-that utterance
        means the real undo did NOT happen (e.g. a stale focus lock, see
        SessionModeManager._do_scratch_that) -- the preview must not pop
        content that's still actually there, and must not append "scratch
        that" itself as if it were dictated."""
        session = _bare_preview_with_real_manager()
        session.on_utterance_final("first thought")
        session.on_utterance_final("scratch that", scratch_success=False)
        assert session._finalized == ["first thought"]

    def test_rolling_cap_drops_oldest_from_the_top(self):
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        cap = DICTATE_PREVIEW_TRANSCRIPT_MAX_UTTERANCES
        for i in range(cap + 2):
            session.on_utterance_final(f"utterance {i}")
        assert len(session._finalized) == cap
        expected = [f"utterance {i}" for i in range(2, cap + 2)]
        assert session._finalized == expected

    def test_dictate_committed_clears_the_finalized_transcript(self):
        """2026-07-19 dogfooding fix: a successful "end" commit just pasted
        every staged line into the target -- the overlay must not keep
        showing already-delivered content."""
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session.on_utterance_final("first thought")
        session.on_utterance_final("second thought")
        # "end" itself is a control phrase (is_dictate_commit) so it is
        # never appended -- only the dictate_committed flag matters here.
        session.on_utterance_final("end", dictate_committed=True)
        assert session._finalized == []

    def test_overlay_stays_open_after_a_committed_clear(self):
        """The overlay object itself is untouched -- only its transcript
        resets. The session continues for the next staged buffer."""
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session.on_utterance_final("first thought")
        session.on_utterance_final("end", dictate_committed=True)
        assert session._closed is False

    def test_dictate_committed_renders_empty_transcript_via_set_transcript(self):
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        events = []
        session._overlay = types.SimpleNamespace(
            set_transcript=lambda lines, partial: events.append((list(lines), partial)),
        )
        session.on_utterance_final("first thought")
        events.clear()
        session.on_utterance_final("end", dictate_committed=True)
        assert events == [([], "")]

    def test_failed_or_refused_commit_retains_lines(self):
        """dictate_committed=False (the default -- what the real call site
        passes for dictate_commit_refused / dictate_commit_blocked_focus_lock
        / dictate_commit_failed) must retain the already-staged transcript;
        nothing was actually delivered. Uses the real SessionModeManager so
        "end" is correctly recognized as a control phrase and not itself
        appended -- isolating the assertion to the dictate_committed flag."""
        session = _bare_preview_with_real_manager()
        session.on_utterance_final("first thought")
        session.on_utterance_final("end", dictate_committed=False)
        assert session._finalized == ["first thought"]

    def test_pause_between_staged_chunks_retains_lines(self):
        """Ordinary silence-boundary pauses within manual-commit DICTATE
        staging (outcome "dictate_staged") never pass dictate_committed --
        the transcript must keep growing across a pause, unchanged from the
        pre-existing 2026-07-18 persistent-transcript behavior."""
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session.on_utterance_final("first chunk")
        session.on_utterance_final("second chunk")  # a later chunk after a pause
        assert session._finalized == ["first chunk", "second chunk"]

    def test_no_op_after_closed(self):
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session._closed = True
        calls = []
        session._overlay = types.SimpleNamespace(
            set_transcript=lambda *a, **k: calls.append(1),
        )
        session.on_utterance_final("hello world")
        assert calls == []
        assert session._finalized == []


class TestStartResetsTranscript:
    def test_re_entering_dictate_resets_the_transcript_to_empty(self, monkeypatch):
        """start() is called on every DICTATE re-entry (a fresh
        DictatePreviewSession per dictation.py's _ensure_streaming_preview,
        but start() also resets explicitly -- see its own comment).

        thread_registry.spawn is no-op'd -- start() spawns a REAL daemon
        thread running _loop() at ~1s cadence against this test's
        duck-typed app; left un-mocked it leaks a live background thread
        for the rest of the test process (only surfaced as a visible
        logging error once _is_control_phrase gave that thread a new,
        frequently-hit failure path to log from -- the leak itself predates
        this task)."""
        monkeypatch.setattr('samsara.streaming.thread_registry.spawn', lambda *a, **k: None)
        app, _ = _base_preview_app()
        session = _bare_preview(app)
        session._finalized = ["stale", "from a previous DICTATE entry"]
        session.start()
        assert session._finalized == []


class TestSetTranscriptRendering:
    """StreamingOverlayQt.set_transcript -- the minimal addition this
    feature made on top of the existing update_text() plumbing."""

    def test_plain_text_when_only_one_finalized_line_and_no_partial(self):
        overlay = StreamingOverlayQt.__new__(StreamingOverlayQt)
        calls = []
        overlay.update_text = lambda text, state: calls.append((text, state))
        overlay.set_transcript(["hello world"], "")
        assert calls == [("hello world", StreamingOverlayQt.STATE_LISTENING)]

    def test_multiple_finalized_lines_joined_with_br(self):
        overlay = StreamingOverlayQt.__new__(StreamingOverlayQt)
        calls = []
        overlay.update_text = lambda text, state: calls.append((text, state))
        overlay.set_transcript(["first", "second"], "")
        assert calls[0][0] == "first<br>second"

    def test_partial_appended_in_a_distinct_span(self):
        overlay = StreamingOverlayQt.__new__(StreamingOverlayQt)
        calls = []
        overlay.update_text = lambda text, state: calls.append((text, state))
        overlay.set_transcript(["settled"], "live words")
        text, state = calls[0]
        assert text.startswith("settled<br>")
        assert "live words" in text
        assert "<span" in text  # visually distinct from settled text

    def test_partial_only_when_no_finalized_lines_yet(self):
        overlay = StreamingOverlayQt.__new__(StreamingOverlayQt)
        calls = []
        overlay.update_text = lambda text, state: calls.append((text, state))
        overlay.set_transcript([], "just started talking")
        text, _state = calls[0]
        assert "just started talking" in text
        assert not text.startswith("<br>")

    def test_html_is_escaped(self):
        overlay = StreamingOverlayQt.__new__(StreamingOverlayQt)
        calls = []
        overlay.update_text = lambda text, state: calls.append((text, state))
        overlay.set_transcript(["less <than> five & six"], "")
        text, _state = calls[0]
        assert "<than>" not in text
        assert "&lt;than&gt;" in text

    def test_falls_back_to_listening_placeholder_when_empty(self):
        overlay = StreamingOverlayQt.__new__(StreamingOverlayQt)
        calls = []
        overlay.update_text = lambda text, state: calls.append((text, state))
        overlay.set_transcript([], "")
        assert calls == [("Listening...", StreamingOverlayQt.STATE_LISTENING)]


# ============================================================================
# dictation.py wiring: config gating, ensure/release, lane transitions.
# ============================================================================

class _FakePreview:
    """Stand-in for DictatePreviewSession -- records lifecycle calls without
    touching Qt or spawning a real thread."""
    instances = []

    def __init__(self, app):
        self.app = app
        self.started = False
        self.stopped = False
        _FakePreview.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset_fake_preview_instances():
    _FakePreview.instances = []
    yield
    _FakePreview.instances = []


def _make_dictation_app(session_streaming_preview=True, monkeypatch=None):
    app = types.SimpleNamespace()
    app.config = {'command_mode': {'session_streaming_preview': session_streaming_preview}}
    app._dictate_preview = None
    app._ensure_streaming_preview = types.MethodType(
        dictation.DictationApp._ensure_streaming_preview, app)
    app._release_streaming_preview = types.MethodType(
        dictation.DictationApp._release_streaming_preview, app)
    app._update_streaming_preview = types.MethodType(
        dictation.DictationApp._update_streaming_preview, app)
    return app


class TestConfigGating:
    def test_config_off_constructs_nothing(self, monkeypatch):
        monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
        app = _make_dictation_app(session_streaming_preview=False)
        app._update_streaming_preview(SessionMode.DICTATE)
        assert app._dictate_preview is None
        assert _FakePreview.instances == []

    def test_config_on_dictate_lane_constructs_and_starts(self, monkeypatch):
        monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
        app = _make_dictation_app(session_streaming_preview=True)
        app._update_streaming_preview(SessionMode.DICTATE)
        assert app._dictate_preview is not None
        assert app._dictate_preview.started is True


class TestLaneTransitions:
    def test_dictate_to_command_stops_and_clears(self, monkeypatch):
        monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
        app = _make_dictation_app()
        app._update_streaming_preview(SessionMode.DICTATE)
        preview = app._dictate_preview
        app._update_streaming_preview(SessionMode.COMMAND)
        assert preview.stopped is True
        assert app._dictate_preview is None

    def test_command_to_dictate_resumes(self, monkeypatch):
        monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
        app = _make_dictation_app()
        app._update_streaming_preview(SessionMode.COMMAND)
        assert app._dictate_preview is None
        app._update_streaming_preview(SessionMode.DICTATE)
        assert app._dictate_preview is not None
        assert app._dictate_preview.started is True

    def test_ava_lane_never_shows_preview(self, monkeypatch):
        monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
        app = _make_dictation_app()
        app._update_streaming_preview(SessionMode.AVA)
        assert app._dictate_preview is None

    def test_ensure_is_idempotent(self, monkeypatch):
        monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
        app = _make_dictation_app()
        app._update_streaming_preview(SessionMode.DICTATE)
        first = app._dictate_preview
        app._update_streaming_preview(SessionMode.DICTATE)
        assert app._dictate_preview is first
        assert len(_FakePreview.instances) == 1

    def test_session_end_closes(self, monkeypatch):
        """exit_command_mode's toggle branch calls _release_streaming_preview
        unconditionally (reset() bypasses on_mode_change) -- simulated here
        directly against the lifecycle methods."""
        monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
        app = _make_dictation_app()
        app._update_streaming_preview(SessionMode.DICTATE)
        preview = app._dictate_preview
        app._release_streaming_preview()
        assert preview.stopped is True
        assert app._dictate_preview is None

    def test_release_when_nothing_running_is_a_safe_no_op(self):
        app = _make_dictation_app()
        app._release_streaming_preview()  # must not raise
        assert app._dictate_preview is None


# ============================================================================
# End-to-end: enter_command_mode/exit_command_mode's toggle branch, through
# the REAL bound methods (reset() bypasses on_mode_change entirely, so these
# two call sites carry their own explicit _update_streaming_preview /
# _release_streaming_preview calls -- see dictation.py's comments there).
# Mirrors test_wake_consumer_lifecycle.py's _make_toggle_session_app, but
# here the preview lifecycle methods are the REAL bound ones under test
# instead of being no-op'd.
# ============================================================================

class _FakeWakeConsumer:
    """Stand-in for WakeConsumer -- only the surface _ensure_wake_consumer/
    _release_wake_consumer touch (._running, .start(), .stop())."""

    def __init__(self):
        self._running = False

    def start(self):
        self._running = True

    def stop(self):
        self._running = False
        return []


def _make_toggle_session_app(monkeypatch, session_streaming_preview=True):
    monkeypatch.setattr('samsara.streaming.DictatePreviewSession', _FakePreview)
    app = types.SimpleNamespace()
    app._wake_consumer = _FakeWakeConsumer()
    app._wake_consumer_reasons = set()
    app._wake_consumer_lock = threading.Lock()
    app.wake_word_triggered = False
    app.process_wake_word_buffer = lambda *a, **k: None
    app._ensure_wake_consumer = types.MethodType(dictation.DictationApp._ensure_wake_consumer, app)
    app._release_wake_consumer = types.MethodType(dictation.DictationApp._release_wake_consumer, app)

    app.command_mode_active = False
    app.ava_mode_active = False
    # enter_command_mode() now exits an active AI-command session first
    # (2026-07-19 incident fix) -- always False here, so that branch is a
    # no-op, but the attribute must exist for the check itself.
    app.ai_command_mode_active = False
    app._command_mode_lock = threading.Lock()
    app._command_mode_miss_count = 0
    app._command_mode_session_start = 0.0
    app._command_mode_ghost_tap = False
    app.recording = False
    app._session_mode_manager = None
    app._dictate_preview = None
    app.config = {
        'command_mode': {
            'mode': 'toggle',
            'inactivity_timeout_s': 300,
            'enter_debounce_ms': 0,
            'exit_earcon': False,
            'session_streaming_preview': session_streaming_preview,
        },
    }
    app._reset_command_mode_inactivity_timer = lambda timeout_s: None
    app._cancel_command_mode_inactivity_timer = lambda: None
    app._ensure_session_mode_manager = lambda: types.SimpleNamespace(reset=lambda **kw: None)
    app._update_mode_overlay = lambda mode: None
    app.play_sound = lambda *a, **k: None
    app.stop_recording = lambda: None
    app._do_enter_command_mode = lambda: None
    monkeypatch.setattr(dictation.thread_registry, 'spawn', lambda *a, **k: None)

    app._ensure_streaming_preview = types.MethodType(
        dictation.DictationApp._ensure_streaming_preview, app)
    app._release_streaming_preview = types.MethodType(
        dictation.DictationApp._release_streaming_preview, app)
    app._update_streaming_preview = types.MethodType(
        dictation.DictationApp._update_streaming_preview, app)
    app.enter_command_mode = types.MethodType(dictation.DictationApp.enter_command_mode, app)
    app.exit_command_mode = types.MethodType(dictation.DictationApp.exit_command_mode, app)
    return app


class TestToggleSessionStreamingPreviewWiring:
    def test_entering_toggle_session_starts_the_preview(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch)
        app.enter_command_mode()
        assert app._dictate_preview is not None
        assert app._dictate_preview.started is True

    def test_exiting_toggle_session_stops_the_preview(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch)
        app.enter_command_mode()
        preview = app._dictate_preview
        app.exit_command_mode()
        assert preview.stopped is True
        assert app._dictate_preview is None

    def test_config_off_never_starts_the_preview(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch, session_streaming_preview=False)
        app.enter_command_mode()
        assert app._dictate_preview is None
        assert _FakePreview.instances == []

    def test_double_enter_does_not_double_start(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch)
        app.enter_command_mode()
        app.enter_command_mode()  # command_mode_active guard makes this a no-op
        assert len(_FakePreview.instances) == 1


class TestSessionExitReleasesPreviewEvenOnCleanupFailure:
    """2026-07-19 dogfooding fix: exit_command_mode() is the single funnel
    every session-exit path (toggle-off key, inactivity timeout, global
    abort phrase, WakeConsumer poll-loop crash -- see wake_consumer.py's
    _poll_loop crash handler) goes through. Previously, if
    _session_mode_manager.reset() or _release_wake_consumer() raised, the
    _release_streaming_preview() call below them in exit_command_mode was
    skipped entirely -- the DICTATE overlay kept running after the session
    had already ended, and none of those callers made up for it
    (wake_consumer.py's crash handler swallows the exception with a bare
    `except: pass`; _on_command_mode_inactivity's own except-fallback
    force-clears command_mode_active without its own release call). The
    fix wraps the risky cleanup in try/finally so the release is
    unconditional regardless of what else in the method raises."""

    def test_release_wake_consumer_raising_still_releases_the_preview(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch)
        app.enter_command_mode()
        preview = app._dictate_preview
        assert preview is not None

        def _raise(reason):
            raise RuntimeError("wake consumer release failed")
        app._release_wake_consumer = _raise

        with pytest.raises(RuntimeError):
            app.exit_command_mode()

        assert preview.stopped is True
        assert app._dictate_preview is None

    def test_session_mode_manager_reset_raising_still_releases_the_preview(self, monkeypatch):
        app = _make_toggle_session_app(monkeypatch)
        app.enter_command_mode()
        preview = app._dictate_preview
        assert preview is not None

        class _RaisingManager:
            def reset(self, **kw):
                raise RuntimeError("reset failed")

        app._session_mode_manager = _RaisingManager()

        with pytest.raises(RuntimeError):
            app.exit_command_mode()

        assert preview.stopped is True
        assert app._dictate_preview is None


# ============================================================================
# _handle_command_mode_utterance -- the on_utterance_final() hook. Reuses
# test_command_mode_utterance_hallucination.py's _make_app/_seg/_buffer_for
# integration-test shape (fake Whisper decode, real method under test).
# ============================================================================

def _seg(text, compression_ratio=1.0, no_speech_prob=0.0):
    return types.SimpleNamespace(
        text=text, compression_ratio=compression_ratio,
        no_speech_prob=no_speech_prob, avg_logprob=-0.1,
    )


def _buffer_for(duration_s=1.0, rate=16000):
    return [np.zeros(int(duration_s * rate), dtype=np.float32)]


def _make_utterance_app(mode, dictate_preview=None, outcome_kind="dictate_staged"):
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app._wake_transcription_in_progress = False
    app.model_rate = 16000
    app.model_lock = Mock()
    app.model_lock.__enter__ = Mock(return_value=None)
    app.model_lock.__exit__ = Mock(return_value=False)
    app.model = Mock()
    app.model.transcribe = Mock(return_value=([_seg("hello world")], types.SimpleNamespace()))
    app.get_transcription_params = Mock(return_value={})
    app.voice_training_window = Mock()
    app.voice_training_window.apply_corrections = Mock(side_effect=lambda t: t)
    app._command_mode_ghost_tap = False
    app._compute_switch_gate_signals = Mock(return_value=Mock())
    app._handle_session_dispatch_outcome = Mock()
    app.play_sound = Mock()
    app._vad_reset = Mock()
    app._dictate_preview = dictate_preview

    manager = Mock()
    manager.mode = mode
    manager.dispatch_utterance = Mock(
        return_value=DispatchOutcome(kind=outcome_kind, detail={}))
    app._ensure_session_mode_manager = Mock(return_value=manager)
    return app, manager


class TestHandleCommandModeUtteranceOnFinalHook:
    def test_dictate_lane_final_notifies_the_preview_with_the_final_text(self):
        """The authoritative final string dispatch_utterance itself used
        (`text` in _handle_command_mode_utterance) is threaded through, not
        re-derived -- the fake model decode below returns "hello world"."""
        preview = Mock()
        app, _manager = _make_utterance_app(SessionMode.DICTATE, dictate_preview=preview)
        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)
        preview.on_utterance_final.assert_called_once_with(
            "hello world", scratch_success=False, dictate_committed=False)

    def test_scratch_success_outcome_is_threaded_through_as_the_real_signal(self):
        """dispatch_utterance's own outcome.kind == "scratch_success" -- not
        re-derived by text-matching "hello world" (the fixture's fake
        decode) -- proves this is threaded from the real outcome, not
        guessed from the utterance text."""
        preview = Mock()
        app, _manager = _make_utterance_app(
            SessionMode.DICTATE, dictate_preview=preview, outcome_kind="scratch_success")
        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)
        preview.on_utterance_final.assert_called_once_with(
            "hello world", scratch_success=True, dictate_committed=False)

    def test_dictate_committed_outcome_is_threaded_through_as_the_real_signal(self):
        """2026-07-19 dogfooding fix: outcome.kind == "dictate_committed" is
        threaded through the same way scratch_success is -- not re-derived
        from text (the fake decode's "hello world" is not itself a control
        phrase)."""
        preview = Mock()
        app, _manager = _make_utterance_app(
            SessionMode.DICTATE, dictate_preview=preview, outcome_kind="dictate_committed")
        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)
        preview.on_utterance_final.assert_called_once_with(
            "hello world", scratch_success=False, dictate_committed=True)

    @pytest.mark.parametrize("outcome_kind", [
        "dictate_commit_refused",
        "dictate_commit_blocked_focus_lock",
        "dictate_commit_failed",
    ])
    def test_refused_or_failed_commit_does_not_set_dictate_committed(self, outcome_kind):
        """A commit that did NOT actually deliver anything must not clear
        the overlay's transcript -- see the parallel
        TestOnUtteranceFinal::test_failed_or_refused_commit_retains_lines."""
        preview = Mock()
        app, _manager = _make_utterance_app(
            SessionMode.DICTATE, dictate_preview=preview, outcome_kind=outcome_kind)
        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)
        preview.on_utterance_final.assert_called_once_with(
            "hello world", scratch_success=False, dictate_committed=False)

    def test_command_lane_final_does_not_touch_the_preview(self):
        """COMMAND-lane utterances never showed a preview in the first
        place (see _update_streaming_preview's DICTATE-only gate) -- a
        preview object could still be set here in principle (a mode switch
        landing back in COMMAND while a stale reference lingered), so this
        locks in that the hook is gated on the utterance's OWN captured
        lane, not merely "is a preview object present."""
        preview = Mock()
        app, _manager = _make_utterance_app(SessionMode.COMMAND, dictate_preview=preview)
        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)
        preview.on_utterance_final.assert_not_called()

    def test_dictate_lane_final_with_no_preview_running_is_safe(self):
        app, _manager = _make_utterance_app(SessionMode.DICTATE, dictate_preview=None)
        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)  # must not raise

    def test_preview_hook_failure_does_not_break_dispatch(self):
        """Best-effort per the task's own invariant: if the preview fails,
        dictation must be byte-identical to today -- a raising
        on_utterance_final must not prevent _handle_session_dispatch_outcome
        (already called earlier in the try block) from having taken effect,
        nor propagate out of the method."""
        preview = Mock()
        preview.on_utterance_final.side_effect = RuntimeError("boom")
        app, manager = _make_utterance_app(SessionMode.DICTATE, dictate_preview=preview)
        dictation.DictationApp._handle_command_mode_utterance(app, _buffer_for(), 16000)  # must not raise
        manager.dispatch_utterance.assert_called_once()
        app._handle_session_dispatch_outcome.assert_called_once()
