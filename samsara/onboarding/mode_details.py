"""Shared placeholder explanations, formatted with each surface's live facts."""

# PLACEHOLDER COPY (378): owner reviews these shared sentences.
MODE_DETAILS = {
    "hold": "Hold the key and speak. Everything you say is typed when you let go. You can change this to a toggle: tap the key, speak, and tap again when you're done.",
    "tap": "Tap the key and speak. Everything you say is typed when you tap again. Nothing to hold; it keeps listening until the second tap.",
    "continuous": "Stays on until you switch it off. {continuous_delivery} For long writing - nothing to hold{continuous_suffix}.",
    "commands_only": "Hold this key and say a command. It is carried out, never typed. {command_help}{ava_detail}",
    "wake_word": "Say the phrase, then your command. No keys. A detector on this computer listens for the phrase - nothing is sent anywhere. Other voices, like a television, can set it off.",
    "hands_free": "{session_start} Say a thought, then '{paste_word}' to paste it. Say commands in between. Say '{exit_phrase}' to end it.",
}


def detail_from_config(mode_id, config, *, paste_word, help_phrase, exit_phrase):
    """Format the shared copy with live capture, catalog and vocabulary facts."""
    from samsara.constants import DEFAULT_SILENCE_TIMEOUT
    from samsara.speech_pace import effective_value
    pause = effective_value(config, 'silence_threshold', DEFAULT_SILENCE_TIMEOUT)
    # PLACEHOLDER COPY (378): conditional sentences also belong to this module.
    duration = 'about two seconds' if pause == DEFAULT_SILENCE_TIMEOUT else f'{pause:g} seconds'
    manual = config.get('continuous_commit_trigger', 'silence') == 'key'
    help_text = (f"The wording has to match a command - say '{help_phrase}' to see the ones for the app you're in."
                 if help_phrase else 'The wording has to match a command - open the Command Reference to see the list.')
    return MODE_DETAILS[mode_id].format(
        continuous_delivery=('Use the configured commit key to type what you said.' if manual else
                             f'Each time you pause for {duration}, what you said is typed.'),
        continuous_suffix='' if manual else ', nothing to say to paste',
        command_help=help_text,
        ava_detail=(' Ava can work out a command you worded differently.'
                    if (config.get('ava_command_session') or {}).get('interpret_misses', False) else ''),
        session_start=('One session you start once.' if (config.get('command_mode') or {}).get('mode', 'hold') == 'toggle'
                       else 'Hold the button to keep the session open; release it to end.'),
        paste_word=paste_word, exit_phrase=exit_phrase,
    )
