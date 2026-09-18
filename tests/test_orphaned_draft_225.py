"""Queue 225: a staged DICTATE draft must survive session exit visibly."""

import types

from samsara.session_modes import SessionMode, SessionModeManager
from samsara.streaming import DictatePreviewSession


class _Overlay:
    def __init__(self):
        self.prompts = []
        self.transcripts = []

    def set_prompt(self, text):
        self.prompts.append(text)

    def set_transcript(self, lines, partial, link_words=False):
        self.transcripts.append((list(lines), partial, link_words))


def _preview(manager):
    preview = DictatePreviewSession.__new__(DictatePreviewSession)
    preview.app = types.SimpleNamespace(_ensure_session_mode_manager=lambda: manager)
    preview._finalized = []
    preview._words = []
    preview._overlay = _Overlay()
    return preview


def test_session_exit_parks_and_next_dictate_preview_names_the_waiting_draft():
    manager = SessionModeManager(
        abort_phrases=[],
        foreground_exe_resolver=lambda: "test.exe",
        inject_fn=lambda text: text,
        remove_chars_fn=lambda count: None,
        command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text: None,
        buffer_dictate_until_commit=True,
    )
    manager._dictate_pending_buffer = "one two three"

    assert manager.retain_draft() == len("one two three")
    manager.reset(initial_mode=SessionMode.DICTATE)

    preview = _preview(manager)
    preview._show_parked_draft(manager)

    assert manager.dictate_pending_buffer == "one two three"
    assert preview._overlay.prompts == ["3 words waiting — say finish to paste, or clear"]
    assert preview._overlay.transcripts == [(["one two three"], "", True)]
