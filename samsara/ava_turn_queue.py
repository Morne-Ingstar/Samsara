"""536: one ordered Ava conversation, with owned waiting feedback."""
from collections import deque
from contextlib import contextmanager
import copy
from functools import wraps
import threading
import time

from samsara import execution_policy
from samsara.ava_memory import AvaMemory
from samsara.live_surface.feedback import Feedback, FeedbackKind, FeedbackPurpose
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)
_creation_lock = threading.Lock()
_request = threading.local()
# STAND-IN COPY (536)
LOADING_LINE = "The local model is still loading."


def ownership_lock(app):
    """Completion and cancellation share this lock, never the transport lock."""
    with _creation_lock:
        lock = getattr(app, "_ava_completion_lock", None)
        if lock is None:
            lock = app._ava_completion_lock = threading.RLock()
        return lock


class _Completion:
    def __init__(self, app, callback, generation):
        self.app, self.callback, self.generation = app, callback, generation
        self.epoch = getattr(app, "_ava_completion_epoch", 0)
        self.done = False
        self.session = (getattr(callback, "__self__", None) is app and
                        getattr(callback, "__name__", "") == "_on_ava_session_request_done")
        if self.session:
            app._ava_dispatch_owner = self

    def current(self):
        return (not self.done and self.epoch == getattr(self.app, "_ava_completion_epoch", 0) and
                execution_policy.is_current(self.app, self.generation) and
                (not self.session or getattr(self.app, "_ava_dispatch_owner", None) is self))

    def __call__(self):
        with ownership_lock(self.app):
            if self.done:
                return
            current = self.current()
            self.done = True
            # Generic finish notifications (e.g. Event.set) still release a
            # waiter. Only the app's stateful completion owns dispatch state.
            if self.callback is not None and (not self.session or current):
                self.callback()
            elif self.session:
                _retired_done(self.app, self.callback, self)


def _retired_done(app, callback, owner=None):
    """Release a stale handoff without the app callback's receipt or drain."""
    with ownership_lock(app):
        session = (getattr(callback, "__self__", None) is app and
                   getattr(callback, "__name__", "") == "_on_ava_session_request_done")
        if not session:
            if callback is not None:
                callback()
            return
        dispatch_owner = getattr(app, "_ava_dispatch_owner", None)
        if dispatch_owner is not None and dispatch_owner is not owner:
            return
        lock = getattr(app, "_ava_session_dispatch_lock", None)
        with lock if lock is not None else ownership_lock(app):
            app._ava_session_request_in_flight = False


@contextmanager
def _completion_context(owner):
    previous = getattr(_request, "completion", None)
    _request.completion = owner
    try:
        yield
    finally:
        _request.completion = previous


def own_turn(fn):
    """Bind even synchronous exits to the app dispatch request they complete."""
    @wraps(fn)
    def call(app, remainder="", on_done=None, generation=None, **kwargs):
        generation = execution_policy.current_generation(app) if generation is None else generation
        with ownership_lock(app):
            if not execution_policy.is_current(app, generation):
                _retired_done(app, on_done)
                return
            owner = _Completion(app, on_done, generation)
        with _completion_context(owner):
            try:
                return fn(app, remainder=remainder, on_done=owner, generation=generation, **kwargs)
            except Exception:
                # Scheduling/local dispatch failures still release a current request.
                owner()
                raise
    return call


def set_outcome(app, chip):
    with ownership_lock(app):
        if can_deliver(app):
            app._ava_turn_outcome = chip


def retire_dispatch(app):
    """Invalidate callbacks before allowing the real app dispatcher to restart."""
    with ownership_lock(app):
        app._ava_completion_epoch = getattr(app, "_ava_completion_epoch", 0) + 1
        app._ava_dispatch_owner = None
        lock = getattr(app, "_ava_session_dispatch_lock", None)
        with lock if lock is not None else ownership_lock(app):
            queue = getattr(app, "_ava_session_dispatch_queue", None)
            if queue is not None:
                queue.clear()
            app._ava_session_request_in_flight = False
            app._ava_turn_outcome = None
        epoch = app._ava_completion_epoch
        def clear_retired():
            with ownership_lock(app):
                if epoch != app._ava_completion_epoch or has_live(app):
                    return
                indicator = getattr(app, "listening_indicator", None)
                if indicator is not None and hasattr(indicator, "clear_feedback"):
                    indicator.clear_feedback("ava")
        schedule = getattr(app, "_schedule_ui", None)
        try:
            schedule(clear_retired) if callable(schedule) else clear_retired()
        except Exception as exc:
            logger.debug("[AVA-WAIT] retired feedback unavailable: %s", type(exc).__name__)


