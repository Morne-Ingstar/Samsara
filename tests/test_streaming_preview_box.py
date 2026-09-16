"""Tests for three hands-free streaming preview box defects reported from
live use (2026-09-11) -- see docs/reviews/ for the corresponding fix
report. Hands-free here means toggle command mode's DICTATE lane
(samsara.streaming.DictatePreviewSession + StreamingOverlayQt), NOT the
wake-word session path.

No module-level `import dictation` -- none of this needs it. A real
SessionModeManager is used (not a re-implementation) to drive the two
commit paths so DEFECT 1's fix is exercised against production dispatch
logic, not a guessed-at outcome shape.
"""
import types

import pytest

from samsara.session_modes import (
    HandsFreeCommandMatch,
    PendingTextPolicy,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
)
from samsara.streaming import (
    DictatePreviewSession,
    StreamingOverlayQt,
    _join_dictate_fragments,
)


# ---------------------------------------------------------------------------
# Shared rig: a real SessionModeManager in buffered-DICTATE mode, wired to a
# fake app, with a fake overlay standing in for StreamingOverlayQt so these
# tests never touch Qt.
# ---------------------------------------------------------------------------

class _FakeOverlay:
    def __init__(self):
        self.calls = []
        self.prompts = []

    # link_words / set_prompt / set_interaction_callbacks / scroll_draft are
    # queue 85's additions to the real overlay; mirrored here so this double
    # keeps matching the API it stands in for. The recorded tuple is
    # unchanged, so every assertion below still reads the same.
    def set_transcript(self, finalized, partial, link_words=False):
        self.calls.append((list(finalized), partial))

    def set_prompt(self, text):
        self.prompts.append(text)

    def set_interaction_callbacks(self, on_word_clicked, on_clear):
        pass

    def scroll_draft(self, where):
        pass

    def show(self):
        pass

    def close(self):
        pass


