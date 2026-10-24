"""The retired Left Alt notice must not run during TTS startup."""

from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import dictation


@pytest.mark.parametrize("hints_enabled", [False, True])
def test_tts_startup_never_speaks_or_shows_retired_left_alt_notice(
        monkeypatch, hints_enabled):
    from samsara.tts import runtime

    monkeypatch.setattr(runtime, "rebuild_tts_runtime", Mock())
    monkeypatch.setattr(runtime, "tts_runtime_lock", lambda _app: nullcontext())

    shown = []
    hints = SimpleNamespace(
        _enabled=hints_enabled,
        maybe_show=lambda hint_id, _message: shown.append(hint_id),
    )
    speak = Mock()
    app = dictation.DictationApp.__new__(dictation.DictationApp)
    app.config = {"tts": {}}
    app.tts_ready = Event()
    app.hints = hints
    app.audio_coordinator = SimpleNamespace(speak=speak)

    app._tts_init_worker()

    assert not any("Left" in str(call) for call in speak.call_args_list)
    assert "ava_command_session_migration_v1" not in shown
