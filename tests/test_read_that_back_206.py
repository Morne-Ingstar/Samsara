"""Queue 206: accessible read-back of the latest committed utterance."""

from unittest.mock import MagicMock

from plugins.commands import core_utils
from samsara import plugin_commands
from samsara.command_catalog import build_catalog
from samsara.tts.coordinator import AVA_SPEECH_CATEGORIES, AudioCoordinator
from samsara.tts.engine_base import SpeechHandle


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def query(self, **kwargs):
        return self.rows


def _app(rows=(), live=None):
    app = MagicMock()
    app.history_store = _Store(rows)
    app._last_dictation_text = live
    app._show_outcome_chip = MagicMock()
    app.audio_coordinator = MagicMock()
    app.tts_engine = None
    return app


def _spoken(app):
    return app.audio_coordinator.speak.call_args.args[0]


def test_dictation_reads_the_exact_pasted_text_and_shows_chip():
    text = "Post smart-correction line one.\nLine two with formatting."
    app = _app(
        [{"entry_type": "dictation", "status": "success", "display_text": "trimmed"}],
        live=text,
    )

    assert core_utils.read_that_back(app) is True

    assert _spoken(app) == text
    app.audio_coordinator.speak.assert_called_once_with(
        text, category="dictation_readback", interruptible=True,
    )
    app._show_outcome_chip.assert_called_once_with("Reading back", "accent")


def test_a_command_is_described_instead_of_read_as_dictation():
    app = _app([{
        "entry_type": "command",
        "status": "success",
        "display_text": "show numbers",
        "matched_command": "show numbers",
    }], live="older dictation")

    core_utils.read_that_back(app)

    assert _spoken(app) == "The last thing was a command: show numbers"


def test_nothing_has_the_explicit_empty_sentence():
    app = _app()

    core_utils.read_that_back(app)

    assert _spoken(app) == "Nothing dictated yet."


def test_long_dictation_is_not_truncated_by_command_mode_limit():
    text = "x" * 400
    app = _app(
        [{"entry_type": "dictation", "status": "success", "display_text": text}],
        live=text,
    )
    app.audio_coordinator.speak = MagicMock()

    core_utils.read_that_back(app)

    assert _spoken(app) == text
    assert len(_spoken(app)) == 400


def test_command_is_in_the_live_accessibility_catalog():
    # build_catalog consumes the production matcher and loads the plugin; the
    # registry entry check below proves this command is registered without
    # importing the app.
    plugin_commands._reinstall_module_commands(core_utils)
    specs = build_catalog()
    entry = plugin_commands._REGISTRY["read that back"]
    assert entry["pack"] == "accessibility"
    assert entry["ai_composable"] is False
    for alias in ("read it back", "what did you type", "read my last dictation"):
        assert plugin_commands._REGISTRY[alias] is entry
    assert any(spec.canonical_id == "core_utils.read_that_back" for spec in specs)


def test_coordinator_readback_is_exempt_captioned_and_interruptible():
    app = MagicMock()
    app.command_mode_active = True
    app.config = {
        "command_mode": {"tts_char_limit": 50},
        "tts": {"speed": 1.0, "volume": 0.8},
    }
    engine = MagicMock()
    engine.speak.return_value = SpeechHandle("readback")
    coordinator = AudioCoordinator(app, engine, config={})
    captions = MagicMock()
    coordinator._ava_accessibility = lambda: (True, False)
    coordinator._emit_caption = captions

    text = "y" * 400
    coordinator.speak(text, category="dictation_readback", interruptible=True)

    assert "dictation_readback" in AVA_SPEECH_CATEGORIES
    assert coordinator._active_interruptible is True
    captions.assert_called_once_with(text, hold=True)
    assert engine.speak.call_args.args[0] == text
    assert engine.speak.call_args.kwargs["category"] == "dictation_readback"
