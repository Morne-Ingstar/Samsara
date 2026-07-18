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
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


_COMMAND_VOCAB_MARKER = 'Voice commands: test_command_a, test_command_b'
_COMMON_TERMS_MARKER = 'Common terms: test_vocab_a, test_vocab_b'


def _make_app(performance_mode='balanced', initial_prompt='', command_mode_recording=False):
    """Minimal duck-typed stand-in for DictationApp's `self`.

    _build_hotkey_transcribe_params() calls self.get_transcription_params()
    internally, so that real method must be bound onto the fake app too
    (not just invoked as DictationApp.get_transcription_params(app) directly).

    The fake get_initial_prompt mirrors the REAL method's include_vocabulary
    contract (samsara/ui/voice_training_qt.py, widened 2026-07-18 from the
    narrower include_commands): include_vocabulary=False (the default here,
    matching command_mode_recording=False) returns `initial_prompt`
    unchanged (Priority 1 only); include_vocabulary=True appends BOTH a
    trained-vocabulary marker (Priority 2, "Common terms") and a command-
    vocabulary marker (Priority 3), so tests can assert on either's
    presence/absence without depending on real custom_vocab or the real
    command registry.
    """
    app = types.SimpleNamespace()
    app.config = {'language': 'en', 'performance_mode': performance_mode}
    app.command_mode_recording = command_mode_recording

    def _fake_get_initial_prompt(include_vocabulary=True):
        if include_vocabulary:
            parts = [p for p in (initial_prompt, _COMMON_TERMS_MARKER, _COMMAND_VOCAB_MARKER) if p]
            return ' '.join(parts)
        return initial_prompt

    app.voice_training_window = types.SimpleNamespace(
        get_initial_prompt=_fake_get_initial_prompt
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


# ============================================================================
# initial_prompt / vocabulary contract.
#
# 2026-07-16 (commit 02e00b9, following the fail-loud sanity check in
# 5048bc6): the command-only hotkey (Right Ctrl / Mouse 4,
# command_mode_recording=True) IS matched against the command registry and
# keeps the auto-derived command-phrase vocabulary (Priority 3). Ordinary
# hold-to-dictate (command_mode_recording=False, the overwhelmingly common
# case) is pure prose, never command-matched, and must NOT receive it --
# that vocabulary measurably destabilized long continuous-speech decodes
# (19 of 51 recent >30s captures showed the signature) for zero benefit on
# this path.
#
# 2026-07-17/18 (SPARK P0 fix, decode matrix N=10/cell against both
# incident WAVs): the SAME destabilization reproduces from Priority 2
# alone (the short trained "Common terms" vocabulary) -- ANY
# non-conversational vocabulary content in initial_prompt is the
# destabilizer, not command phrases specifically. Free-form paths now drop
# BOTH Priority 2 and 3, keeping only Priority 1 (explicit custom prompt,
# a user-set override rather than auto-derived vocabulary) -- see
# samsara/ui/voice_training_qt.py's get_initial_prompt(include_vocabulary=...).
# ============================================================================

@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_hold_to_dictate_omits_all_vocabulary(mode):
    """command_mode_recording defaults to False (ordinary hold-to-dictate)
    -- NEITHER the command-vocabulary marker NOR the common-terms marker
    may appear in its initial_prompt. Only the explicit custom prompt
    (Priority 1) survives."""
    app = _make_app(performance_mode=mode, initial_prompt='some trained prompt',
                     command_mode_recording=False)
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert _COMMAND_VOCAB_MARKER not in params['initial_prompt']
    assert _COMMON_TERMS_MARKER not in params['initial_prompt']
    assert params['initial_prompt'] == 'some trained prompt'


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_hold_to_dictate_with_no_explicit_prompt_is_empty(mode):
    """The common case: no explicit custom prompt configured. Free-form
    hold-to-dictate must get a genuinely empty initial_prompt, not merely
    "vocabulary-free" -- this is the literal "initial_prompt must be
    empty/None" requirement for the overwhelmingly common no-override case."""
    app = _make_app(performance_mode=mode, initial_prompt='',
                     command_mode_recording=False)
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert params['initial_prompt'] == ''


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_command_hotkey_keeps_all_vocabulary(mode):
    """command_mode_recording=True (Right Ctrl / Mouse 4 command-only
    hotkey) IS matched against the command registry -- it must keep
    receiving BOTH the command vocabulary AND the trained common-terms
    vocabulary, unlike ordinary hold-to-dictate."""
    app = _make_app(performance_mode=mode, initial_prompt='some trained prompt',
                     command_mode_recording=True)
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert _COMMAND_VOCAB_MARKER in params['initial_prompt']
    assert _COMMON_TERMS_MARKER in params['initial_prompt']
    assert 'some trained prompt' in params['initial_prompt']


def test_command_hotkey_still_forces_english_and_clean_slate():
    """The include_vocabulary wiring must not disturb the pre-existing
    command-hotkey overrides (English language, clean-slate reset) -- this
    guards against a careless refactor of the include_vocabulary branch
    accidentally short-circuiting the language/condition_on_previous_text
    forcing below it."""
    app = _make_app(performance_mode='accurate', initial_prompt='p',
                     command_mode_recording=True)
    params = dictation.DictationApp._build_hotkey_transcribe_params(app)
    assert params['language'] == 'en'
    assert params['condition_on_previous_text'] is False
    assert params['vad_filter'] is False


# ============================================================================
# get_transcription_params(include_vocabulary=...) base contract -- the
# shared method _handle_command_mode_utterance, transcribe_continuous_buffer,
# and process_wake_word_buffer all call directly (unlike the hotkey path,
# which builds its own params via _build_hotkey_transcribe_params above).
# ============================================================================

@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_get_transcription_params_include_vocabulary_false_is_priority_1_only(mode):
    app = _make_app(performance_mode=mode, initial_prompt='explicit override')
    params = dictation.DictationApp.get_transcription_params(app, include_vocabulary=False)
    assert params['initial_prompt'] == 'explicit override'
    assert _COMMON_TERMS_MARKER not in params['initial_prompt']
    assert _COMMAND_VOCAB_MARKER not in params['initial_prompt']


@pytest.mark.parametrize('mode', ['fast', 'balanced', 'accurate'])
def test_get_transcription_params_include_vocabulary_true_is_default(mode):
    """No argument must behave identically to include_vocabulary=True --
    every pre-existing caller that doesn't pass this new parameter
    (streaming.py's final-pass params, the voice-training phrase
    self-test) must see no behavior change."""
    app = _make_app(performance_mode=mode, initial_prompt='p')
    default_params = dictation.DictationApp.get_transcription_params(app)
    explicit_params = dictation.DictationApp.get_transcription_params(app, include_vocabulary=True)
    assert default_params['initial_prompt'] == explicit_params['initial_prompt']
    assert _COMMON_TERMS_MARKER in default_params['initial_prompt']
    assert _COMMAND_VOCAB_MARKER in default_params['initial_prompt']


# ============================================================================
# Per-path wiring: does each of the three methods that call
# get_transcription_params() directly (not through _build_hotkey_transcribe_
# params) pass the correct include_vocabulary value? Each test patches
# get_transcription_params with a spy that raises immediately after
# recording its kwargs -- this captures the ONE call under test without
# needing to mock the rest of that method's downstream pipeline (model
# decode, diagnostics, corrections, command dispatch), matching this
# module's established "exercise the real production code, minimal
# scaffolding" philosophy for a method this large.
# ============================================================================

class _FakeModelLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _base_fake_app(**extra):
    """Common scaffolding for the three per-path wiring tests below: real
    model.transcribe() is mocked to return zero segments, so each method's
    OWN pre-existing "text came back empty" early-return does all the
    cleanup for us -- no need to mock command dispatch, diagnostics,
    corrections, or history beyond the minimum each method touches before
    that return. get_transcription_params is spied (not replaced with a
    raising stub) so real execution completes normally down that path."""
    captured = {}

    def _capture_get_transcription_params(self, **kwargs):
        captured['include_vocabulary'] = kwargs.get('include_vocabulary', True)
        return {
            'language': 'en', 'initial_prompt': '', 'no_speech_threshold': 0.6,
            'log_prob_threshold': -1.0, 'beam_size': 3, 'vad_filter': True,
            'vad_parameters': {'min_silence_duration_ms': 500, 'speech_pad_ms': 200},
            'condition_on_previous_text': False, 'without_timestamps': True,
            'word_timestamps': False,
        }

    app = types.SimpleNamespace(
        capture_rate=16000, model_rate=16000, config={},
        model=Mock(), model_lock=_FakeModelLock(),
        voice_training_window=types.SimpleNamespace(apply_corrections=lambda t: t),
        # Unconditional finally-block cleanup a couple of these methods
        # perform regardless of the early "text came back empty" return --
        # no-ops here since none of it is what's under test.
        _vad_reset=lambda: None, _vad_available=True, _log_history=lambda **k: None,
        **extra,
    )
    app.model.transcribe.return_value = ([], types.SimpleNamespace(language='en'))
    app.get_transcription_params = types.MethodType(_capture_get_transcription_params, app)
    return app, captured


def test_transcribe_continuous_buffer_omits_vocabulary():
    """Free-form path -- always include_vocabulary=False, no branching."""
    app, captured = _base_fake_app()
    buffer = [np.zeros(16000, dtype=np.float32)]  # 1.0s, above the 0.51s guard
    dictation.DictationApp.transcribe_continuous_buffer(app, buffer)
    assert captured.get('include_vocabulary') is False


def test_process_wake_word_buffer_dictation_lane_omits_vocabulary():
    """Free-form path -- always include_vocabulary=False (this method has
    no internal command/dictation branch; the whole lane is dictation)."""
    app, captured = _base_fake_app(
        _wake_transcription_in_progress=False,
        _wake_audio_is_below_gate=lambda *a, **k: False,
        app_state='wake_session',
        _restart_wake_session_timer=lambda: None,
        _wake_diag_acc=None,
    )
    buffer = [np.zeros(16000, dtype=np.float32)]
    dictation.DictationApp.process_wake_word_buffer(app, buffer)
    assert captured.get('include_vocabulary') is False


@pytest.mark.parametrize('mode, expected', [
    (dictation.SessionMode.COMMAND, True),
    (dictation.SessionMode.DICTATE, False),
    (dictation.SessionMode.AVA, False),
])
def test_handle_command_mode_utterance_gates_on_session_mode(mode, expected):
    """COMMAND (matcher-side, short utterances) keeps vocabulary; DICTATE
    and AVA (both free-form -- prose to a text field or an LLM query, never
    command-matched) drop it, same rule as every other free-form path."""
    app, captured = _base_fake_app(
        _wake_transcription_in_progress=False,
        _ensure_session_mode_manager=lambda: types.SimpleNamespace(mode=mode),
    )
    buffer = [np.zeros(16000, dtype=np.float32)]
    dictation.DictationApp._handle_command_mode_utterance(app, buffer, 16000)
    assert captured.get('include_vocabulary') is expected


# ============================================================================
# SPARK P0 auto-retry: _apply_retry_on_suspected_loss. Pure function --
# audio/sample_rate/duration are only forwarded to
# _suspected_silent_data_loss, which is monkeypatched here so the trigger
# logic itself (not the sanity-check heuristic, already covered by
# test_long_dictation_quality.py's TestSuspectedSilentDataLoss) is what's
# under test.
# ============================================================================

class TestRetryOnSuspectedLoss:
    def _result(self, text, low_confidence=False):
        return dictation._HotkeyDecodeResult(text, low_confidence, [], 'en', 'short')

    def test_no_retry_when_not_suspected(self, monkeypatch):
        monkeypatch.setattr(dictation, '_suspected_silent_data_loss', lambda *a, **k: False)
        original = self._result('a complete sentence')
        retry_fn = Mock(side_effect=AssertionError('retry_fn must not be called'))
        result, suspected, retried = dictation._apply_retry_on_suspected_loss(
            original, retry_fn, audio=None, sample_rate=16000, audio_duration=30.0,
            is_command_lane=False,
        )
        assert result is original
        assert suspected is False
        assert retried is False
        retry_fn.assert_not_called()

    def test_no_retry_on_command_lane_even_when_suspected(self, monkeypatch):
        """Retry must NEVER fire on command-lane decodes -- matcher-side,
        short utterances; this mechanism exists for long free-form prose."""
        monkeypatch.setattr(dictation, '_suspected_silent_data_loss', lambda *a, **k: True)
        original = self._result('short')
        retry_fn = Mock(side_effect=AssertionError('retry_fn must not be called'))
        result, suspected, retried = dictation._apply_retry_on_suspected_loss(
            original, retry_fn, audio=None, sample_rate=16000, audio_duration=30.0,
            is_command_lane=True,
        )
        assert result is original
        assert suspected is True  # still reported so diagnostics reflect it
        assert retried is False
        retry_fn.assert_not_called()

    def test_retry_fires_exactly_once_and_delivers_retry_when_it_passes(self, monkeypatch):
        calls = {'n': 0}

        def _fake_sanity(text, *a, **k):
            # First call (original text) -> suspected. Second call (retry
            # text) -> recovered. Anything after that is a bug (more than
            # one retry).
            calls['n'] += 1
            if calls['n'] == 1:
                return True
            if calls['n'] == 2:
                return False
            raise AssertionError('sanity check invoked more than twice -- retry fired more than once')

        monkeypatch.setattr(dictation, '_suspected_silent_data_loss', _fake_sanity)
        original = self._result('codex transcripts')
        retry_result = self._result('so I made a folder for codex transcripts mainly the ones run through CLI')
        retry_fn = Mock(return_value=retry_result)

        result, suspected, retried = dictation._apply_retry_on_suspected_loss(
            original, retry_fn, audio=None, sample_rate=16000, audio_duration=24.0,
            is_command_lane=False,
        )
        retry_fn.assert_called_once()
        assert result is retry_result  # ONLY the retry's result is delivered
        assert suspected is False
        assert retried is True

    def test_retry_fires_once_and_delivers_the_longer_when_both_fail(self, monkeypatch):
        monkeypatch.setattr(dictation, '_suspected_silent_data_loss', lambda *a, **k: True)
        original = self._result('a somewhat longer original fragment of text')
        shorter_retry = self._result('short retry')
        retry_fn = Mock(return_value=shorter_retry)

        result, suspected, retried = dictation._apply_retry_on_suspected_loss(
            original, retry_fn, audio=None, sample_rate=16000, audio_duration=24.0,
            is_command_lane=False,
        )
        retry_fn.assert_called_once()
        assert result is original  # original had more chars
        assert suspected is True
        assert retried is True

    def test_retry_fires_once_and_delivers_the_longer_retry_when_both_fail(self, monkeypatch):
        monkeypatch.setattr(dictation, '_suspected_silent_data_loss', lambda *a, **k: True)
        original = self._result('short original')
        longer_retry = self._result('a substantially longer retry fragment of recovered text')
        retry_fn = Mock(return_value=longer_retry)

        result, suspected, retried = dictation._apply_retry_on_suspected_loss(
            original, retry_fn, audio=None, sample_rate=16000, audio_duration=24.0,
            is_command_lane=False,
        )
        retry_fn.assert_called_once()
        assert result is longer_retry  # retry had more chars, even though it also failed
        assert suspected is True
        assert retried is True

    def test_retry_decode_uses_empty_initial_prompt(self):
        """Integration-shaped check on the REAL retry_fn construction (not
        just the pure trigger function above): the closure built in the
        hotkey transcribe() path must override initial_prompt to "" for
        the retry, regardless of what the original transcribe_params had
        (including a non-empty explicit Priority-1 override -- the retry
        is the last-resort recovery attempt, see
        _apply_retry_on_suspected_loss's docstring)."""
        captured = {}

        class _FakeApp:
            model_rate = 16000

            def _decode_hotkey_audio(self, audio_faded, transcribe_params, audio_duration):
                captured['initial_prompt'] = transcribe_params['initial_prompt']
                return dictation._HotkeyDecodeResult('retried text', False, [], 'en', 'short')

        transcribe_params = {'initial_prompt': 'some explicit override', 'language': 'en'}

        def _retry_decode():
            _retry_params = dict(transcribe_params)
            _retry_params['initial_prompt'] = ""
            return _FakeApp()._decode_hotkey_audio(None, _retry_params, 24.0)

        _retry_decode()
        assert captured['initial_prompt'] == ""
