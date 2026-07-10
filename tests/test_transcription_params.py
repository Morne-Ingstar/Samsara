"""Regression tests locking in the "Gate and Reset" hallucination-prevention
transcription parameters against silent future edits.

Exercises DictationApp.get_transcription_params() and the extracted
DictationApp._build_hotkey_transcribe_params() helper directly against a
minimal duck-typed `self` -- no audio, no model load, no full Samsara boot.
Matches the existing DictationApp.method(app, ...) pattern used elsewhere in
this test suite (see test_dictation_app.py).

_build_hotkey_transcribe_params() was extracted from the hotkey transcribe()
closure specifically so this suite can call the REAL production code path
(not a re-implementation that could silently drift from it):
condition_on_previous_text=False is a forced override (conversation-context
reset), while initial_prompt is sourced from voice_training_window.
get_initial_prompt() so vocabulary biasing still applies per hotkey press.
Both the normal (<30s) and [LONG] branches of transcribe() consume the same
dict from this one method.

vad_filter is DELIBERATELY left at the mode default (True) as of 2026-07-10
-- previously force-disabled here, reversed by an A/B decode-parameter
experiment (tools/transcribe_ab.py) that traced a "you know" -> "i know" /
hallucinated-garbage word-loss defect to faster-whisper decoding the
~1.5-1.8s prebuffer+start-earcon noise region adjacent to the user's soft
leading words when its own VAD was disabled. See _build_hotkey_transcribe_
params()'s docstring in dictation.py for the full experiment writeup. This
is an intentional, evidence-based lock reversal, not a silent edit -- if you
are changing vad_filter back, re-run the A/B experiment against real
"you know"-shaped audio first.

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
    conversation context (condition_on_previous_text) is. vad_filter is
    NOT forced -- it now passes through the mode default (see module
    docstring: reversed 2026-07-10, A/B-experiment-driven)."""
    app = _make_app(performance_mode='accurate', initial_prompt='some trained prompt')
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert params['condition_on_previous_text'] is False
    assert params['initial_prompt'] == 'some trained prompt'
    assert params['vad_filter'] is True  # accurate mode's own default


@pytest.mark.parametrize('mode,expected_vad_filter', [
    ('fast', True), ('balanced', True), ('accurate', True),
])
def test_hotkey_vad_filter_matches_wake_path_not_force_disabled(mode, expected_vad_filter):
    """2026-07-10 A/B-experiment-driven reversal, locked deliberately (see
    module docstring and dictation.py's _build_hotkey_transcribe_params
    docstring for the full writeup): vad_filter must NOT be force-disabled
    for the hotkey path in any performance mode -- every mode already
    defaults it True, and the hotkey path must not override that back to
    False. This is the parameter that fixed a real "you know" -> "i know"/
    garbage word-loss defect; silently re-disabling it would reintroduce
    that defect."""
    app = _make_app(performance_mode=mode, initial_prompt='some trained prompt')
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert params['vad_filter'] is expected_vad_filter


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
