"""Serialize duck ownership changes across the parent algorithm's RPC calls.

The transport serializes individual commands. A read, ownership update and
write must also be one transaction: another hold must not snapshot an old
hold's attenuation while its restore is in flight. Lifecycle locks separately
keep a reused SessionDucker from starting until its own stop has finished.
"""

from functools import wraps
import threading


_SESSION_TRANSACTION = threading.RLock()


def session_transaction(function):
    @wraps(function)
    def transaction(*args, **kwargs):
        with _SESSION_TRANSACTION:
            return function(*args, **kwargs)
    return transaction


def lifecycle_transaction(function):
    @wraps(function)
    def transaction(self, *args, **kwargs):
        with self._lifecycle_lock:
            return function(self, *args, **kwargs)
    return transaction


def rebase_user_volume(state, current, epsilon):
    """An external change supersedes every older lease's attenuation.

    Keep the lease IDs so their eventual releases still balance, but make
    their factors neutral. A later lease may duck the user's new baseline;
    releasing it returns to that baseline, even while older leases remain.
    Call only inside a session transaction and the shared bookkeeping lock.
    """
    expected = state.written_volume
    if expected is None:
        expected = state.applied_volume
    if abs(current - expected) <= epsilon:
        return False
    state.original_volume = current
    state.applied_volume = current
    state.written_volume = current
    state.lease_factors = dict.fromkeys(state.lease_factors, 1.0)
    return True
