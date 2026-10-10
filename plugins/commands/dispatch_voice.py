"""Optional owner dispatch routing; packaged copies leave utterances to Ava."""
from importlib import import_module


def _local():
    try:
        return import_module("plugins.commands.local_dispatch_voice")
    except ModuleNotFoundError as exc:
        if exc.name != "plugins.commands.local_dispatch_voice":
            raise
        return None


def _forward(name, *args):
    local = _local()
    return getattr(local, name)(*args) if local is not None else False


def route_ava_utterance(app, text):
    return _forward("route_ava_utterance", app, text)


def is_dispatch_shaped(text):
    return _forward("is_dispatch_shaped", text)


def match_send(text):
    return _forward("match_send", text)


def awaiting_follow_up(app, text=""):
    return _forward("awaiting_follow_up", app, text)
