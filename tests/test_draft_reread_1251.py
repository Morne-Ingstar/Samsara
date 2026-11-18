"""One explicit hold decode; every replacement remains a guarded proposal."""
from types import SimpleNamespace, MethodType
import weakref
import gc
import threading

import numpy as np
import pytest

from samsara import draft_reread as reread
from samsara.ava_edit import session, draft_review
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals


@pytest.fixture
def rig(monkeypatch):
    session.clear()
    monkeypatch.setattr(reread, '_busy', threading.Lock())
    notices, calls, jobs = [], [], []
    app = SimpleNamespace(config={}, model_rate=16000,
        _show_outcome_chip=lambda *args: notices.append(args),
        _speak_session_notice=lambda *_: None,
        get_transcription_params=lambda **_: {'language': 'af', 'beam_size': 5, 'initial_prompt': None})
    app.agent = MethodType(lambda self, *_: None, app)
    manager = SessionModeManager(abort_phrases=['cancel dictation'],
        inject_fn=lambda *_: False, foreground_exe_resolver=lambda: 'fake.exe',
        remove_chars_fn=lambda *_: None, command_dispatch_fn=lambda *_: None,
        agent_dispatch_fn=app.agent, buffer_dictate_until_commit=True,
        prepare_dictate_fn=lambda text: text)
    manager.reset(SessionMode.DICTATE)
    app._session_mode_manager = manager
    monkeypatch.setattr(reread, 'speech_pcm', lambda audio, rate: np.array(audio, dtype=np.float32, copy=True))
    from samsara import decode_recovery
    def decode(owner, pcm, params, duration):
        calls.append((owner, pcm.copy(), params, duration))
        return SimpleNamespace(text='Synthetic cleaner sentence.', language_rejected=False)
    monkeypatch.setattr(decode_recovery, 'decode_hold', decode)
    monkeypatch.setattr(reread, 'notice', lambda _, message: notices.append(message))
    monkeypatch.setattr(draft_review, 'show', lambda _: True)
    from samsara.runtime import thread_registry
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **_: jobs.append(fn))
    def stage(text='Synthetic first.', audio=None):
        return manager._stage_dictate_chunk(text, np.ones(1600, dtype=np.float32) if audio is None else audio)
    value = SimpleNamespace(app=app, manager=manager, notices=notices, calls=calls, jobs=jobs, stage=stage)
    yield value
    session.clear()


def test_ordered_pcm_is_one_normal_hold_decode_and_a_proposal(rig):
    rig.stage(audio=np.full(1600, .1, dtype=np.float32))
    rig.stage('Second.', np.full(800, .2, dtype=np.float32))
    before = rig.manager.draft_document.text
    assert reread.request(rig.app)
    assert rig.calls == [] and rig.manager.draft_document.text == before
    rig.jobs.pop()()
    assert len(rig.calls) == 1
    owner, pcm, params, duration = rig.calls[0]
    assert owner is rig.app and duration == .15
    np.testing.assert_array_equal(pcm, np.r_[np.full(1600, .1, np.float32), np.full(800, .2, np.float32)])
    assert params['language'] == 'af' and params['beam_size'] == 5
    assert session.pending().proposal.rewritten == 'Synthetic cleaner sentence.'
    assert rig.manager.draft_document.text == before


def test_accept_and_existing_scratch_undo(rig):
    rig.stage()
    before = rig.manager.draft_document.text
    reread.request(rig.app); rig.jobs.pop()()
    assert session.apply(rig.app)['applied']
    assert rig.manager.draft_document.text == 'Synthetic cleaner sentence.'
    assert rig.manager._do_scratch_that_n(1)['scratched'] == 1
    assert rig.manager.draft_document.text == before


def test_cancel_keeps_original(rig):
    rig.stage(); before = rig.manager.draft_document.text
    reread.request(rig.app); rig.jobs.pop()()
    assert session.discard(rig.app)
    assert rig.manager.draft_document.text == before


