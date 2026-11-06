"""Staged cleanup stays reversible in both displays, with optional voice replies."""
from types import SimpleNamespace

import pytest

from samsara.ava_edit import intent, session
from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.session_modes import SessionMode, SessionModeManager
from samsara.streaming import DictatePreviewSession, _StreamingWidget, IdleSettings, StreamingOverlayQt
from samsara.ui.live_surface_qt import LiveSurfaceWidget, cleanup_buttons, sync_cleanup_buttons

SOURCE = "I went to the. Store and. Bought some milk."
RESULT = "I went to the store and bought some milk."


@pytest.fixture
def app(monkeypatch):
    session.clear()
    monkeypatch.setattr(session.thread_registry, "spawn", lambda _name, fn, **_: fn())
    value = SimpleNamespace(config={"ava_edit": {"enabled": False}},
        _show_outcome_chip=lambda *_: None, _speak_session_notice=lambda *_: None)
    value._agent = lambda *_: None
    manager = SessionModeManager(abort_phrases=["cancel"], inject_fn=lambda *_: False,
        foreground_exe_resolver=lambda: "fake.exe", remove_chars_fn=lambda *_: None,
        command_dispatch_fn=lambda *_: None, agent_dispatch_fn=value._agent,
        buffer_dictate_until_commit=True, live_surface_enabled=True)
    manager.reset(SessionMode.DICTATE)
    manager._dictate_pending_buffer = SOURCE
    value._session_mode_manager = manager
    yield value
    session.clear()


@pytest.mark.parametrize("phrase", ["clean up", "clean this up", "clean up the draft",
    "clean up everything", "clean up the whole draft", "clean up the last sentence"])
def test_spoken_scopes_are_whole_requests(app, phrase):
    source = "First sentence. I went to The store and bought some milk." if "last sentence" in phrase else SOURCE
    app._session_mode_manager._dictate_pending_buffer = source
    assert intent.draft_cleanup_scope(phrase) is not None
    assert session.handle_utterance(app, phrase)
    assert session.pending().draft_target[0] is app._session_mode_manager
    assert app._session_mode_manager.dictate_pending_buffer == source
    if "last sentence" in phrase:
        assert session.pending().proposal.original == "I went to The store and bought some milk."
        assert session.pending().proposal.rewritten == "I went to the store and bought some milk."
    else:
        assert session.pending().proposal.rewritten == RESULT


def test_embedded_cleanup_does_not_fire(app):
    assert intent.draft_cleanup_scope("We should clean up the draft tomorrow") is None
    assert not session.handle_utterance(app, "We should clean up the draft tomorrow")
    assert session.pending() is None


@pytest.mark.parametrize("palette", ["dark", "light"])
@pytest.mark.parametrize("display", ["Full", "Compact"])
def test_click_accept_cancel_and_empty(app, qapp, display, palette):
    from samsara.ui import theme
    original_theme = theme.active_theme()
    theme.set_theme(palette)

    manager = app._session_mode_manager
    controller = LiveSurfaceController(app, widget_factory=lambda c: LiveSurfaceWidget(c, reduced_motion=True))
    app.live_surface = controller
    LiveSurfaceIndicatorAdapter(controller).set_session_mode("DICTATE")
    controller.start(); controller.sync_draft(manager); controller.drain()
    if display == "Full":
        widget = controller.widget
        clean, clear = widget._clean_up, widget._clear
    else:
        compact = _StreamingWidget(False, IdleSettings())
        preview = DictatePreviewSession.__new__(DictatePreviewSession)
        preview.app = app; preview._closed = False
        preview._overlay = SimpleNamespace(_widget=compact)
        preview._resync_from_draft = lambda *_: None
        preview._render = lambda *_: None
        app._dictate_preview = preview
        compact.enable_interaction(lambda *_: None, preview._on_clear_clicked)
        compact.enable_cleanup(preview._on_clean_up_clicked)
        compact.show_overlay()
        widget = compact._w; widget._interactive = True
        widget._on_update(SOURCE, StreamingOverlayQt.STATE_LISTENING, "plain")
        clean, clear = widget._clean_up_btn, widget._clear_btn
    qapp.processEvents()
    assert widget.width() == 500  # Smallest supported draft surface.
    assert_button_labels(widget, clean, clear)
    before = clean.geometry(), clear.geometry()
    clean.click(); sync_cleanup_buttons(app); qapp.processEvents()
    assert session.pending().proposal.rewritten == RESULT
    assert (clean.text(), clear.text()) == ("Accept?", "Cancel")
    assert_button_labels(widget, clean, clear)
    assert (clean.geometry(), clear.geometry()) == before
    clear.click(); sync_cleanup_buttons(app); qapp.processEvents()
    assert session.pending() is None and manager.dictate_pending_buffer == SOURCE
    assert (clean.text(), clear.text()) == ("Clean up", "Clear draft")
    clean.click(); sync_cleanup_buttons(app); clean.click(); sync_cleanup_buttons(app)
    qapp.processEvents()
    assert session.pending() is None and manager.dictate_pending_buffer == RESULT
    assert (clean.text(), clear.text()) == ("Clean up", "Clear draft")
    manager._dictate_pending_buffer = ""
    if display == "Full":
        controller.sync_draft(manager); controller.drain()
    else:
        widget._on_update("Listening...", StreamingOverlayQt.STATE_PLACEHOLDER, "plain")
    assert not clean.isEnabled()
    if display == "Compact": compact.stop_life(); widget.hide()
    controller.close()
    theme.set_theme(original_theme)


