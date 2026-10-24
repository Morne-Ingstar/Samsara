"""Local Dictionary teaching, shared with Ava and usable with AI off."""
from samsara.plugin_commands import command


@command('add word', aliases=['teach word', 'learn word', 'learn the word', 'add the word'],
         pack='core', risk_class='safe', ai_visible=False,
         side_effects=['file', 'ui', 'audio'], reversible=True)
def add_word(app, remainder='', **kwargs):
    """Teach a Dictionary word; spell unfamiliar names and answer yes to the readback."""
    from samsara import teach_patterns
    from plugins.commands.ask_ollama import _check_teaching_intent
    if (getattr(app, '_dictionary_add_focused', False) is True
            and remainder.strip().rstrip('.!?').casefold() == 'add'):
        app._schedule_ui(app._dictionary_panel.add_by_voice)
        return None
    text = remainder if teach_patterns.dictionary_command(remainder) else 'add word ' + remainder
    return _check_teaching_intent(app, text)


@command('spell word', pack='core', risk_class='safe', ai_visible=False,
         side_effects=['file', 'ui', 'audio'], reversible=True)
def spell_word(app, remainder='', **kwargs):
    """Spell a Dictionary word with letter names or NATO words, then say yes."""
    from plugins.commands.ask_ollama import _check_teaching_intent
    text = remainder if remainder.lower().startswith('spell word') else 'spell word ' + remainder
    return _check_teaching_intent(app, text)


@command('add that to the dictionary', pack='core', risk_class='safe', ai_visible=False,
         side_effects=['ui'], reversible=True)
def add_that(app, remainder='', **kwargs):
    """Open Dictionary with the last delivered dictation filled in; edit it, then say add."""
    from plugins.commands.ask_ollama import _check_teaching_intent
    return _check_teaching_intent(app, 'add that to the dictionary')
