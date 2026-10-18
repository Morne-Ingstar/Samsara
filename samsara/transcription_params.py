"""Pure decode profile construction and in-place hotkey overrides.

App state and prompt/language resolution stay in the DictationApp facade.
"""
from samsara.config_defaults import DEFAULTS

def get_transcription_params(config, *, language, initial_prompt,
                             no_speech_threshold, log_prob_threshold):
    """Get transcription parameters based on performance mode setting.

    The app facade resolves language and initial_prompt before calling here.
    Its include_vocabulary=False builds initial_prompt from ONLY the
    explicit user-set config['initial_prompt'] override (Priority 1),
    omitting genuine trained vocabulary and auto-derived command
    phrases (Priorities 2/3) -- see dictionary_store.get_initial_prompt
    for the full history. Free-form dictation paths (hold-to-dictate,
    toggle-session DICTATE/AVA, transcribe_continuous_buffer,
    process_wake_word_buffer) pass False; command-matched paths keep
    the default True. Defaults to True so every OTHER existing caller
    (streaming.py's final-pass params, the voice-training phrase
    self-test) is unaffected by this parameter's addition.

    Returns dict of parameters for model.transcribe()
    Performance modes:
    - fast: Lowest latency, may sacrifice some accuracy
    - balanced: Good speed/accuracy tradeoff
    - accurate: Best accuracy, slower (default)
    """
    mode = config.get('performance_mode', DEFAULTS['performance_mode'])

    base_params = {
        # Single source of truth for the Whisper `language` kwarg --
        # "auto" resolves to None (faster-whisper auto-detect); every
        # other value passes through as-is. See samsara/languages.py.
        'language': language,
        'initial_prompt': initial_prompt,
        # Native faster-whisper silence suppression (primary hallucination
        # defense -- see "Gate and Reset" architecture, module-level
        # constants in dictation.py). More causal than any post-hoc text check:
        # Whisper itself returns empty on low-speech-probability audio.
        'no_speech_threshold': no_speech_threshold,
        'log_prob_threshold': log_prob_threshold,
    }

    if mode == 'fast':
        # Fastest settings - greedy decoding, minimal VAD
        return {
            **base_params,
            'beam_size': 1,  # Greedy decoding (fastest)
            'vad_filter': True,
            'vad_parameters': dict(
                min_silence_duration_ms=300,
                speech_pad_ms=100,
            ),
            'condition_on_previous_text': False,
            # Queue 124: was True, and that silently lost long-form
            # speech -- see the block on `balanced` below for the
            # mechanism and the measurements. `fast` had it worse than
            # `balanced` (87.9% of the words returned against 94.4%), and
            # fixing it costs `fast` 0.21 WER points and 16 ms on short
            # clips. "May sacrifice some accuracy" is a fair description
            # of beam 1; silently dropping an eighth of a paragraph is
            # not, and it is not what anyone chooses this mode for.
            'without_timestamps': False,
            'word_timestamps': False,
            'temperature': 0.0,  # Deterministic (faster)
        }
    elif mode == 'accurate':
        # Most accurate settings
        return {
            **base_params,
            'beam_size': 5,
            'vad_filter': True,
            'vad_parameters': dict(
                min_silence_duration_ms=500,
                speech_pad_ms=300,
            ),
            'condition_on_previous_text': True,
            'without_timestamps': False,
            'word_timestamps': False,
        }
    else:  # balanced
        return {
            **base_params,
            'beam_size': 3,
            'vad_filter': True,
            'vad_parameters': dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200,
            ),
            'condition_on_previous_text': False,
            # Queue 124. THIS LINE WAS `True`, AND IT LOST DICTATED TEXT.
            # Do not put it back without reading the report.
            #
            # Measured on 5 minutes of continuous read speech, one
            # parameter changed per arm (perf_artifacts + queue 124):
            #
            #   base   balanced              7.95% WER, 94.4% of words
            #   base   + without_timestamps  2.89% WER, 99.5% of words
            #   small  balanced             34.88% WER, 66.1% of words
            #   small  + without_timestamps  1.63% WER, 99.5% of words
            #
            # condition_on_previous_text is NOT the cause -- flipping it
            # alone changed nothing (base 7.95% -> 8.31%). The 118 report
            # attributed the loss to lost context; that was wrong.
            #
            # MECHANISM, read from faster_whisper/transcribe.py: with
            # without_timestamps=True the prompt carries the
            # `no_timestamps` token (:1554), so the model emits no
            # timestamp tokens, so _split_segments_by_timestamps finds
            # none and falls through to `seek += segment_size` -- the
            # decoder can only advance in whole 30 s blocks. A window
            # culled by no_speech_threshold/log_prob_threshold does
            # `seek += segment_size; continue` (:1234) and takes 30
            # SECONDS OF SPEECH with it, and a window that decodes only
            # part of its audio cannot resume at the last word. With
            # timestamps on, seek advances to the last decoded timestamp
            # (:1079) and nothing between windows is lost.
            #
            # Short utterances hide all of it: they fit in one window, so
            # there is no seam to lose. That is why every prior bench
            # missed this.
            'without_timestamps': False,
            'word_timestamps': False,
        }