def test_optional_voice_apply_and_discard_use_existing_session(app):
    assert session.handle_utterance(app, "clean up")
    assert session.handle_utterance(app, "discard")
    assert app._session_mode_manager.dictate_pending_buffer == SOURCE
    assert session.handle_utterance(app, "clean up")
    assert session.handle_utterance(app, "apply")
    assert app._session_mode_manager.dictate_pending_buffer == RESULT


def test_changed_draft_is_not_overwritten(app):
    session.handle_utterance(app, "clean up")
    app._session_mode_manager._dictate_pending_buffer = "New words."
    assert not session.apply(app)["applied"]
    assert app._session_mode_manager.dictate_pending_buffer == "New words."


def test_short_fade_and_reduced_motion(qapp):
    from PySide6.QtWidgets import QPushButton
    clean, clear = QPushButton("Clean up"), QPushButton("Clear draft")
    cleanup_buttons(clean, clear, True)
    assert clean._cleanup_fade.duration() == 140
    assert clean._cleanup_fade.endValue() == 1.
    clean._cleanup_fade.stop(); clear._cleanup_fade.stop()
    cleanup_buttons(clean, clear, False, reduced_motion=True)
    assert clean.text() == "Clean up" and clear.text() == "Clear draft"


def assert_button_labels(widget, clean, clear):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton
    assert clean.height() == clear.height()
    for button in widget.findChildren(QPushButton):
        if not button.isVisible():
            continue
        button.ensurePolished()
        metrics = button.fontMetrics()
        padding = button.property("labelPadding") or 18
        required = metrics.horizontalAdvance(button.text()) + padding
        assert button.sizeHint().width() >= required, button.text()
        assert button.width() >= required, button.text()
        option = QStyleOptionButton()
        button.initStyleOption(option)
        contents = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
        assert metrics.elidedText(button.text(), Qt.TextElideMode.ElideRight, contents.width()) == button.text()
        assert widget.rect().contains(button.geometry()) if button.parentWidget() is widget else button.parentWidget().rect().contains(button.geometry())


def test_draft_proposal_status_omits_button_instructions(app, qapp, monkeypatch):
    """1137, owner override 2026-11-05: show how to view, accept and reject.

    Keep this historical node name so gate comparisons retain the existing ID.
    Mention real visible controls on Full; Compact must have voice directions.
    """
    _assert_draft_status(app, qapp, monkeypatch, "Compact")


def test_draft_proposal_status_names_only_visible_controls(app, qapp, monkeypatch):
    _assert_draft_status(app, qapp, monkeypatch, "Full")