@pytest.mark.parametrize('edit', ['manual', 'voice'])
def test_edited_draft_is_never_decoded(rig, edit):
    rig.stage()
    if edit == 'voice':
        from samsara.draft_commands import dispatch_draft_command
        dispatch_draft_command(rig.manager, 'uppercase that', 'uppercase that')
    else:
        rig.manager.draft_document.replace_text('Synthetic manual edit.', edited=True)
    before = rig.manager.draft_document.text
    assert not reread.request(rig.app)
    assert rig.jobs == [] and rig.manager.draft_document.text == before


@pytest.mark.parametrize('when', ['during_decode', 'after_proposal'])
def test_stale_result_never_overwrites_newer_draft(rig, when):
    rig.stage(); reread.request(rig.app)
    if when == 'after_proposal':
        rig.jobs.pop()()
    rig.stage('Synthetic added sentence.')
    before = rig.manager.draft_document.text
    if when == 'during_decode':
        rig.jobs.pop()()
        assert session.pending() is None
    else:
        assert not session.apply(rig.app)['applied']
    assert rig.manager.draft_document.text == before


@pytest.mark.parametrize('kind', ['none', 'lost_chunk', 'before_build', 'restored'])
def test_incomplete_audio_declines_instead_of_partial_rewrite(rig, kind):
    rig.stage()
    if kind == 'none':
        rig.manager._dictate_pending_audio = []
    elif kind == 'lost_chunk':
        rig.manager._dictate_pending_audio.append(None)
    elif kind == 'before_build':
        rig.manager._reread_pcm.clear()
        rig.stage('Synthetic next.')
    else:
        rig.manager._dictate_pending_audio = [np.ones(1600, dtype=np.float32)]
    assert not reread.request(rig.app) and rig.jobs == []
    assert 'complete audio' in rig.notices[-1]


def test_cap_drops_oldest_with_notice_and_preserves_text(rig, monkeypatch):
    monkeypatch.setattr(reread, 'MAX_AUDIO_BYTES', 12800)
    rig.stage(); rig.stage('Synthetic next.')
    assert rig.manager._dictate_pending_audio[0] is None
    assert sum(ref.nbytes for ref in rig.manager._dictate_pending_audio if ref is not None) == 6400
    assert sum(entry[1].nbytes for entry in rig.manager._reread_pcm.values()) == 6400
    assert any('Older draft audio' in str(note) for note in rig.notices)
    assert not reread.request(rig.app)
    assert 'Synthetic next.' in rig.manager.draft_document.text


def test_reset_releases_pcm_without_helper_holding_old_list(rig):
    rig.stage()
    ref = weakref.ref(rig.manager._dictate_pending_audio[0])
    rig.manager.reset(SessionMode.DICTATE)
    gc.collect()
    assert ref() is None
    assert not rig.manager._reread_pcm


@pytest.mark.parametrize('phrase', ['re-read it', 're-read', 'reread it', 'reread', 're read it'])
def test_voice_action_uses_existing_audio_gate(rig, phrase):
    rig.stage()
    good = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
    outcome = rig.manager.dispatch_utterance(phrase, good)
    assert outcome.detail['phrase'] == 're-read' and len(rig.jobs) == 1


def test_embedded_phrase_stays_prose(rig):
    rig.stage()
    rig.manager.dispatch_utterance('We should re-read it tomorrow.', UtteranceSignals(has_contiguous_speech=True))
    assert not rig.jobs and 'tomorrow' in rig.manager.draft_document.text


def test_duplicate_request_does_not_queue_more_decodes(rig):
    rig.stage(); assert reread.request(rig.app)
    assert not reread.request(rig.app)
    rig.jobs.pop()()


def test_existing_automatic_commit_audio_is_unchanged(rig):
    original = np.linspace(-.2, .2, 1600, dtype=np.float32)
    rig.stage(audio=original)
    assert rig.manager._dictate_pending_audio[0] is original


