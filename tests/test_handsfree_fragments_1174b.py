"""1174b evidence-only gate: synthetic DSP/decoder probes, no distance claim.

Generated speech + real VAD/decoder characterization lives in the worker's
external report. These portable tests verify production plumbing/behavior
with deterministic evidence. No models, owner audio, devices or subprocesses.
"""
from dataclasses import asdict, replace
from types import MethodType, SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

import dictation  # The maintained gate adapter supplies safe source definitions.
from samsara import speech_presence as presence
from samsara.handsfree_fragment_policy import CaptureEvidence, FragmentEvidence
from samsara.session_modes import SessionMode, UtteranceSignals
from tests.test_dictate_streaming_preview import _bare_preview
from tests.test_session_modes import manager_factory


GOOD = UtteranceSignals(True, (1.2,), True)


def segment(text='Blue.', *, logprob=-.2, no_speech=.1, temperature=0., compression=1.2):
    return NS(text=text, avg_logprob=logprob, no_speech_prob=no_speech,
              temperature=temperature, compression_ratio=compression)


def owner(manager=None):
    return NS(model_rate=16000, config={}, _session_mode_manager=manager,
              _ava_cmd_generation=3, _ace_engine=NS(_ring=NS(device_epoch=7)))


@pytest.fixture
def detector(monkeypatch):
    detector = presence.SpeechPresence()
    detector._model = Mock(side_effect=lambda a: np.full(len(a)//512, .8, np.float32))
    monkeypatch.setattr(presence, '_presence', detector)
    return detector


def collect(app, audio, segments=(), **kwargs):
    take = CaptureEvidence(app)
    skipped = presence.skip_decode(audio, app, path='handsfree', evidence_sink=take, **kwargs)
    return skipped, take.attach(GOOD, segments, app).fragment_evidence


def test_capture_retains_same_vad_statistics_and_original_segment_quality(detector, manager_factory):
    manager, _ = manager_factory(buffer_dictate_until_commit=True)
    app = owner(manager)
    audio = np.full(16000, .02, np.float32)
    segs = [segment(temperature=.6), NS(avg_logprob=-.7)]
    rejected, evidence = collect(app, audio, segs)
    assert not rejected and evidence.fresh is True
    assert evidence.speech.duration_s == 1
    assert evidence.speech.speech_duty == 1
    assert evidence.speech.totals_and_runs_s == ((1., 1.), (1., 1.), (1., 1.))
    assert evidence.speech.active_rms == pytest.approx(.02)
    assert evidence.segments[0].temperature == .6
    assert evidence.segments[1].avg_logprob == -.7
    assert evidence.segments[1].no_speech_prob is None
    assert detector._model.call_count == 1
    assert UtteranceSignals(False).fragment_evidence is None


@pytest.mark.parametrize('rate', [8000, 16000, 48000])
def test_active_speech_energy_uses_only_existing_speech_windows(detector, rate):
    app = owner(NS(_session_epoch=1))
    app.model_rate = rate
    audio = np.full(rate, .01, np.float32)
    audio[:rate//2] = .5  # Non-speech energy must not become speech energy.
    detector._model.side_effect = lambda a: np.r_[np.zeros(16), np.full(len(a)//512-16, .8)]
    _, evidence = collect(app, audio)
    assert evidence.speech.active_rms == pytest.approx(.01, abs=.00001)
    assert 0 < evidence.speech.speech_duty < 1


@pytest.mark.parametrize('case', ['off', 'unknown', 'short', 'missing_scores', 'invalid_scores'])
def test_unknown_disabled_and_unmeasured_evidence_never_fabricates_support(detector, case):
    app = owner(NS(_session_epoch=1))
    audio = np.zeros(16000, np.float32)
    if case == 'off':
        app.config = {'dictation': {'speech_presence_gate': False}}
    elif case == 'unknown':
        detector._model = None
        detector._load_model = Mock(side_effect=RuntimeError())
    elif case == 'short':
        audio = audio[:100]
    elif case == 'missing_scores':
        detector.measure = lambda *_: presence.Presence(True, .1, .1, .8)
    else:
        detector._model.side_effect = lambda a: np.full(len(a)//512, np.nan)
    rejected, evidence = collect(app, audio)
    assert not rejected
    assert evidence.speech.active_rms is None
    if case != 'missing_scores':
        assert evidence.speech.known is False
        assert evidence.speech.speech_duty is None


@pytest.mark.parametrize('change', ['session', 'manager', 'mic', 'execution'])
def test_generations_cannot_reuse_an_earlier_takes_evidence(detector, change):
    manager = NS(_session_epoch=2)
    app = owner(manager)
    take = CaptureEvidence(app)
    presence.skip_decode(np.ones(16000), app, path='handsfree', evidence_sink=take)
    if change == 'session': manager._session_epoch += 1
    elif change == 'manager': app._session_mode_manager = NS(_session_epoch=2)
    elif change == 'mic': app._ace_engine._ring.device_epoch += 1
    else: app._ava_cmd_generation += 1
    stale = take.attach(GOOD, [segment()], app).fragment_evidence
    assert stale.fresh is False
    _, following = collect(app, np.ones(16000), [segment()])
    assert following.fresh is True
    assert following.capture_token is not stale.capture_token


def test_cold_missing_binding_and_reused_collector_stay_unknown(detector):
    app = NS(model_rate=16000, config={})
    take = CaptureEvidence(app)
    for _ in range(2):
        presence.skip_decode(np.ones(16000), app, path='handsfree', evidence_sink=take)
    evidence = take.attach(GOOD, [NS(avg_logprob=float('nan'))], app).fragment_evidence
    assert evidence.fresh is None and evidence.speech.known is False
    assert evidence.segments[0].avg_logprob is None


@pytest.mark.parametrize('speech', [True, False])
def test_an_observer_failure_does_not_change_the_existing_verdict(detector, speech):
    detector._model.side_effect = lambda a: np.full(len(a)//512, .8 if speech else 0.)
    sink = Mock(side_effect=RuntimeError())
    assert presence.skip_decode(np.ones(16000), owner(), path='handsfree', evidence_sink=sink) is not speech
    sink.assert_called_once()


def test_evidence_holds_no_pcm_or_transcript(detector):
    _, evidence = collect(owner(NS(_session_epoch=1)), np.ones(16000), [segment()])
    def visit(value):
        assert not isinstance(value, np.ndarray)
        assert value != 'Blue.' if isinstance(value, str) else True
        if isinstance(value, dict):
            for child in value.values(): visit(child)
        elif isinstance(value, (tuple, list)):
            for child in value: visit(child)
    visit(asdict(evidence))


@pytest.mark.parametrize('weak', [.1001, .3, .5001])
@pytest.mark.parametrize('logprob,temperature', [(-.2, 0), (-.7, .6), (None, None)])
def test_identical_intended_and_background_evidence_has_no_safe_veto(detector, weak, logprob, temperature):
    detector._model.side_effect = lambda a: np.r_[np.full(2, weak), np.zeros(len(a)//512-2)]
    app = owner(NS(_session_epoch=1))
    audio = np.full(16000, .001, np.float32)
    segs = [segment(logprob=logprob, temperature=temperature)]
    # Same sampled signal can be a deliberate quiet word or background playback.
    deliberate = collect(app, audio, segs)[1]
    background = collect(app, audio, segs)[1]
    assert deliberate.speech == background.speech
    assert deliberate.segments == background.segments
    assert deliberate.capture_token is not background.capture_token


def production_app(monkeypatch, manager, full):
    from tests.conftest import apply_fake_app_defaults
    from samsara import button_voice, failure_reasons
    app = owner(manager)
    apply_fake_app_defaults(app)
    app.config = {'ui': {'live_surface': {'enabled': full}}}
    app.audio_coordinator = None
    app._command_mode_ghost_tap = False
    app._ensure_session_mode_manager = lambda: manager
    app._vad_available = True
    app._vad_model = object()
    app._buffer_has_contiguous_speech = Mock(return_value=True)
    app._compute_switch_gate_signals = MethodType(dictation.DictationApp._compute_switch_gate_signals, app)
    app._decode_session_audio = Mock(return_value=([segment()], NS()))
    app.get_transcription_params = lambda **_: {'log_prob_threshold': -1., 'no_speech_threshold': .6}
    app._filter_dictation_language = lambda text, info: text
    app._is_dictate_context_echo = lambda *_: False
    app._handle_session_dispatch_outcome = Mock()
    app._resolve_command_miss = lambda outcome, text: outcome
    app._show_outcome_chip = Mock()
    app._log_cmd_utt_dropped = Mock()
    app.play_sound = Mock()
    app._vad_reset = Mock()
    app._intent_shadow_observe = Mock()
    app._dictate_preview = _bare_preview(app)
    app._dictate_preview._overlay.set_paused = Mock()
    app.live_projection = {'text': manager.dictate_pending_buffer, 'syncs': 0}
    def sync_draft(current):
        app.live_projection.update(text=current.dictate_pending_buffer,
                                   syncs=app.live_projection['syncs'] + 1)
    app.live_surface = NS(sync_draft=Mock(side_effect=sync_draft))
    monkeypatch.setattr(dictation, '_cancel_window_module', lambda: None)
    monkeypatch.setattr(dictation, 'get_dictionary_store', lambda _: NS(apply_corrections=lambda text: text))
    monkeypatch.setattr(dictation.diagnostics, 'record', lambda *a, **k: None)
    monkeypatch.setattr(button_voice, 'consume_before_pending', lambda *a, **k: False)
    monkeypatch.setattr(failure_reasons, 'post_clipping', lambda *a, **k: None)
    return app


def run(app, audio=None):
    audio = np.full(16000, .02, np.float32) if audio is None else audio
    dictation.DictationApp._handle_command_mode_utterance(app, [audio], 16000)


@pytest.mark.parametrize('full', [False, True], ids=['Compact', 'Full'])
@pytest.mark.parametrize('text', ['Blue.', 'Inez.', 'Seven.', 'Blue. Seven. Inez.', 'literal end'])
@pytest.mark.parametrize('logprob,temperature', [(-.2, 0.), (-.7, .6)])
def test_production_capture_signal_dispatch_and_projection_preserve_short_prose(
        detector, manager_factory, monkeypatch, full, text, logprob, temperature):
    manager, _ = manager_factory(buffer_dictate_until_commit=True)
    manager.reset(SessionMode.DICTATE)
    app = production_app(monkeypatch, manager, full)
    app._decode_session_audio.return_value = ([segment(text, logprob=logprob, temperature=temperature)], NS())
    dispatch = Mock(wraps=manager.dispatch_utterance)
    manager.dispatch_utterance = dispatch
    run(app)
    assert manager.dictate_pending_buffer
    assert text.removeprefix('literal ').rstrip('.').lower() in manager.dictate_pending_buffer.lower()
    assert app._dictate_preview._finalized
    assert app._dictate_preview._finalized == [manager.dictate_pending_buffer]
    assert app.live_projection['text'] == manager.dictate_pending_buffer
    assert detector._model.call_count == 1
    signals = dispatch.call_args.args[1]
    assert isinstance(signals.fragment_evidence, FragmentEvidence)
    assert signals.fragment_evidence.fresh is True
    assert signals.fragment_evidence.segments[0].temperature == temperature
    assert app._show_outcome_chip.call_count == 0


@pytest.mark.parametrize('full', [False, True], ids=['Compact', 'Full'])
def test_existing_no_speech_reject_preserves_draft_audio_context_undo_and_next_word(
        detector, manager_factory, monkeypatch, full):
    manager, _ = manager_factory(buffer_dictate_until_commit=True)
    manager.reset(SessionMode.DICTATE)
    manager.dispatch_utterance('An earlier draft.', GOOD)
    app = production_app(monkeypatch, manager, full)
    app._dictate_preview._finalized = [manager.dictate_pending_buffer]
    before = (manager.dictate_pending_buffer, manager.dictate_context_tail(), len(manager._stack),
              tuple(manager._dictate_pending_audio), list(app._dictate_preview._finalized),
              dict(app.live_projection))
    detector._model.side_effect = lambda a: np.zeros(len(a)//512)
    run(app)
    after = (manager.dictate_pending_buffer, manager.dictate_context_tail(), len(manager._stack),
             tuple(manager._dictate_pending_audio), list(app._dictate_preview._finalized),
             dict(app.live_projection))
    assert after == before
    app._decode_session_audio.assert_not_called()
    app._show_outcome_chip.assert_not_called()
    detector._model.side_effect = lambda a: np.full(len(a)//512, .8)
    run(app)
    assert 'Blue' in manager.dictate_pending_buffer
    assert len(manager._dictate_pending_audio) == len(before[3]) + 1
    assert manager._dictate_pending_audio[-1] is not None
    assert app.live_projection['text'] == manager.dictate_pending_buffer


@pytest.mark.parametrize('full', [False, True], ids=['Compact', 'Full'])
def test_existing_exhausted_quality_warning_is_preserved(detector, manager_factory, monkeypatch, full):
    manager, _ = manager_factory(buffer_dictate_until_commit=True)
    manager.reset(SessionMode.DICTATE)
    app = production_app(monkeypatch, manager, full)
    app._decode_session_audio.return_value = ([segment(logprob=-1.3)], NS())
    run(app)
    assert manager.dictate_pending_buffer == ''
    assert app._dictate_preview._finalized == []
    app._show_outcome_chip.assert_called_once_with("Didn't catch that - say it again", 'warning')


@pytest.mark.parametrize('case', ['unknown_vad', 'missing_quality', 'mic_drift', 'decode_error'])
def test_production_unknown_drift_and_error_keep_existing_behavior(detector, manager_factory, monkeypatch, case):
    manager, _ = manager_factory(buffer_dictate_until_commit=True)
    manager.reset(SessionMode.DICTATE)
    app = production_app(monkeypatch, manager, True)
    if case == 'unknown_vad':
        monkeypatch.setattr(detector, 'measure', lambda *_: presence.Presence(False))
    if case == 'missing_quality':
        app._decode_session_audio.return_value = ([NS(text='Blue.', compression_ratio=1.2)], NS())
    if case == 'mic_drift':
        def decoded(*args, **kwargs):
            app._ace_engine._ring.device_epoch += 1
            return [segment()], NS()
        app._decode_session_audio.side_effect = decoded
    if case == 'decode_error':
        app._decode_session_audio.side_effect = RuntimeError('synthetic decoder failure')
    dispatch = Mock(wraps=manager.dispatch_utterance)
    manager.dispatch_utterance = dispatch
    run(app)
    if case == 'decode_error':
        dispatch.assert_not_called()
        assert manager.dictate_pending_buffer == ''
        app.play_sound.assert_called_once_with('error')
    else:
        assert manager.dictate_pending_buffer == 'Blue.'
        evidence = dispatch.call_args.args[1].fragment_evidence
        if case == 'unknown_vad': assert not evidence.speech.known
        if case == 'missing_quality': assert evidence.segments[0].avg_logprob is None
        if case == 'mic_drift': assert evidence.fresh is False


@pytest.mark.parametrize('phrase,kind', [('halt','stopped'), ('end','dictate_committed'), ('cancel','abort')])
def test_controls_keep_their_existing_routing_with_evidence(detector, manager_factory, phrase, kind):
    manager, _ = manager_factory(buffer_dictate_until_commit=True)
    manager._stop_fn = Mock()
    manager.reset(SessionMode.DICTATE)
    manager.dispatch_utterance('An earlier draft.', GOOD)
    app = owner(manager)
    _, evidence = collect(app, np.ones(16000), [segment()])
    signals = replace(GOOD, fragment_evidence=evidence)
    assert manager.dispatch_utterance(phrase, signals).kind == kind


def test_configured_stop_and_short_confident_end_keep_their_existing_gates(detector, manager_factory):
    manager, mocks = manager_factory(buffer_dictate_until_commit=True)
    manager._stop_fn = Mock()
    manager._stop_phrases = frozenset({'stop'})
    manager.reset(SessionMode.DICTATE)
    manager.dispatch_utterance('An earlier draft.', GOOD)
    _, evidence = collect(owner(manager), np.ones(16000), [segment()])
    signals = UtteranceSignals(False, (1.2,), True, fragment_evidence=evidence)
    assert manager.dispatch_utterance('stop', signals).kind == 'stopped'
    assert manager.dispatch_utterance('end', signals).kind == 'dictate_committed'
    mocks['inject'].assert_called_once()


def test_short_pending_answer_routes_before_prose(detector, manager_factory):
    pending = Mock(side_effect=lambda text: 'approved' if text == 'yes' else None)
    manager, _ = manager_factory(buffer_dictate_until_commit=True, pending_reply_fn=pending)
    manager.reset(SessionMode.DICTATE)
    app = owner(manager)
    _, evidence = collect(app, np.ones(16000), [segment('yes')])
    outcome = manager.dispatch_utterance('yes', replace(GOOD, fragment_evidence=evidence))
    assert outcome.kind == 'pending_reply'
    assert manager.dictate_pending_buffer == ''
