"""Queue 822: read the focused selection or the last delivered dictation."""

from unittest.mock import MagicMock

from plugins.commands import core_utils


class _Store:
    def __init__(self, rows=()):
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


def test_focused_selection_takes_precedence_over_last_dictation(monkeypatch):
    from samsara.ava_edit import selection

    monkeypatch.setattr(selection, "capture", lambda: ("Selected line.", object()))
    app = _app(live="Recently dictated sentence.")

    assert core_utils.read_that_back(app) is True

    assert _spoken(app) == "Selected line."
    app.audio_coordinator.speak.assert_called_once_with(
        "Selected line.", category="dictation_readback", interruptible=True,
    )


def test_no_selection_reads_the_last_delivered_dictation(monkeypatch):
    from samsara.ava_edit import selection

    monkeypatch.setattr(selection, "capture", lambda: (None, None))
    text = "The sentence just delivered."
    app = _app(live=text)

    core_utils.read_that_back(app)

    assert _spoken(app) == text


def test_selection_router_falls_back_to_last_dictation_when_nothing_is_selected(monkeypatch):
    from samsara.ava_edit import selection

    monkeypatch.setattr(selection, "capture", lambda: (None, None))
    app = _app(live="Sentence routed through Ava's selection handler.")

    assert selection.propose_and_stage(app, "read that back") is True

    assert _spoken(app) == "Sentence routed through Ava's selection handler."


def test_history_supplies_the_last_dictation_after_live_delivery_expires(monkeypatch):
    from samsara.ava_edit import selection

    monkeypatch.setattr(selection, "capture", lambda: (None, None))
    app = _app([{"entry_type": "dictation", "status": "success",
                 "display_text": "Earlier session dictation."}])

    core_utils.read_that_back(app)

    assert _spoken(app) == "Earlier session dictation."


def test_no_selection_or_dictation_speaks_the_required_empty_state(monkeypatch):
    from samsara.ava_edit import selection

    monkeypatch.setattr(selection, "capture", lambda: (None, None))
    app = _app()

    core_utils.read_that_back(app)

    assert _spoken(app) == "There's nothing to read back yet."


def test_recent_command_is_not_read_as_dictation(monkeypatch):
    from samsara.ava_edit import selection

    monkeypatch.setattr(selection, "capture", lambda: (None, None))
    app = _app([{"entry_type": "command", "status": "success",
                 "display_text": "show numbers", "matched_command": "show numbers"}])

    core_utils.read_that_back(app)

    assert _spoken(app) == "There's nothing to read back yet."
