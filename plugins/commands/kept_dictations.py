"""Explicitly keep the last dictation; exact phrases never capture prose."""
from samsara.plugin_commands import command


@command('keep that', aliases=['save that dictation'], pack='core',
         risk_class='safe', ai_visible=False, whole_utterance=True)
def keep_dictation(app, remainder='', **kwargs):
    # PLACEHOLDER COPY (1176)
    """Saves audio and text in History > Kept, even with automatic keeping off."""
    if remainder.strip():
        return False
    from samsara.kept_dictations import request_keep
    request_keep(app, voice=True)
    return True