class _Lane:
    def __init__(self):
        self.lock = threading.RLock()
        self.model_lock = threading.RLock()
        self.queue = deque()
        self.jobs = []
        self.running = False
        self.indicator = None


def _lane(app):
    with _creation_lock:
        lane = getattr(app, "_ava_conversation_lane", None)
        if not isinstance(lane, _Lane):
            lane = app._ava_conversation_lane = _Lane()
        return lane


def _alive(app, job):
    owner = job.get("completion")
    return (not job["done"] and not job["cancelled"] and
            (owner is None or owner.current()) and execution_policy.is_current(app, job["generation"]))


def has_live(app):
    lane = _lane(app)
    with lane.lock:
        return any(_alive(app, job) for job in lane.jobs)


def _feedback(app, lane=None):
    lane = lane or _lane(app)
    def update():
        with lane.lock:
            live = [job for job in lane.jobs if _alive(app, job)]
            active = bool(live)
            loading = any(job["loading"] for job in live)
            indicator = getattr(app, "listening_indicator", None)
            if getattr(app, "_ava_conversation_lane", None) is lane:
                app.ava_thinking_since = live[0]["started"] if live else None
            if indicator is None:
                return
            if active and lane.indicator is None:
                # A different producer's wait keeps its existing state/label.
                if getattr(indicator, "_thinking", False) is True:
                    return
                agent = getattr(indicator, "_agent_waiting", None)
                if isinstance(agent, str) and agent:
                    return
                controller = getattr(indicator, "controller", None)
                if controller is not None and hasattr(controller, "view"):
                    owner = controller.view().notice.owner
                    if isinstance(owner, str) and owner not in ("", "ava"):
                        return
                lane.indicator = indicator
                indicator.set_thinking(True, label="Ava", owner=lane)
                indicator._ava_wait_owner = lane
            if lane.indicator is not indicator or getattr(indicator, "_ava_wait_owner", None) is not lane:
                if lane.indicator is indicator:
                    # Another producer took the sign over: drop the claim so the next Ava turn raises it again.
                    lane.indicator = None
                return
            if active and loading:
                feedback = Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING,
                                    "ava", alive=lambda: has_live(app))
                indicator.show_feedback(LOADING_LINE, "accent", None, feedback)
            elif not active:
                lane.indicator = None
                indicator._ava_wait_owner = None
                indicator.set_thinking(False, label="Ava", owner=lane)
                if hasattr(indicator, "clear_feedback"):
                    indicator.clear_feedback("ava")
    schedule = getattr(app, "_schedule_ui", None)
    try:
        schedule(update) if callable(schedule) else update()
    except Exception as exc:
        logger.debug("[AVA-WAIT] feedback unavailable: %s", type(exc).__name__)


def _loading(app, job):
    with _lane(app).lock:
        if not _alive(app, job) or job["loading"]:
            return
        if (getattr(app, "config", {}).get("ava", {}) or {}).get("provider_policy") != "local":
            return
        job["loading"] = True
    _feedback(app)


def submit(app, worker, generation, on_skip=None):
    """Queue before spawning; a later utterance cannot overtake this one."""
    with ownership_lock(app):
        if not can_deliver(app) or not execution_policy.is_current(app, generation):
            if on_skip is not None:
                on_skip()
            return
        return _submit(app, worker, generation, on_skip)


def _submit(app, worker, generation, on_skip):
    lane = _lane(app)
    job = dict(worker=worker, generation=generation, on_skip=on_skip,
               started=time.monotonic(), done=False, cancelled=False,
               loading=False, cue=False, timers=[])
    job["completion"] = getattr(_request, "completion", None)
    with lane.lock:
        lane.queue.append(job)
        lane.jobs.append(job)
        start = not lane.running
        lane.running = True
    _feedback(app)
    launched = False
    try:
        for name, delay, callback in (("ava-wait-cue", 1.0, _cue),
                                      ("ava-local-loading", 8.0, _loading)):
            job["timers"].append(thread_registry.timer(
                name, delay, lambda callback=callback: callback(app, job), daemon=True))
        if start:
            thread_registry.spawn("ask_ollama._worker", lambda: _drain(app, lane), daemon=True)
        launched = True
    finally:
        if not launched:
            # Release this request normally; cancellation would invalidate its
            # completion and strand the app's existing in-flight flag.
            with lane.lock:
                if job in lane.queue:
                    lane.queue.remove(job)
                if job in lane.jobs:
                    lane.jobs.remove(job)
                job["cancelled"] = job["done"] = True
                lane.running = bool(lane.jobs)
                for timer in job["timers"]:
                    timer.cancel()
                if start:
                    # A failing spawn can already have started its thread.
                    # Its transport must not own the replacement lane.
                    with _creation_lock:
                        if getattr(app, "_ava_conversation_lane", None) is lane:
                            app._ava_conversation_lane = _Lane()
                            app.ava_thinking_since = None
            _feedback(app, lane)
            if on_skip is not None:
                on_skip()