def _assert_draft_status(app, qapp, monkeypatch, display):
    from samsara import command_catalog
    from PySide6.QtWidgets import QWidget, QPushButton, QVBoxLayout
    records = [dict(canonical_id=f"selection_edit.{key}", phrase=value)
               for key, value in (("show_the_change", "inspect proposed edit"),
                                  ("apply", "apply the edit"), ("discard", "discard the edit"))]
    monkeypatch.setattr(command_catalog, "guidance_catalog", lambda *_: records)
    full = None
    if display == "Full":
        full = QWidget()
        layout = QVBoxLayout(full)
        full._clean_up, full._clear = QPushButton("Accept?"), QPushButton("Cancel")
        layout.addWidget(full._clean_up); layout.addWidget(full._clear)
        full.show()
        app.live_surface = SimpleNamespace(widget=full)
    messages = []
    app._show_outcome_chip = lambda text, *_: messages.append(text)
    try:
        session.handle_utterance(app, "clean up")
        text = messages[-1]
        assert "your draft" in text and f"{session.pending().proposal.change_count} change" in text
        assert "inspect proposed edit to view" in text
        assert "apply the edit" in text and "discard the edit" in text
        assert "accept" in text.casefold() and "reject" in text.casefold()
        assert app._session_mode_manager.dictate_pending_buffer == SOURCE
        if full:
            assert "click Accept? / Cancel" in text
            assert len(text.splitlines()) == 2
            full.hide()
            session._chip(app, "edit_proposed", {"changes": 1})
            assert "click" not in messages[-1]
        else:
            assert "click" not in text and len(text.splitlines()) == 1
    finally:
        if full:
            full.close()


@pytest.mark.parametrize("answer", ["Cancel", "Accept?", "expire"])
def test_show_change_is_registered_read_only_and_keeps_pending_owner(app, qapp, monkeypatch, answer):
    """1137: the advertised preview opens the proposal without accepting it."""
    from samsara import command_catalog, plugin_commands
    from samsara.ava_edit import draft_review
    from samsara.ui import qt_runtime
    from PySide6.QtWidgets import QTextBrowser, QPushButton
    monkeypatch.setattr(plugin_commands, "_REGISTRY", {})
    monkeypatch.setattr(plugin_commands, "_MODULE_ENTRIES", {})
    draft_review.register_commands("plugins.commands.selection_edit")
    registered = plugin_commands._REGISTRY["show the change"]
    assert registered["risk_class"] == "ui" and not registered["ai_visible"]
    records = [dict(canonical_id=f"selection_edit.{key}", phrase=value)
               for key, value in (("show_the_change", registered["phrase"]),
                                  ("apply", "apply"), ("discard", "discard"))]
    monkeypatch.setattr(command_catalog, "guidance_catalog", lambda *_: records)
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: True)
    monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
    session.handle_utterance(app, "clean up")
    staged, question = session.pending(), app._ava_question
    assert not session.handle_utterance(app, '"show the change"')
    assert not session.handle_utterance(app, "We should show the change tomorrow")
    assert registered["func"](app, "")
    window = app._ava_edit_preview
    assert [body.toPlainText() for body in window.findChildren(QTextBrowser)] == [SOURCE, RESULT]
    assert session.pending() is staged and app._ava_question is question
    assert app._session_mode_manager.dictate_pending_buffer == SOURCE
    # Preview controls and expiry retire exactly the same bound proposal.
    if answer == "expire":
        staged.created_at -= session.PROPOSAL_TTL_S + 1
        assert session.pending() is None
    else:
        next(button for button in window.findChildren(QPushButton) if button.text() == answer).click()
    assert session.pending() is None and not window.isVisible()
    assert app._session_mode_manager.dictate_pending_buffer == (RESULT if answer == "Accept?" else SOURCE)


def test_pending_preview_is_routed_in_hands_free_and_stays_literal_otherwise(app, monkeypatch):
    """1137: only a whole, grounded preview phrase for a waiting proposal fires."""
    from samsara import command_catalog
    from samsara.ava_edit import draft_review
    from samsara.session_modes import UtteranceSignals
    monkeypatch.setattr(command_catalog, "guidance_catalog", lambda *_: [
        dict(canonical_id="selection_edit.show_the_change", phrase="inspect proposed edit")])
    viewed = []
    monkeypatch.setattr(draft_review, "show", lambda owner: viewed.append(session.pending()) or True)
    app._session_mode_manager._agent_dispatch_fn = SimpleNamespace(__self__=app)
    signals = UtteranceSignals(has_contiguous_speech=True, transcript_confident=True, compression_ratios=(1.0,))
    session.handle_utterance(app, "clean up")
    staged = session.pending()
    assert app._session_mode_manager._dispatch_in_mode("inspect proposed edit", signals).kind == "ava_edit_handled"
    assert viewed == [staged] and session.pending() is staged
    session.clear()
    app._session_mode_manager._dispatch_in_mode("inspect proposed edit", signals)
    assert viewed == [staged]
    assert app._session_mode_manager.dictate_pending_buffer.endswith("inspect proposed edit")