def build_hotkey_transcribe_params(base_params, *, command_mode_recording):
    """Build model.transcribe() kwargs for the hotkey dictation path.

    Starts from get_transcription_params() (mode-based defaults) and
    forces the hotkey-specific overrides: VAD disabled (the user
    explicitly pressed the hotkey -- don't strip their speech), and a
    clean per-press conversation-context reset (no residual conditioning
    carried over from a previous press -- see the "Gate and Reset"
    hallucination-prevention architecture, module-level constants near
    the top of dictation.py). Used by both the normal (<30s) and [LONG]
    branches of the hotkey transcribe() closure -- they share this same
    dict, so this is the single place that guarantee is enforced.

    initial_prompt's vocabulary component: this method is shared by two
    different hotkey presses distinguished only by
    command_mode_recording at call time -- ordinary hold-to-dictate
    (free prose, never matched against the command registry) and the
    command-only hotkey / Mouse 4 (self.command_mode_recording=True,
    matched against the command registry a few lines below). Only the
    latter actually needs vocabulary biasing at all;
    the facade's get_transcription_params include_vocabulary therefore mirrors
    command_mode_recording exactly. Explicit custom prompt (Priority 1,
    config['initial_prompt']) still applies to both regardless -- it's
    a user-set override, not auto-derived vocabulary.

    HISTORY: 2026-07-16 (commit 02e00b9) first gated only the
    auto-derived command-phrase list (Priority 3) off this path, having
    proved it destabilized long continuous-speech decodes. 2026-07-17/18
    (SPARK decode matrix, N=10/cell against both incident WAVs) found
    the SAME failure mode from Priority 2 alone (the short "Common
    terms:" trained-vocabulary list) -- ANY non-conversational
    vocabulary content in initial_prompt is the actual destabilizer,
    not command phrases specifically. Widened accordingly: hold-to-
    dictate now drops Priorities 2 AND 3 both, keeping only the
    explicit Priority-1 override if the user set one.

    vad_filter=False HISTORY (2026-07-10, flipped twice in one night --
    read this before touching it again): originally force-disabled
    (the setting you see now) from the click/bloop hallucination era,
    on the theory that faster-whisper's own VAD could strip a user's
    genuine speech right after the hotkey press. Commit 576f412
    flipped it to True (mode default) based on an A/B decode-parameter
    experiment (tools/transcribe_ab.py) against dumps of "you know
    what I mean" transcribing as "i know what i mean"/garbage -- BUT
    that experiment ran against Whisper "base" (transcribe_ab.py's
    hardcoded model), not the production model, and the observed
    defect turned out to be unrelated to decode parameters entirely:
    samsara/cleanup.py's FILLERS list stripped r'\\byou know\\b'
    UNANCHORED, deleting the phrase from every position in every
    dictation regardless of vad_filter, downstream of Whisper. Fixed
    there (comma-anchored, matching every other context-sensitive
    filler in that list) instead. Re-running the same dumps confirms
    the PRODUCTION model transcribes them correctly with vad_filter
    True OR False -- the A/B result that justified the flip doesn't
    replicate once the real (cleanup.py) cause is fixed, so this
    reverts to the original force-False: smaller change surface, and
    the theoretical clipping risk it guards against was never actually
    disproven, only a different, unrelated bug was found and fixed.
    tools/transcribe_ab.py now accepts --model/--device and defaults
    to the live-config model rather than a hardcoded 'base', so this
    specific model-mismatch confound can't recur silently.
    tests/test_transcription_params.py's vad_filter lock was reverted
    to match -- see that file for the test-level documentation.
    """
    # The facade resolves include_vocabulary from command_mode_recording (True only for
    # the command-only hotkey / Mouse 4, matched below): that press
    # benefits from vocabulary biasing (trained "Common terms" + the
    # auto-derived command-phrase list), but ordinary hold-to-dictate
    # never matches the command registry, so it drops both -- see the
    # docstring above.
    transcribe_params = base_params
    transcribe_params['initial_prompt'] = transcribe_params['initial_prompt'] or ""
    # DISABLE faster-whisper's VAD for hotkey-triggered dictation.
    # User explicitly pressed the hotkey — don't strip their speech.
    transcribe_params['vad_filter'] = False
    # Force a clean slate on EVERY hotkey press. Conditioning on
    # tokens carried over from a previous press is what let
    # hallucinations escalate over a session -- each press must
    # start with zero residual conversational state, independent of the
    # [LONG] path (which has its own reasons not to condition).
    transcribe_params['condition_on_previous_text'] = False
    # Command-mode hotkey (Right Ctrl / Mouse 4, self.command_mode_recording)
    # is matched against the English command registry -- force English
    # regardless of the configured dictation language so command
    # recognition stays reliable. Commands are English-only by design;
    # Ava-mode (Right Alt) recordings are NOT forced here since that
    # content goes to the LLM as a natural-language query, not matched
    # against a fixed phrase registry.
    if command_mode_recording:
        transcribe_params['language'] = 'en'
    return transcribe_params

