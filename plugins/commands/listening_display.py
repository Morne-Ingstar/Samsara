"""Find or reset the selected Compact / Full listening display."""
from samsara.plugin_commands import command


_objects = ("indicator", "listening indicator", "listening display", "display",
            "status bar", "bar", "widget", "little window",
            "Samsara's window", "microphone thing")
_targets = tuple(dict.fromkeys((*_objects, *(f"the {item}" for item in _objects))))
_find_verbs = ("where is", "where's", "find", "show me", "bring back", "I lost")
_reset_verbs = ("reset", "put back")
_find_aliases = [f"{verb} {target}" for verb in _find_verbs for target in _targets]
_reset_aliases = [f"{verb} {target}" for verb in _reset_verbs for target in _targets]
# Help discovery already owns some whole questions such as “where's the
# listening display”. Keep those routes intact and use the other exact forms.
from samsara.help_intents import PHRASES as _HELP_PHRASES
_find_aliases = [phrase for phrase in _find_aliases if phrase not in _HELP_PHRASES]
_reset_aliases = [phrase for phrase in _reset_aliases if phrase not in _HELP_PHRASES]
_find_aliases = [phrase for phrase in _find_aliases if phrase != "find the listening indicator"]
_reset_aliases = [phrase for phrase in _reset_aliases if phrase != "reset the listening indicator"]

# PLACEHOLDER COPY (1044): ordinary command-result wording.
_FOUND_REPLY = "{name} display: {where}."
_NOT_FOUND_REPLY = "The listening display could not be found right now."
_RESET_REPLY = "Listening display reset to the bottom left of your main screen."
_RESET_FAILED_REPLY = "The listening display could not be reset right now."


@command("find the listening indicator", aliases=_find_aliases, whole_utterance=True,
         pack="core", risk_class="safe", ai_visible=False)
def find_listening_display(app, remainder="", **kwargs):
    """Finds and briefly highlights the selected listening display."""
    from samsara.ui import qt_runtime
    from plugins.commands.core_utils import speak_if_available
    from samsara import listening_display
    from samsara.display_recovery import find, location

    def run():
        if find(app):
            name = "Full" if listening_display.run_display(app).full else "Compact"
            where = location(app, full=listening_display.run_display(app).full)
            speak_if_available(app, _FOUND_REPLY.format(name=name, where=where))
        else:
            speak_if_available(app, _NOT_FOUND_REPLY)

    qt_runtime.post(run)
    return True


@command("reset the listening indicator", aliases=_reset_aliases, whole_utterance=True,
         pack="core", risk_class="safe", ai_visible=False)
def reset_listening_display(app, remainder="", **kwargs):
    """Returns the selected display to its default bottom-left corner."""
    from samsara.ui import qt_runtime
    from plugins.commands.core_utils import speak_if_available
    from samsara.display_recovery import reset

    def run():
        ok = reset(app)
        speak_if_available(app, _RESET_REPLY if ok else _RESET_FAILED_REPLY)

    qt_runtime.post(run)
    return True