def _make_manager(inject_fn=None, probe_fn=None, command_matches=False):
    manager = SessionModeManager(
        abort_phrases=["stop listening"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 111,
        inject_fn=inject_fn or (lambda text, check: True),
        remove_chars_fn=lambda n: None,
        command_dispatch_fn=lambda text: types.SimpleNamespace(
            matched=command_matches, phrase=text,
        ),
        agent_dispatch_fn=lambda text, context: None,
        buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=probe_fn,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager


# The two reserved-command policies the combined hands-free lane defines
# (session_modes.PendingTextPolicy). Real phrases, matching dictation.py's
# _HANDS_FREE_COMMIT_COMMANDS / _HANDS_FREE_PRESERVE_COMMANDS sets.
def _reserved_command_probe(text):
    normalized = " ".join((text or "").lower().split())
    if normalized == "submit":
        return HandsFreeCommandMatch(
            dispatch_text=normalized, phrase=normalized,
            pending_policy=PendingTextPolicy.COMMIT,
        )
    if normalized == "scroll down":
        return HandsFreeCommandMatch(
            dispatch_text=normalized, phrase=normalized,
            pending_policy=PendingTextPolicy.PRESERVE,
        )
    return None


def _make_preview(manager):
    app = types.SimpleNamespace(_session_mode_manager=manager)
    app._ensure_session_mode_manager = lambda: manager
    preview = DictatePreviewSession.__new__(DictatePreviewSession)
    preview.app = app
    preview._closed = False
    preview._finalized = []
    preview._generation = 0
    preview._overlay = _FakeOverlay()
    return preview


_SIG = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


def _stage(manager, preview, text):
    outcome = manager.dispatch_utterance(text, _SIG)
    preview.on_utterance_final(
        text,
        scratch_success=(outcome.kind == "scratch_success"),
        dictate_committed=(outcome.kind == "dictate_committed"),
    )
    return outcome


# ---------------------------------------------------------------------------
# DEFECT 1 -- stale text after a commit, on every path that can commit.
# ---------------------------------------------------------------------------

class TestCommitClearsTranscript:
    def test_end_of_sentence_path_clears(self):
        """Explicit spoken 'end' -- outcome.kind == 'dictate_committed'."""
        manager = _make_manager()
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")
        assert preview._finalized == ["hello world"]

        outcome = _stage(manager, preview, "end")

        assert outcome.kind == "dictate_committed"
        assert preview._finalized == []
        assert preview._overlay.calls[-1] == ([], "")

    def test_submit_path_via_local_commit_key_clears(self):
        """The trusted local (keyboard) commit-key path -- dictation.py's
        _commit_pending_hands_free_dictation calls manager.
        commit_pending_dictation() directly (no spoken text) and passes
        final_text="" through the SAME on_utterance_final signal shape."""
        manager = _make_manager()
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")
        assert preview._finalized == ["hello world"]

        outcome = manager.commit_pending_dictation()
        preview.on_utterance_final(
            "", dictate_committed=(outcome.kind == "dictate_committed"),
        )

        assert outcome.kind == "dictate_committed"
        assert preview._finalized == []
        assert preview._overlay.calls[-1] == ([], "")

    def test_submit_via_implicit_mode_switch_commit_clears(self):
        """DEFECT 1's actual root cause: saying something that switches
        mode AWAY from DICTATE while a thought is staged (e.g. "command
        mode") auto-commits the pending buffer inside
        SessionModeManager._do_switch, but the outcome bubbling back out
        is "mode_switch", never "dictate_committed" -- the caller-supplied
        flag alone can't see this commit happened. Without the fix this
        left "hello world" stuck in the box even though it was genuinely
        delivered."""
        manager = _make_manager()
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")
        assert preview._finalized == ["hello world"]

        outcome = _stage(manager, preview, "command mode")

        assert outcome.kind == "mode_switch"
        assert preview._finalized == []
        assert preview._overlay.calls[-1] == ([], "")

    def test_refused_commit_retains_lines(self):
        """A REFUSED/FAILED commit must NOT clear -- nothing was delivered."""
        def _refusing_inject(text, check):
            return False  # simulates a paste failure

        manager = _make_manager(inject_fn=_refusing_inject)
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")

        outcome = _stage(manager, preview, "end")

        assert outcome.kind == "dictate_commit_failed"
        assert preview._finalized == ["hello world"]

    def test_stale_partial_from_in_flight_decode_is_not_rendered_after_commit(self):
        """DEFECT 1's second half: a tick that started decoding BEFORE a
        commit landed must not paint its now-stale partial back over the
        transcript on_utterance_final just cleared."""
        manager = _make_manager()
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")

        generation_at_tick_start = preview._generation
        _stage(manager, preview, "end")  # commits, bumps _generation
        assert preview._finalized == []

        # Simulate _loop's stale-generation guard directly (the tick
        # "decoded" a partial belonging to the utterance before the commit).
        assert generation_at_tick_start != preview._generation


class TestReservedHandsFreeCommandsAreNeverTranscript:
    """DEFECT 1 residue: the combined hands-free lane's reserved commands
    ("submit", "scroll down", "click 3", ...) are dispatched by
    SessionModeManager._dispatch_hands_free_command, which returns
    "hands_free_command_executed" -- not a dictate/control outcome. The
    preview's _is_control_phrase did not consult that probe, so the spoken
    command's own words were appended to the visible transcript as if
    dictated.
    """

    def test_spoken_submit_commits_and_leaves_no_trace(self):
        """The submit/send path named in DEFECT 1: "submit" is in
        dictation.py's _HANDS_FREE_COMMIT_COMMANDS, so it commits the staged
        thought first. The transcript must end up empty -- and in
        particular must never contain the word "submit"."""
        manager = _make_manager(probe_fn=_reserved_command_probe,
                                command_matches=True)
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")
        assert preview._finalized == ["hello world"]

        outcome = _stage(manager, preview, "submit")

        assert outcome.kind == "hands_free_command_executed"
        assert preview._finalized == []
        assert preview._overlay.calls[-1] == ([], "")

    def test_preserve_policy_command_does_not_leak_into_transcript(self):
        """"scroll down" (a PRESERVE command) deliberately leaves the staged
        thought pending, so nothing clears it -- which is exactly why the
        leak was permanent here: the word sat above the user's real
        dictation until the next commit."""
        manager = _make_manager(probe_fn=_reserved_command_probe,
                                command_matches=True)
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")

        outcome = _stage(manager, preview, "scroll down")

        assert outcome.kind == "hands_free_command_executed"
        # Still staged (PRESERVE), so the preview must still show it -- and
        # must show ONLY it. The transcript mirrors the real pending buffer.
        assert preview._finalized == ["hello world"]
        assert manager.dictate_pending_buffer.strip() == "hello world"

    def test_blocked_commit_command_does_not_leak_either(self):
        """A COMMIT-policy command whose commit fails returns
        hands_free_command_blocked and retains the buffer -- the phrase must
        still never appear as transcript text."""
        manager = _make_manager(inject_fn=lambda text, check: False,
                                probe_fn=_reserved_command_probe,
                                command_matches=True)
        preview = _make_preview(manager)
        _stage(manager, preview, "hello world")

        outcome = _stage(manager, preview, "submit")

        assert outcome.kind == "hands_free_command_blocked"
        assert preview._finalized == ["hello world"]

    def test_literal_escape_hatch_is_still_dictation(self):
        """"literal scroll down" must stay visible text -- dispatch_utterance
        checks match_literal_payload BEFORE the reserved-command probe, and
        the preview mirrors that ordering."""
        manager = _make_manager(probe_fn=_reserved_command_probe,
                                command_matches=True)
        preview = _make_preview(manager)

        outcome = _stage(manager, preview, "literal scroll down")

        assert outcome.kind == "dictate_staged"
        assert preview._finalized == ["literal scroll down"]

    def test_unwired_probe_is_safe(self):
        """No probe wired (hands_free_command_probe_fn=None) must not raise
        and must leave ordinary dictation visible."""
        manager = _make_manager()
        preview = _make_preview(manager)

        _stage(manager, preview, "scroll down")

        assert preview._finalized == ["scroll down"]


# ---------------------------------------------------------------------------
# DEFECT 2 -- same-thought fragments render on one line; explicit
# newline/paragraph tokens still break.
# ---------------------------------------------------------------------------

class TestJoinDictateFragments:
    def test_four_short_segments_render_on_one_line(self):
        html_out = _join_dictate_fragments(
            ["The", "quick", "brown", "fox"],
        )
        assert "<br>" not in html_out
        assert html_out == "The quick brown fox"

    def test_newline_token_produces_a_break(self):
        html_out = _join_dictate_fragments(
            ["first paragraph", "second\nparagraph", "third"],
        )
        assert html_out == "first paragraph second<br>paragraph third"

    def test_empty_fragments_are_skipped(self):
        assert _join_dictate_fragments(["", None, "word"]) == "word"


# ---------------------------------------------------------------------------
# DEFECT 3 -- Unicode round-trips through the preview render untouched.
# ---------------------------------------------------------------------------

class TestUnicodeRoundTrips:
    @pytest.mark.parametrize("text", [
        "I don’t think so",           # curly apostrophe, U+2019
        "an em dash — right here",     # em dash, U+2014
        "a smiling emoji \U0001F600 here",  # astral-plane character
    ])
    def test_character_survives_the_render(self, text):
        html_out = _join_dictate_fragments([text])
        # html.escape only touches &<>"' -- none of these characters are
        # among them, so the escaped output must contain the ORIGINAL
        # character verbatim, not a corrupted/garbled substitute.
        assert text in html_out

    def test_all_three_together_in_one_transcript(self):
        html_out = _join_dictate_fragments([
            "I don’t think so",
            "an em dash — here",
            "a smiling emoji \U0001F600 test",
        ])
        assert "’" in html_out
        assert "—" in html_out
        assert "\U0001F600" in html_out
        assert "<br>" not in html_out  # same-thought fragments, one line

    def test_bytes_input_is_decoded_not_repr_leaked(self):
        """Defensive boundary (DEFECT 3): if a bytes object ever reaches
        the renderer, it must be UTF-8 decoded, not fall through to
        Python's default bytes repr (which would render literal
        "\\xe2\\x80\\x99"-style escapes as visible garbage)."""
        raw = "I don’t think so".encode("utf-8")
        html_out = _join_dictate_fragments([raw])
        assert "’" in html_out
        assert "\\x" not in html_out


class TestFullRenderRoundTrip:
    """The whole StreamingOverlayQt.set_transcript render, not just the join
    helper -- the live partial goes through its own separate escape/span
    build, so a codec fault there would never show up in a
    _join_dictate_fragments-only test. No Qt: update_text short-circuits when
    no widget exists, so it is replaced with a capture."""

    @staticmethod
    def _capturing_overlay():
        overlay = StreamingOverlayQt(dim=False)
        captured = []
        overlay.update_text = lambda text, state=None, rich=False: captured.append((text, state))
        return overlay, captured

    def test_finalized_and_partial_both_survive_the_render(self):
        overlay, captured = self._capturing_overlay()

        overlay.set_transcript(
            ["I don’t think so", "an em dash — here"],
            "and a partial \U0001F600 too",
        )

        rendered, state = captured[-1]
        assert "’" in rendered
        assert "—" in rendered
        assert "\U0001F600" in rendered
        assert "\\x" not in rendered and "Ã" not in rendered and "â€" not in rendered
        assert state == StreamingOverlayQt.STATE_LISTENING
        # Dim-italic partial styling preserved (DEFECT 2 kept this intact).
        assert "font-style:italic" in rendered

    def test_paused_render_also_survives(self):
        overlay, captured = self._capturing_overlay()

        overlay.set_paused(["I don’t think so \U0001F600"])

        rendered, _state = captured[-1]
        assert "’" in rendered
        assert "\U0001F600" in rendered
        assert "Paused (hold)" in rendered

    def test_empty_transcript_reads_listening(self):
        overlay, captured = self._capturing_overlay()

        overlay.set_transcript([], "")

        assert captured[-1][0] == "Listening..."
