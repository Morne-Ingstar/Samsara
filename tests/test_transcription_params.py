"""Regression tests locking in the "Gate and Reset" hallucination-prevention
transcription parameters against silent future edits.

Exercises DictationApp.get_transcription_params() and the extracted
DictationApp._build_hotkey_transcribe_params() helper directly against a
minimal duck-typed `self` -- no audio, no model load, no full Samsara boot.
Matches the existing DictationApp.method(app, ...) pattern used elsewhere in
this test suite (see test_dictation_app.py).

_build_hotkey_transcribe_params() was extracted from the hotkey transcribe()
closure specifically so this suite can call the REAL production code path
(not a re-implementation that could silently drift from it): vad_filter=False
and condition_on_previous_text=False are forced overrides (conversation-
context reset), while initial_prompt is sourced from
voice_training_window.get_initial_prompt() so vocabulary biasing still
applies per hotkey press. Both the normal (<30s) and [LONG] branches of
transcribe() consume the same dict from this one method.

Verified against the installed faster-whisper (1.2.1) WhisperModel.transcribe
signature: it accepts `no_speech_threshold` and `log_prob_threshold` exactly
as spelled here -- not `logprob_threshold` or any other variant.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


def _make_app(performance_mode='balanced', initial_prompt=''):
    """Minimal duck-typed stand-in for DictationApp's `self`.

    _build_hotkey_transcribe_params() calls self.get_transcription_params()
    internally, so that real method must be bound onto the fake app too
    (not just invoked as DictationApp.get_transcription_params(app) directly).
    """
    app = types.SimpleNamespace()
    app.config = {'language': 'en', 'performance_mode': performance_mode}
    app.voice_training_window = types.SimpleNamespace(
        get_initial_prompt=lambda: initial_prompt
    )
    app.get_transcription_params = types.MethodType(
        dictation.DictationApp.get_transcription_params, app)
    return app


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_native_thresholds_present_for_every_mode(mode):
    """no_speech_threshold/log_prob_threshold must survive in every performance mode."""
    app = _make_app(performance_mode=mode)
    params = dictation.DictationApp.get_transcription_params(app)
    assert params['no_speech_threshold'] == 0.6
    assert params['log_prob_threshold'] == -1.0


def test_gate_max_buffer_s_is_8_seconds():
    """Guards against silently regressing the whisper-hold fix (3.0 -> 8.0):
    3-6s near-silent/whisper holds were bypassing the gate and producing
    phantom "Thank you for watching" text before this was raised."""
    assert dictation._GATE_MAX_BUFFER_S == 8.0


def test_hotkey_params_force_clean_slate_overrides_mode_defaults():
    """The hotkey path's reset must win even when mode defaults would
    otherwise set condition_on_previous_text=True (accurate mode). The
    voice training prompt (vocabulary biasing) is NOT reset -- only
    conversation context (condition_on_previous_text) is."""
    app = _make_app(performance_mode='accurate', initial_prompt='some trained prompt')
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert params['condition_on_previous_text'] is False
    assert params['initial_prompt'] == 'some trained prompt'
    assert params['vad_filter'] is False


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_hotkey_params_force_clean_slate_all_modes(mode):
    """Same clean-slate guarantee regardless of performance mode. The normal
    (<30s) and [LONG] branches of transcribe() both consume the SAME dict
    returned by _build_hotkey_transcribe_params() -- one assertion here
    covers both branches, since there is only one construction site."""
    app = _make_app(performance_mode=mode, initial_prompt='some trained prompt')
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert params['condition_on_previous_text'] is False
    assert params['initial_prompt'] == 'some trained prompt'
