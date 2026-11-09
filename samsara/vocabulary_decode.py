"""Bounded vocabulary delivery at the application decode boundary (1009).

Hotwords are decoder context too: this proves delivery, not acoustic accuracy.
Keep vocabulary lists out of prose initial_prompt (813); preserve explicit
context and recovery overrides. No model loading, Qt, or profile reads here.
"""
import logging
import threading

from samsara.builtin_vocabulary import builtin_entries, relevant_words, word_key
from samsara.dictionary_store import get_dictionary_store

logger = logging.getLogger(__name__)
_LOG_LOCK = threading.RLock()
PATHS = ('hold', 'hold_long', 'commit_redecode', 'ava', 'continuous', 'wake',
         'command_fast', 'session', 'hold_partial', 'handsfree_preview',
         'hold_final', 'wake_debug')


def _token_count(model, text):
    if not text:
        return 0
    tokenizer = getattr(model, 'hf_tokenizer', None)
    if tokenizer is not None:
        try:
            return len(tokenizer.encode(' ' + text.strip(), add_special_tokens=False).ids)
        except (AttributeError, TypeError, ValueError):
            pass
    # Byte-level BPE cannot require more tokens than its UTF-8 bytes.
    return len((' ' + text.strip()).encode('utf-8'))


def _terms(app):
    config = getattr(app, 'config', {}) or {}
    store = get_dictionary_store(app)
    users = getattr(store, 'custom_vocab', ())
    if not isinstance(users, (list, tuple)):
        users = ()  # Legacy test/plugin stores may expose only get_initial_prompt.
    preferences = getattr(store, '_builtin_preferences', None)
    disabled = getattr(preferences, 'disabled', ())
    if not isinstance(disabled, (set, list, tuple)):
        disabled = ()
    builtins = relevant_words(config, users, disabled)
    core = {word_key(entry['word']) for entry in builtin_entries(config)
            if 'core' in entry['packs']}
    ordered = list(users) + [w for w in builtins if word_key(w) in core]
    ordered += [w for w in builtins if word_key(w) not in core]
    seen, result = set(), []
    for word in ordered:
        if not isinstance(word, str) or not word.strip():
            continue
        key = word_key(word)
        if key not in seen:
            seen.add(key)
            result.append(word.strip())
    return result


def vocabulary_params(app, params):
    """Fit complete terms, user then core then other enabled packs, to 223 tokens.

    Budget initial_prompt and hotwords together, including their leading spaces.
    Explicit/context prompts take precedence; trim oversize prompts on word
    boundaries so faster-whisper cannot silently discard their beginning.
    No caller uses prefix; reject it rather than silently lose hotwords (the
    faster-whisper API ignores hotwords when prefix is present).
    """
    result = dict(params)
    if result.get('prefix'):
        raise ValueError('Vocabulary decoding does not support prefix')
    model = getattr(app, 'model', None)
    prompt = result.get('initial_prompt')
    if prompt:
        while prompt and _token_count(model, prompt) > 223:
            prompt = prompt.rsplit(' ', 1)[0] if ' ' in prompt else ''
        result['initial_prompt'] = prompt
    available = 223 - _token_count(model, prompt)
    kept = []
    for word in _terms(app):
        candidate = ', '.join(kept + [word])
        if _token_count(model, candidate) <= available:
            kept.append(word)
    result['hotwords'] = ', '.join(kept) or None
    return result, len(kept)


def log_vocabulary(app, reason='session_start'):
    """One count-only summary per session start or effective vocabulary change."""
    config = getattr(app, 'config', {}) or {}
    params, count = vocabulary_params(app, {
        'initial_prompt': config.get('initial_prompt') or None})
    signature = (tuple(_terms(app)), config.get('initial_prompt'))
    with _LOG_LOCK:
        state = getattr(app, '__dict__', {}).get('_vocabulary_decode_log')
        if reason == 'change' and state and state['signature'] == signature:
            return
        app._vocabulary_decode_log = {'signature': signature, 'paths': set()}
        logger.debug('[VOCAB] reason=%s paths=%s terms=%d prompt_tokens=%d budget=223',
                     reason, ','.join(PATHS), count,
                     _token_count(getattr(app, 'model', None), params.get('initial_prompt')))


def transcribe_with_vocabulary(app, audio, *, path, **params):
    """All active application call sites pass through here, including retries."""
    from samsara.game_mode import decode_model
    from copy import copy
    selected = decode_model(app, path)
    if selected is not app.model:
        app = copy(app)
        app.model = selected
    effective, count = vocabulary_params(app, params)
    signature = (tuple(_terms(app)), (getattr(app, 'config', {}) or {}).get('initial_prompt'))
    with _LOG_LOCK:
        state = getattr(app, '__dict__', {}).get('_vocabulary_decode_log')
        if not state or state['signature'] != signature:
            log_vocabulary(app, 'change' if state else 'session_start')
            state = app._vocabulary_decode_log
        if path not in state['paths']:
            state['paths'].add(path)
            logger.debug('[VOCAB] path=%s terms=%d prompt_tokens=%d budget=223',
                         path, count, _token_count(app.model, effective.get('initial_prompt')))
    from samsara.hallucination_filter import transcribe_without_outro
    from samsara.config_defaults import cfg_get
    from samsara.hold_recording_recovery import incident_model
    model = app.model if path in ('hold_partial', 'handsfree_preview') else incident_model(app)
    from samsara.game_mode import observe_decode
    model = observe_decode(app, model, path)  # 1200: diagnostics only; native call/pull timing.
    result = transcribe_without_outro(model, audio, effective,
        rate=getattr(app, 'model_rate', 16000), enabled=path not in ('hold_partial', 'handsfree_preview')
        and cfg_get(getattr(app, 'config', {}) or {}, 'dictation.signoff_filter'))
    if path == 'commit_redecode':
        from samsara.segment_speech_guard import screen_commit
        return screen_commit(result, audio, getattr(app, 'model_rate', 16000), effective)
    return result
