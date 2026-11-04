"""Session-local manual override for the resident CPU dictation model."""
from samsara.plugin_commands import command
from samsara.game_mode import set_manual


@command('game mode on', whole_utterance=True, pack='core', risk_class='ui')
def game_mode_on(app, remainder=''):
    """Uses CPU dictation without live partials until game mode is turned off."""
    return set_manual(app, True)


@command('game mode off', whole_utterance=True, pack='core', risk_class='ui')
def game_mode_off(app, remainder=''):
    """Restores the previous dictation hardware and disables automatic switching."""
    return set_manual(app, False)


@command('game mode auto', whole_utterance=True, pack='core', risk_class='ui')
def game_mode_auto(app, remainder=''):
    """Returns game-mode detection to the automatic setting."""
    return set_manual(app, None)
