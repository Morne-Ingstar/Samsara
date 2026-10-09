"""One rule for speech about Ava command outcomes; unknown effects matter."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import inspect

# Only immediately visible/audible effects belong here. Everything else is important.
PERCEPTIBLE_COMMANDS = frozenset({
    "pause", "play", "resume", "next track", "previous track", "mute", "unmute",
    "volume up", "volume down", "set volume", "open", "open notepad", "open chrome",
    "open firefox", "open calculator", "open explorer", "open obsidian", "focus",
    "switch window", "bring", "scroll down", "scroll up", "new tab", "close tab",
    "minimize", "maximize", "minimize window", "maximize window", "restore window",
    "close window", "next tab", "previous tab", "show desktop", "show windows",
    "show commands", "show settings", "show home", "show numbers", "hide numbers",
    "refresh numbers", "command reference", "show quick reference", "stop music",
    "stop spotify", "stop playback",
})
_ACTION = ContextVar("ava_action_feedback", default=None)


def _handler(executor, command):
    lookup = getattr(executor, '_registry_entry', None)
    entry = lookup(command) if callable(lookup) else None
    handler = getattr(entry, 'handler', None)
    return entry, inspect.unwrap(handler) if callable(handler) else None


def is_perceptible(command, executor=None):
    entry, handler = _handler(executor, command)
    if getattr(entry, 'cmd_type', None) == 'launch':
        return True
    if getattr(handler, '__module__', '') == 'plugins.commands.music' and getattr(
            handler, '__qualname__', '').startswith(('_named_transport_handler.', '_named_volume_handler.')):
        return True
    phrase = str(command).lower()
    return phrase in PERCEPTIBLE_COMMANDS or phrase.startswith((
        "pause ", "resume ", "play ", "mute ", "unmute ", "open ", "launch ", "focus "))


def action_decision(level, outcome, *, perceptible=False, screen_reader=False):
    if screen_reader or level not in ("silent", "brief"):
        return "speak"
    if outcome == "completed_chain":
        return "sound" if level == "brief" else "skip"
    if outcome != "completed" or not perceptible:
        return "speak"
    return "skip"


@dataclass
class _Action:
    perceptible: bool
    failure_notices_only: bool = False
    state: str = 'completed'
    deferred: list = field(default_factory=list)
    replaying: bool = False
    announced: bool = False


@contextmanager
def action_scope(command, executor=None):
    _, handler = _handler(executor, command)
    action = _Action(is_perceptible(command, executor),
                     getattr(handler, '__module__', '') == 'plugins.commands.app_verbs')
    token = _ACTION.set(action)
    try:
        yield action
    except BaseException:
        action.state = 'failed'
        raise
    finally:
        try:
            if action.state != 'completed':
                action.replaying = True
                for callback in action.deferred:
                    callback()
        finally:
            _ACTION.reset(token)


def execute_action(executor, command, app, **kwargs):
    with action_scope(command, executor) as action:
        result = executor.execute_canonical(command, app, **kwargs)
        action.state = getattr(getattr(result, 'state', None), 'value', 'failed')
        if action.state in ('failed', 'rejected', 'miss') and not action.announced:
            detail = getattr(result, 'detail', {}) or {}
            speak_result(app, detail.get('message') or detail.get('reason') or 'I could not do that.',
                         {'state': action.state}, command)
        return result


def defer_speech(coordinator, text, category, **kwargs):
    action = _ACTION.get()
    if action is not None:
        action.announced = True
    level = (coordinator.app.config.get('tts', {}) or {}).get('feedback_level')
    if (action is None or action.replaying or not action.perceptible
            or level not in ('silent', 'brief')
            or category not in ('command', 'general', 'agent_response')
            or (action.failure_notices_only and category == 'agent_response')):
        return False
    action.deferred.append(lambda: coordinator.speak(text, category=category, **kwargs))
    return True


def scoped_decision(level, category):
    action = _ACTION.get()
    if action is None:
        return None
    outcome = 'completed' if category == 'command' and action.state == 'completed' else 'important'
    return action_decision(level, outcome, perceptible=action.perceptible)


def speak_result(app, label, detail, command=None):
    level = (getattr(app, "config", {}).get("tts", {}) or {}).get("feedback_level", "brief")
    outcome = detail.get("state")
    if outcome == "completed" and detail.get("chain_count"):
        outcome = "completed_chain"
    decision = action_decision(level, outcome, perceptible=is_perceptible(command, getattr(app, 'command_executor', None)))
    if decision in ("speak", "sound"):
        speak = getattr(app, "_speak_session_notice", None)
        if callable(speak):
            if decision == "sound":
                speak(label, category="command")  # Preserve the coordinator's Brief chain sound.
            else:
                with action_scope(command if outcome == "completed" else ""):
                    speak(label, category="command")
