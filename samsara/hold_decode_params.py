"""Decode parameters for the free-form hold-to-record streaming path (813).

Hold dictation is free-form prose: dictionary_store.get_initial_prompt
documents that the vocabulary layers ("Common terms:", built-in hints,
command phrases) must stay out of initial_prompt there. On a one-word
clip a vocabulary-list prompt is an unusual decoder context, so partials
and the final decode ask for include_vocabulary=False. Only the user's own
explicit initial_prompt override is kept.
"""
import logging

logger = logging.getLogger('Samsara')


def free_form_prompt(app):
    """The hold path's initial_prompt: explicit override only, never vocabulary."""
    from samsara.dictionary_store import get_dictionary_store
    try:
        return get_dictionary_store(app).get_initial_prompt(include_vocabulary=False)
    except Exception as e:
        logger.debug(f"[STREAM] get_initial_prompt failed: {e}")
        return None


def final_base_params(app, language):
    """get_transcription_params for a hold final, with vocabulary off."""
    try:
        return dict(app.get_transcription_params(include_vocabulary=False))
    except Exception as e:
        logger.debug(f"[STREAM] get_transcription_params failed: {e}")
        return {'language': language, 'initial_prompt': None}