def _cue(app, job):
    with _lane(app).lock:
        if not _alive(app, job) or job["cue"]:
            return
        job["cue"] = True
        level = (getattr(app, "config", {}).get("tts", {}) or {}).get("feedback_level", "brief")
    if level != "silent" and hasattr(app, "play_sound"):
        try:
            app.play_sound("ava_thinking")
        except Exception as exc:
            logger.debug("[AVA-WAIT] cue unavailable: %s", type(exc).__name__)


def _drain(app, lane):
    while True:
        with lane.lock:
            if not lane.queue:
                lane.running = False
                return
            job = lane.queue.popleft()
        try:
            if _alive(app, job):
                _request.generation = job["generation"]
                _request.lane, _request.job = lane, job
                try:
                    with _completion_context(job["completion"]):
                        job["worker"]()
                finally:
                    _request.generation = None
                    _request.lane = _request.job = None
            elif job["on_skip"] is not None:
                job["on_skip"]()
        except Exception as exc:
            logger.debug("[AVA-QUEUE] worker failed: %s", type(exc).__name__)
        finally:
            with lane.lock:
                job["done"] = True
                if job in lane.jobs:
                    lane.jobs.remove(job)
                for timer in job["timers"]:
                    timer.cancel()
            _feedback(app, lane)


def cancel(app):
    """Detach abandoned I/O so a new question never waits behind it."""
    with ownership_lock(app):
        retire_dispatch(app)
        _cancel_lane(app)


def _cancel_lane(app):
    lane = getattr(app, "_ava_conversation_lane", None)
    if lane is None:
        return
    with lane.lock:
        for job in lane.jobs:
            job["cancelled"] = True
            for timer in job["timers"]:
                timer.cancel()
        skipped = list(lane.queue)
        lane.queue.clear()
        for job in skipped:
            job["done"] = True
            lane.jobs.remove(job)
        with _creation_lock:
            if getattr(app, "_ava_conversation_lane", None) is lane:
                app._ava_conversation_lane = _Lane()
                app.ava_thinking_since = None
    _feedback(app, lane)
    for job in skipped:
        if job["on_skip"] is not None:
            job["on_skip"]()


def serialize_model(fn):
    """Protect prompt assembly, shared memory and transport as one turn.

    Each request edits a private history until its generation is validated.
    A cancelled HTTP call can finish without corrupting a newer conversation.
    """
    @wraps(fn)
    def call(prompt, app, *args, **kwargs):
        lane = getattr(_request, "lane", None) or _lane(app)
        with lane.model_lock:
            previous = getattr(_request, "memory", None)
            previous_generation = getattr(_request, "generation", None)
            if previous_generation is None:
                _request.generation = execution_policy.capture_generation(app)
            _request.memory = {}
            try:
                result = fn(prompt, app, *args, **kwargs)
                with ownership_lock(app):
                    if can_deliver(app):
                        for original, working in _request.memory.values():
                            with original._lock:
                                original._history = list(working._history)
                            original.save()
                return result
            finally:
                _request.memory = previous
                _request.generation = previous_generation
    return call


def memory_for(original):
    """Keep in-flight user/assistant turns out of the committed history."""
    memories = getattr(_request, "memory", None)
    if memories is None or not isinstance(original, AvaMemory):
        return original
    if id(original) not in memories:
        working = copy.copy(original)
        with original._lock:
            working._history = list(original._history)
            working._actions = list(original._actions)
        working._lock = threading.Lock()
        working._persist_path = None
        memories[id(original)] = original, working
    return memories[id(original)][1]


def transport(send, *args, **kwargs):
    """The timeout belongs to one HTTP request, never a multi-post search."""
    return send(*args, **kwargs)


def cloud_send(send, system, prompt, app, **kwargs):
    # HEAD's ordinary cloud send uses its existing 30s default/signature.
    return send(system, prompt, app, **kwargs)


@contextmanager
def before_deadline(app, deadline):
    lock = _lane(app).model_lock
    remaining = min(threading.TIMEOUT_MAX, max(0.0, deadline - time.monotonic()))
    acquired = lock.acquire(timeout=remaining)
    try:
        yield acquired and time.monotonic() < deadline
    finally:
        if acquired:
            lock.release()


def can_deliver(app):
    generation = getattr(_request, "generation", None)
    job = getattr(_request, "job", None)
    owner = getattr(_request, "completion", None)
    return ((owner is None or owner.current()) and (job is None or not job["cancelled"])
            and execution_policy.is_current(app, generation))
