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

vad_filter FLIPPED TWICE in one night (2026-07-10) -- read this before
touching it again. It was force-disabled from the click/bloop
hallucination era ("user explicitly pressed the hotkey -- don't strip
their speech"). Commit 576f412 flipped it to the mode default (True) based
on an A/B decode-parameter experiment (tools/transcribe_ab.py) against
"you know what I mean" hotkey dumps that transcribed as "i know what i
mean"/garbage. That experiment ran against faster-whisper "base"
(transcribe_ab.py's hardcoded model), not the production model, and the
real cause turned out to be unrelated to decode parameters: samsara/
cleanup.py's FILLERS list stripped r'\byou know\b' UNANCHORED, deleting
the phrase from every position in every dictation downstream of Whisper,
independent of vad_filter. Fixed there instead (comma-anchored, matching
the module's other context-sensitive fillers). Re-running the same dumps
against the PRODUCTION model shows it transcribes them correctly with
vad_filter True OR False -- the A/B result doesn't replicate once the real
cause is fixed, so this reverts to the original force-False (smaller
change surface; the clipping-risk concern it exists for was never actually
disproven, only a different bug was found). tools/transcribe_ab.py now
accepts --model/--device and defaults to the live-config model instead of
a hardcoded 'base', so this specific model-mismatch confound can't recur
silently. If you're tempted to flip this a third time: fix the actual bug
first (check samsara/cleanup.py and any other post-Whisper text pipeline
before touching decode parameters), and reproduce against the production
model, not a hardcoded stand-in.

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
    otherwise set condition_on_previous_text=True (accurate mode) or
    vad_filter=True (every mode). The voice training prompt (vocabulary
    biasing) is NOT reset -- only conversation context
    (condition_on_previous_text) and VAD are forced."""
    app = _make_app(performance_mode='accurate', initial_prompt='some trained prompt')
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert params['condition_on_previous_text'] is False
    assert params['initial_prompt'] == 'some trained prompt'
    assert params['vad_filter'] is False


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_hotkey_vad_filter_matches_wake_path_not_force_disabled(mode):
    """Original lock, restored 2026-07-10 (see module docstring for the
    full flip-flop history -- vad_filter went False -> True -> False in
    one night): vad_filter must be force-disabled for the hotkey path in
    EVERY performance mode, regardless of what the mode default would
    otherwise be. Despite this test's name (kept for git-blame continuity
    across the reversal -- it originally asserted the opposite), the
    invariant it locks is: hotkey vad_filter is always False."""
    app = _make_app(performance_mode=mode, initial_prompt='some trained prompt')
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
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
