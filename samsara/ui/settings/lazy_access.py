"""Materialize Settings editors only for explicit navigation or lookup."""
from functools import wraps
from contextlib import contextmanager
from copy import deepcopy
from threading import get_ident


class _SourceApp:
    """Read the visit's draft during construction without swapping live config."""
    def __init__(self, app, config):
        object.__setattr__(self, '_app', app)
        object.__setattr__(self, '_config', deepcopy(config))
        object.__setattr__(self, '_thread', get_ident())
        object.__setattr__(self, '_active', True)

    def __getattr__(self, name):
        if name == 'config' and self._active and get_ident() == self._thread:
            return self._config
        return getattr(self._app, name)

    def __setattr__(self, name, value):
        setattr(self._app, name, value)


@contextmanager
def source_config(owner):
    app = owner.app
    view = _SourceApp(app, owner._controls_at_open)
    owner.app = view
    try:
        yield
    finally:
        object.__setattr__(view, '_active', False)
        owner.app = app


def assembling(method):
    """Internal tree walks must only inspect the pages already being built."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self._assembly_depth = getattr(self, '_assembly_depth', 0) + 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._assembly_depth -= 1
    return wrapped


class LookupMap(dict):
    def __init__(self, owner, lookup, complete=None):
        super().__init__()
        self.owner, self.lookup, self.complete = owner, lookup, complete

    def _available(self):
        return not (self.owner._building_contents or self.owner._assembly_depth)

    def __missing__(self, key):
        if self._available():
            self.lookup(key)
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        raise KeyError(key)

    def __contains__(self, key):
        if not dict.__contains__(self, key) and self._available():
            self.lookup(key)
        return dict.__contains__(self, key)

    def get(self, key, default=None):
        if self._available():
            try:
                return self[key]
            except KeyError:
                return default
        return super().get(key, default)

    def values(self):
        if self.complete is not None and self._available():
            self.complete()
        return super().values()


class LookupRows(list):
    def __init__(self, owner, complete):
        super().__init__()
        self.owner, self.complete = owner, complete

    def __iter__(self):
        if not (self.owner._building_contents or self.owner._assembly_depth):
            self.complete()
        return super().__iter__()