def test_decode_exception_releases_admission_and_preserves_text(rig, monkeypatch):
    from samsara import decode_recovery
    rig.stage(); before = rig.manager.draft_document.text
    def fail(*_):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(decode_recovery, 'decode_hold', fail)
    assert reread.request(rig.app); rig.jobs.pop()()
    assert rig.manager.draft_document.text == before and session.pending() is None
    assert reread.request(rig.app); rig.jobs.pop()()


def test_empty_prepared_text_is_never_a_delete_proposal(rig):
    rig.stage(); before = rig.manager.draft_document.text
    rig.manager._prepare_dictate_fn = lambda _: ''
    reread.request(rig.app); rig.jobs.pop()()
    assert session.pending() is None and rig.manager.draft_document.text == before


def test_voice_decline_does_not_replace_its_reason_with_generic_chip(rig):
    from samsara.session_modes import outcome_chip
    rig.stage()
    rig.manager.draft_document.replace_text('Synthetic edited.', edited=True)
    outcome = rig.manager.dispatch_utterance('re-read it', UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,)))
    assert outcome.detail['requested'] is False and outcome_chip(outcome.kind, outcome.detail) is None


def test_failed_audio_gate_keeps_voice_phrase_literal(rig):
    rig.stage()
    rig.manager.dispatch_utterance('re-read it', UtteranceSignals(has_contiguous_speech=False))
    assert not rig.jobs


def test_real_bundled_vad_accepts_silence_without_disk(monkeypatch):
    # Exercise the installed API as well as the deterministic span test below.
    assert reread.speech_pcm(np.zeros(16000, dtype=np.float32), 16000) is None


def test_speech_extraction_uses_vad_spans_without_pause_samples(monkeypatch):
    from faster_whisper import vad
    monkeypatch.setattr(vad, 'get_speech_timestamps', lambda *_, **__: [
        {'start': 2, 'end': 5}, {'start': 8, 'end': 10}])
    np.testing.assert_array_equal(reread.speech_pcm(np.arange(12, dtype=np.float32), 16000), [2, 3, 4, 8, 9])


@pytest.mark.parametrize('display', ['Compact', 'Full'])
def test_buttons_fit_action_row_and_request_same_action(rig, monkeypatch, qapp, display):
    from samsara.ui import theme
    theme.set_theme('dark')
    clicked = []
    monkeypatch.setattr(reread, 'request', lambda app: clicked.append(app))
    rig.stage()
    if display == 'Full':
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        from samsara.ui.live_surface_qt import LiveSurfaceWidget
        controller = LiveSurfaceController(rig.app, widget_factory=lambda c: LiveSurfaceWidget(c, reduced_motion=True))
        rig.app.live_surface = controller
        LiveSurfaceIndicatorAdapter(controller).set_session_mode('DICTATE')
        controller.start(); controller.sync_draft(rig.manager); controller.drain()
        widget = controller.widget
        button, neighbor = widget._reread, widget._clean_up
    else:
        from samsara.streaming import DictatePreviewSession, StreamingOverlayQt, IdleSettings
        preview = DictatePreviewSession.__new__(DictatePreviewSession)
        preview.app = rig.app
        overlay = StreamingOverlayQt(idle=IdleSettings())
        overlay.set_cleanup_callback(preview._on_clean_up_clicked)
        compact = overlay._new_widget()
        compact.enable_interaction(lambda *_: None, lambda: None)
        compact.show_overlay()
        widget = compact._w
        widget._interactive = True
        widget._on_update(rig.manager.draft_document.text, StreamingOverlayQt.STATE_LISTENING, 'plain')
        button, neighbor = widget._reread_btn, widget._clean_up_btn
    qapp.processEvents()
    assert button.text() == 'Re-read' and button.isVisible()
    assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 12
    assert widget.rect().contains(button.mapTo(widget, button.rect().topLeft()))
    assert widget.rect().contains(button.mapTo(widget, button.rect().bottomRight()))
    assert not button.geometry().intersects(neighbor.geometry())
    button.click(); assert clicked == [rig.app]
    widget.close()
