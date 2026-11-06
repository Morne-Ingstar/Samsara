"""Real display/voice regressions from the release review; synthetic text only."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.test_message_lifetime_1038 import display


@pytest.mark.parametrize('text,words', [
    ('we\u2019re ready', ['we\u2019re', 'ready']),
    ("we're ready", ["we're", 'ready']),
    ('well-known words', ['well-known', 'words']),
])
def test_full_word_targets_keep_contractions_and_offsets(text, words):
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    widget = NS(_segments=lambda: [('synthetic', text)],
        _view=NS(document_id='test', revision=1, provisional_text=''),
        _selection=None, _transcript=Mock())
    LiveSurfaceWidget._render_transcript(widget)
    assert [target['word'] for target in widget._word_targets] == words
    for target in widget._word_targets:
        assert text[target['start']:target['end']] == target['word']


def test_full_provisional_markers_and_game_mode(qapp, monkeypatch):
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.live_surface.model import EventKind, Lane, SurfaceEvent
    from samsara.ui import live_surface_qt, qt_runtime
    monkeypatch.setattr(qt_runtime, 'post', lambda *_: None)
    app = NS(config={'ui': {'live_surface': {'enabled': True}}})
    controller = LiveSurfaceController(app,
        widget_factory=lambda c: live_surface_qt.LiveSurfaceWidget(c, reduced_motion=True))
    try:
        controller.start()
        controller.begin_capture(Lane.HOLD)
        controller.post(SurfaceEvent(EventKind.PARTIAL, text='Synthetic preview.'))
        controller.drain()
        widget = controller.widget
        assert widget._provisional.text() == '\u22ef Synthetic preview.'
        assert widget._transcript.toPlainText() == '\u22ef Synthetic preview.'
        widget.set_game_mode(True)
        assert widget._lane_text(widget._view).endswith(' \u00b7 Game mode')
    finally:
        controller.close()


def test_compact_game_mode_separator(qapp):
    from samsara.ui.listening_indicator import ListeningIndicator
    indicator = ListeningIndicator()
    try:
        indicator.set_game_mode(True)
        assert indicator._resolve_colors()[2].endswith(' \u00b7 Game mode')
        assert '\u00c2' not in indicator._resolve_colors()[2]
    finally:
        indicator.destroy()
        indicator.deleteLater()


def recover(app, source, monkeypatch):
    from samsara import hotkeys, microphone_recovery
    if source == 'mouse_hotkey_warning':
        hotkeys._clear_mouse_hotkey_warning(app)
    else:
        app._no_microphone, app._using_default_mic = True, False
        monkeypatch.setattr(microphone_recovery, '_boot_grace', lambda *_: True)
        microphone_recovery.set_state(app, False)


@pytest.mark.parametrize('source', ['mouse_hotkey_warning', 'windows_warning'])
@pytest.mark.parametrize('ttl', [1800, None])
def test_actual_recovery_clears_protected_warning_immediately(display, source, ttl, monkeypatch):
    from samsara.live_surface.feedback import schedule_feedback
    d = display
    schedule_feedback(d.app, 'Synthetic device warning.', 'warning', ttl, source=source)
    d.flush()
    assert d.text() == 'Synthetic device warning.'
    d.i.clear_outcome()
    d.flush()
    assert d.text() == 'Synthetic device warning.'  # Routine clear stays protected.
    recover(d.app, source, monkeypatch)
    d.flush()
    assert d.text() == ''


@pytest.mark.parametrize('source', ['mouse_hotkey_warning', 'windows_warning'])
def test_recovery_removes_queued_owned_warning_but_keeps_other_problem(display, source, monkeypatch):
    from samsara.live_surface.feedback import schedule_feedback
    d = display
    d.show('Other active problem.', 'error')
    schedule_feedback(d.app, 'Recovered warning.', 'warning', None, source=source)
    d.flush()
    recover(d.app, source, monkeypatch)
    d.flush()
    assert d.text() == 'Other active problem.'
    d.i.dismiss_error()
    d.flush()
    assert d.text() == ''  # The resolved warning is not promoted later.


@pytest.mark.parametrize('source', ['mouse_hotkey_warning', 'windows_warning'])
def test_stale_recovery_cannot_erase_new_warning_from_same_owner(display, source, monkeypatch):
    from samsara.live_surface.feedback import schedule_feedback
    d = display
    schedule_feedback(d.app, 'First warning.', 'warning', 1800, source=source)
    d.flush()
    queued = []
    immediate = d.app._schedule_ui
    d.app._schedule_ui = lambda fn, *args: queued.append(lambda: fn(*args))
    recover(d.app, source, monkeypatch)
    d.app._schedule_ui = immediate
    # A fresh problem arrives while the earlier recovery is still queued.
    schedule_feedback(d.app, 'New warning.', 'warning', None, source=source)
    d.flush()
    d.clock[0] = 20
    d.expire()
    assert d.text() == 'New warning.'
    assert len(queued) == 1
    queued[0]()
    d.flush()
    assert d.text() == 'New warning.'


@pytest.mark.parametrize('phrase,action_name', [('click accept', 'apply'), ('click cancel', 'discard')])
@pytest.mark.parametrize('replaced', [False, True])
def test_preview_spoken_labels_use_real_buttons_and_pending_identity(qapp, monkeypatch, phrase, action_name, replaced):
    from PySide6.QtWidgets import QDialog, QPushButton
    from samsara.ava_edit import draft_review, session
    from samsara.ui import button_voice_qt, qt_runtime
    staged = NS(proposal=NS(original='Synthetic source.', rewritten='Synthetic revision.'))
    pending = [staged]
    monkeypatch.setattr(session, 'pending', lambda: pending[0])
    actions = {name: Mock() for name in ('apply', 'discard')}
    for name, action in actions.items():
        monkeypatch.setattr(session, name, action)
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())
    monkeypatch.setattr(draft_review, 'phrases', lambda app: {})
    app = NS(config={})
    queued = []
    bridge = None
    arbitrary = QDialog()
    try:
        assert not button_voice_qt._supported(arbitrary)
        assert draft_review.show(app)
        qapp.processEvents()
        preview = app._ava_edit_preview
        buttons = [b for b in preview.findChildren(QPushButton) if b.isVisible()]
        assert {b.text() for b in buttons} == {'Accept?', 'Cancel'}
        assert button_voice_qt._supported(preview)
        discovered = button_voice_qt.visible_actions(qapp)
        assert {a.label for a in discovered if callable(a.target) and a.target() in buttons} == {'Accept?', 'Cancel'}
        bridge = button_voice_qt.VisibleActions(qapp, queued.append)
        assert bridge.dispatch(phrase)
        if replaced:
            pending[0] = NS(proposal=staged.proposal)
        queued.pop()()
        if replaced:
            assert all(not action.called for action in actions.values())
        else:
            actions[action_name].assert_called_once_with(app)
            assert not actions['discard' if action_name == 'apply' else 'apply'].called
        assert not preview.isVisible()
    finally:
        if bridge is not None:
            bridge.timer.stop()
            bridge.deleteLater()
        draft_review.close()
        arbitrary.close()
        qapp.processEvents()
