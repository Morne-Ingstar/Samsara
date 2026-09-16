"""Queue 109 -- losing hands-free must be explained, recoverable and countable.

Three defects from Astra's review (astra_review/SAMSARA_REVIEW_2026-09-15.md),
all of which cost a user who cannot type their way back in:

  F6   the poll loop's fatal path had cleanup but no recovery. Production
       built WakeConsumer without on_fatal, so the whole notification was
       play_sound('error') and listening simply stopped.
  F12  Home derived hands-free READY from "enabled, not snoozed, microphone
       present" -- none of which is listening -- so a dead consumer could not
       even be explained on Home.
  F11  the outcome-ring writer appended tuples and the miss diagnostic read
       dicts, so the diagnostic could never fire.

Everything here drives PRODUCTION code. The fatal tests run the REAL
WakeConsumer._poll_loop against a real WakeConsumer with the real bound
DictationApp.on_fatal handler; the F11 tests push real DispatchOutcomes
through the real writer and read them with the real reader. That last pairing
is the assertion the existing suite could not make: tests/test_home_qt.py
injects Hint fixtures, which cannot see a schema mismatch and did not.

DictationApp methods are compiled out of dictation.py by AST (the harness
tests/test_wake_consumer_lifecycle.py established) rather than imported: the
app is normally running on this machine and importing dictation.py starts it.
"""
import ast
import collections
import logging
import sys
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import outcome_ring
from samsara import session_modes as sm
from samsara.audio_engine import wake_consumer as wake_module
from samsara.audio_engine.ring import EMPTY
from samsara.ui import home_signals


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _FakeTimers:
    """thread_registry.timer, recorded instead of started, so a retry
    schedule can be asserted and then fired on demand."""

    def __init__(self):
        self.scheduled = []

    def timer(self, name, delay, fn, args=(), kwargs=None, daemon=None):
        self.scheduled.append((name, delay, fn, tuple(args), dict(kwargs or {})))
        return Mock()

    def fire_last(self):
        _name, _delay, fn, args, kwargs = self.scheduled[-1]
        fn(*args, **kwargs)


def _load(names, timers):
    """Compile the named DictationApp methods only -- dictation.py's module
    body (which starts the app) never runs."""
    path = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    app_cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    methods = [n for n in app_cls.body
               if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods} == set(names), (
        f"missing from dictation.py: {set(names) - {n.name for n in methods}}")
    module = types.ModuleType('dictation_policy_109')
    module.__dict__.update(
        time=time, collections=collections,
        logger=logging.getLogger('dictation_policy_109'),
        outcome_ring=outcome_ring,
        flight_recorder=types.SimpleNamespace(record=lambda *a, **k: None),
        thread_registry=types.SimpleNamespace(spawn=Mock(), timer=timers.timer),
    )
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), 'exec'),
         module.__dict__)
    return type('AppPolicy', (), {n.name: module.__dict__[n.name] for n in methods})


APP_METHODS = (
    '_on_wake_consumer_fatal', '_retry_hands_free_after_fatal',
    'restart_hands_free', '_publish_hands_free_fault',
    '_show_outcome_chip', '_show_dispatch_outcome_chip',
)


class _App:
    """The duck-typed DictationApp the fatal path and Home actually touch.

    Carries the REAL bound methods; everything else is the minimum the
    production code reads. start_wake_word_mode is the app's own (it is a
    hundred lines of earcons, ducking and indicator work that this queue does
    not touch), so it is stubbed to do the one thing restart_hands_free
    depends on: arm detection and start the consumer.
    """

    def __init__(self, policy, *, consumer=None, wake_enabled=True):
        for name in APP_METHODS:
            setattr(self, name, getattr(policy, name).__get__(self, type(self)))
        self.config = {'wake_word_enabled': wake_enabled, 'microphone': 7,
                       'wake_word_config': {'phrase': 'jarvis'}}
        self.available_mics = [{'id': 7, 'name': 'USB mic'}]
        self.snoozed = False
        self.wake_word_active = True
        self._wake_start_pending = False
        self._wake_models_state = 'ready'
        self._wake_consumer = consumer
        self._wake_consumer_reasons = set()
        self._hands_free_fault = None
        self._hands_free_fault_attempts = 0
        self._outcome_ring = collections.deque(maxlen=8)
        self.sounds = []
        self.start_calls = 0
        self.start_succeeds = True
        self.listening_indicator = None
        # _handle_fatal writes these directly.
        self.app_state = 'asleep'
        self.wake_word_triggered = False
        self.command_mode_active = False
        self.ava_command_session_active = False

    # -- the app's own surface the compiled methods call ------------------
    def play_sound(self, name, **kw):
        self.sounds.append(name)

    def wake_ready_state(self) -> str:
        return self._wake_models_state

    def start_wake_word_mode(self):
        self.start_calls += 1
        if not self.start_succeeds:
            return
        self.wake_word_active = True
        self._wake_consumer_reasons.add('wake_word')
        if self._wake_consumer is not None:
            self._wake_consumer.start()

    def _ensure_wake_consumer(self, reason):
        self._wake_consumer_reasons.add(reason)
        if self._wake_consumer is not None and not self._wake_consumer.running:
            self._wake_consumer.start()

    def _publish_wake_state(self):
        return None

    def _schedule_ui(self, fn, *args):
        fn(*args)


class _FakeConsumer:
    """Only the lifecycle surface restart_hands_free and Home read."""

    def __init__(self, running=False):
        self._running = running
        self.starts = 0

    @property
    def running(self):
        return self._running

    def start(self):
        self.starts += 1
        self._running = True


@pytest.fixture
def timers():
    return _FakeTimers()


@pytest.fixture
def policy(timers):
    return _load(APP_METHODS, timers)


@pytest.fixture
def app(policy):
    return _App(policy, consumer=_FakeConsumer(running=True))


#: Real consumers built by a test, so a restart's real poll thread is always
#: joined -- a leaked poller would outlive the test and keep polling.
_LIVE_CONSUMERS = []


@pytest.fixture(autouse=True)
def _stop_live_consumers():
    yield
    while _LIVE_CONSUMERS:
        consumer = _LIVE_CONSUMERS.pop()
        try:
            consumer.stop()
        except Exception:
            pass


def _real_consumer(app):
    """A REAL WakeConsumer over a mock engine, wired to the app's real
    on_fatal -- the production construction from dictation.py:3024."""
    reader = Mock()
    reader.read_next = Mock(return_value=EMPTY)
    reader.snap_to_head = Mock()
    engine = Mock()
    engine.register_consumer = Mock(return_value=reader)
    consumer = wake_module.WakeConsumer(engine, app, on_fatal=app._on_wake_consumer_fatal)
    app._wake_consumer = consumer
    _LIVE_CONSUMERS.append(consumer)
    return consumer, reader


def _kill(consumer, reader, exc):
    """Make the REAL poll loop die of `exc`, synchronously.

    _handle_fatal sets the stop event and disarms wake detection on its way
    out, so a second fatal in one test has to re-arm what the first cleared --
    the same thing a restart does.
    """
    import threading
    reader.read_next = Mock(side_effect=exc)
    consumer._app.wake_word_active = True
    consumer._stop_event = threading.Event()
    consumer._running = True
    consumer._fatal_reported = False
    try:
        consumer._poll_loop(consumer._stop_event)
    finally:
        # The device is "back" once the loop has died: a restart in the same
        # test must be allowed to succeed rather than fatal again instantly.
        reader.read_next = Mock(return_value=EMPTY)


# ---------------------------------------------------------------------------
# A. Classification and the retry cap
# ---------------------------------------------------------------------------

class TestFatalClassification:
    def test_device_failures_are_transient(self):
        for exc in (OSError("device unavailable"),
                    TimeoutError("read timed out"),
                    IOError("stream closed")):
            assert wake_module.classify_fatal(exc) == wake_module.FATAL_TRANSIENT, exc

    def test_portaudio_errors_are_transient(self):
        sd = pytest.importorskip("sounddevice")
        error = getattr(sd, "PortAudioError", None)
        if error is None:
            pytest.skip("this sounddevice build has no PortAudioError")
        assert wake_module.classify_fatal(error("no device")) == wake_module.FATAL_TRANSIENT

    def test_programming_errors_are_permanent(self):
        """A retry would re-enter the same code with the same inputs and
        raise again on the very next frame."""
        for exc in (TypeError("NoneType is not subscriptable"),
                    AttributeError("'NoneType' has no attribute 'pcm'"),
                    KeyError("phrase"), IndexError("list index"),
                    ValueError("bad shape"), RuntimeError("no"),
                    ZeroDivisionError("nope"), NameError("x")):
            assert wake_module.classify_fatal(exc) == wake_module.FATAL_PERMANENT, exc

    def test_an_unrecognised_exception_is_permanent(self):
        """The safe direction: a wrong 'permanent' costs one manual restart,
        a wrong 'transient' costs a retry loop."""
        class OddError(Exception):
            pass

        assert wake_module.classify_fatal(OddError("?")) == wake_module.FATAL_PERMANENT

    def test_the_cap_is_the_schedule(self):
        assert wake_module.FATAL_RETRY_CAP == len(wake_module.FATAL_RETRY_DELAYS_S)
        assert wake_module.FATAL_RETRY_DELAYS_S == tuple(
            sorted(wake_module.FATAL_RETRY_DELAYS_S)), "the backoff must not shrink"


# ---------------------------------------------------------------------------
# B. F6 -- a fatal leaves a persistent, explained state, not a beep
# ---------------------------------------------------------------------------

class TestFatalLeavesRecoverableState:
    def test_a_real_fatal_produces_a_persistent_reason_and_a_restart_action(self, app):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("device unavailable"))

        fault = app._hands_free_fault
        assert fault is not None, "the fatal left nothing to explain it"
        assert fault.reason and fault.reason[0].isupper()
        assert "OSError" in fault.exception and "device unavailable" in fault.exception

        state = home_signals.hands_free_state(app)
        assert state.status == home_signals.UNAVAILABLE
        assert state.detail.startswith("Hands-free stopped")
        assert state.action == home_signals.ACTION_RESTART_HANDS_FREE
        assert state.action_label == home_signals.RESTART_HANDS_FREE_LABEL
        assert "OSError" in state.evidence

    def test_the_beep_is_no_longer_the_whole_notification(self, app):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("gone"))
        # The earcon still plays -- it is immediate feedback -- but it is now
        # accompanied by something that survives it.
        assert app.sounds == ['error']
        assert home_signals.problem_notice(app) is not None

    def test_the_explanation_does_not_expire(self, app):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, TypeError("frame is None"))
        first = home_signals.hands_free_state(app)
        for _ in range(5):                       # five Home refreshes later
            again = home_signals.hands_free_state(app)
        assert again == first
        assert again.status == home_signals.UNAVAILABLE

    def test_the_notice_carries_the_restart_action(self, app):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("gone"))
        notice = home_signals.problem_notice(app)
        assert notice.capability == home_signals.HANDS_FREE
        assert notice.action == home_signals.ACTION_RESTART_HANDS_FREE
        assert notice.action_label == home_signals.RESTART_HANDS_FREE_LABEL
        assert notice.consequence == "The wake word cannot hear you."

    def test_a_fault_outranks_a_snooze(self, app):
        """A stopped listener that is also snoozed still stopped. Saying
        'you paused listening' would blame the user for a crash."""
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("gone"))
        app.snoozed = True
        assert home_signals.hands_free_state(app).status == home_signals.UNAVAILABLE

    def test_hands_free_switched_off_is_still_not_a_fault(self, policy):
        off = _App(policy, consumer=_FakeConsumer(running=True), wake_enabled=False)
        consumer, reader = _real_consumer(off)
        _kill(consumer, reader, OSError("gone"))
        assert home_signals.hands_free_state(off).status == home_signals.OFF
        assert home_signals.problem_notice(off) is None


# ---------------------------------------------------------------------------
# C. The retry policy
# ---------------------------------------------------------------------------

class TestRetryPolicy:
    def test_a_transient_fatal_retries_within_its_cap(self, app, timers):
        consumer, reader = _real_consumer(app)
        delays = []
        for _ in range(wake_module.FATAL_RETRY_CAP):
            app._hands_free_fault = None          # a fresh fatal each round
            _kill(consumer, reader, OSError("device unavailable"))
            delays.append(timers.scheduled[-1][1])
        assert len(timers.scheduled) == wake_module.FATAL_RETRY_CAP
        assert delays == list(wake_module.FATAL_RETRY_DELAYS_S)

        # The cap holds: the next transient fatal schedules nothing at all,
        # and stops promising another go.
        _kill(consumer, reader, OSError("device unavailable"))
        assert len(timers.scheduled) == wake_module.FATAL_RETRY_CAP
        assert app._hands_free_fault.retrying is False
        assert home_signals.hands_free_state(app).status == home_signals.UNAVAILABLE

    def test_a_programming_error_does_not_retry_at_all(self, app, timers):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, TypeError("frame is None"))
        assert timers.scheduled == []
        assert app._hands_free_fault.classification == wake_module.FATAL_PERMANENT
        assert app._hands_free_fault.retrying is False
        assert app._hands_free_fault_attempts == 0

    def test_a_scheduled_retry_restarts_capture(self, app, timers):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("device unavailable"))
        assert consumer.running is False and app.wake_word_active is False

        app.start_succeeds = True
        timers.fire_last()
        assert app.start_calls == 1
        assert app.wake_word_active is True
        assert app._hands_free_fault is None
        assert home_signals.hands_free_state(app).status == home_signals.READY

    def test_a_retry_that_fails_leaves_the_fault_standing(self, app, timers):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("device unavailable"))
        app.start_succeeds = False
        timers.fire_last()
        assert app._hands_free_fault is not None
        assert app._hands_free_fault.retrying is True      # budget not spent
        assert home_signals.hands_free_state(app).status == home_signals.UNAVAILABLE

    def test_a_user_restart_resets_the_automatic_budget(self, app, timers):
        consumer, reader = _real_consumer(app)
        for _ in range(wake_module.FATAL_RETRY_CAP):
            app._hands_free_fault = None
            _kill(consumer, reader, OSError("device unavailable"))
        assert app._hands_free_fault_attempts == wake_module.FATAL_RETRY_CAP

        assert app.restart_hands_free() is True            # source='user'
        assert app._hands_free_fault_attempts == 0
        # A later fatal is allowed to try again by itself.
        before = len(timers.scheduled)
        _kill(consumer, reader, OSError("device unavailable"))
        assert len(timers.scheduled) == before + 1


# ---------------------------------------------------------------------------
# D. The restart action really restarts capture
# ---------------------------------------------------------------------------

class TestRestartAction:
    def test_it_starts_the_real_consumer_and_clears_the_fault(self, policy):
        target = _App(policy, consumer=None)
        consumer, reader = _real_consumer(target)
        _kill(consumer, reader, OSError("device unavailable"))
        assert consumer.running is False

        assert target.restart_hands_free() is True
        assert consumer.running is True, "the restart did not restart capture"
        assert consumer._thread is not None and consumer._thread.is_alive()
        assert target.wake_word_active is True
        assert target._hands_free_fault is None
        assert home_signals.hands_free_state(target).status == home_signals.READY
        consumer.stop()

    def test_a_restart_that_does_not_bring_listening_back_reports_false(self, app):
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("gone"))
        app.start_succeeds = False
        assert app.restart_hands_free() is False
        assert app._hands_free_fault is not None

    def test_the_home_helper_reports_what_the_app_reports(self, app, monkeypatch):
        from samsara.ui import home_qt
        consumer, reader = _real_consumer(app)
        _kill(consumer, reader, OSError("gone"))
        app.start_succeeds = False
        assert home_qt.restart_hands_free(app) is False
        app.start_succeeds = True
        assert home_qt.restart_hands_free(app) is True

    def test_the_home_helper_survives_an_app_that_raises(self, app):
        from samsara.ui import home_qt
        app.restart_hands_free = Mock(side_effect=RuntimeError("boom"))
        assert home_qt.restart_hands_free(app) is False


# ---------------------------------------------------------------------------
# E. F12 -- readiness comes from the lifecycle, not from config
# ---------------------------------------------------------------------------

class TestReadinessIsRuntime:
    def test_a_stopped_consumer_is_not_ready_even_with_a_microphone(self, app):
        """The exact F12 case: enabled, not snoozed, microphone present --
        and nothing listening."""
        app._wake_consumer = _FakeConsumer(running=False)
        state = home_signals.hands_free_state(app)
        assert state.status == home_signals.UNAVAILABLE
        assert state.detail == "Hands-free is not listening."
        assert state.action == home_signals.ACTION_RESTART_HANDS_FREE
        assert home_signals.mic_available(app) is True

    def test_detection_not_armed_is_not_ready(self, app):
        app.wake_word_active = False
        assert home_signals.hands_free_state(app).status == home_signals.UNAVAILABLE

    def test_ready_needs_models_consumer_and_arming_together(self, app):
        assert home_signals.hands_free_state(app).status == home_signals.READY
        for attr, value in (('_wake_models_state', 'off'),
                            ('wake_word_active', False)):
            restore = getattr(app, attr)
            setattr(app, attr, value)
            assert home_signals.hands_free_state(app).status != home_signals.READY
            setattr(app, attr, restore)

    def test_still_loading_is_unknown_not_ready_and_not_a_fault(self, app):
        app._wake_models_state = 'loading'
        state = home_signals.hands_free_state(app)
        assert state.status == home_signals.UNKNOWN
        assert not state.is_fault
        assert home_signals.problem_notice(app) is None

    def test_a_pending_start_is_unknown_not_a_fault(self, app):
        app.wake_word_active = False
        app._wake_start_pending = True
        assert home_signals.hands_free_state(app).status == home_signals.UNKNOWN

    def test_no_consumer_to_ask_is_unknown_not_ready(self, app):
        app._wake_consumer = None
        state = home_signals.hands_free_state(app)
        assert state.status == home_signals.UNKNOWN
        assert not state.is_fault

    def test_it_reads_the_apps_own_readiness_api(self, app):
        """wake_models_state consumes DictationApp.wake_ready_state -- the
        API the brief pointed at -- and adds none of its own."""
        app.wake_ready_state = lambda: 'loading'
        assert home_signals.wake_models_state(app) == 'loading'
        app.wake_ready_state = Mock(side_effect=RuntimeError("boom"))
        assert home_signals.wake_models_state(app) == 'ready'   # never breaks Home
        del app.wake_ready_state
        assert home_signals.wake_models_state(app) == 'ready'   # test doubles

    def test_a_missing_microphone_still_wins_over_not_listening(self, app):
        app.available_mics = []
        app._wake_consumer = _FakeConsumer(running=False)
        state = home_signals.hands_free_state(app)
        assert state.detail == "Microphone unavailable."
        assert state.action == ""        # no button reconnects a microphone


# ---------------------------------------------------------------------------
# F. F11 -- one schema, producer to consumer
# ---------------------------------------------------------------------------

class TestOutcomeSchema:
    """The assertion that was missing. tests/test_home_qt.py injects Hint
    fixtures; a Hint fixture cannot notice that the writer and the reader
    disagree about what an outcome IS."""

    def _misses(self, app, n=3):
        for _ in range(n):
            app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="command_miss"))

    def test_the_real_writers_misses_are_counted_by_the_real_reader(self, app):
        self._misses(app, 3)
        assert home_signals.recent_misses(app) == (3, 3), (
            "the producer and the consumer still disagree about the schema")

    def test_the_diagnostic_becomes_eligible_from_real_command_misses(self, app, monkeypatch):
        monkeypatch.setattr(home_signals, "corrections_waiting", lambda a: 0)
        monkeypatch.setattr(home_signals, "_diag_records", lambda a, limit: [])
        self._misses(app, home_signals.MISS_MIN)
        hints = [h for h in home_signals.diagnostic_hints(app) if h.id == "diag.misses"]
        assert hints, "repeated command failure still gets no guidance"
        assert hints[0].text == "3 of your last 3 commands were not recognised."
        assert "threshold 3" in hints[0].evidence

    def test_one_miss_short_of_the_threshold_says_nothing(self, app, monkeypatch):
        monkeypatch.setattr(home_signals, "corrections_waiting", lambda a: 0)
        monkeypatch.setattr(home_signals, "_diag_records", lambda a, limit: [])
        self._misses(app, home_signals.MISS_MIN - 1)
        assert [h for h in home_signals.diagnostic_hints(app) if h.id == "diag.misses"] == []

    def test_dictation_outcomes_are_not_counted_as_commands(self, app):
        """The denominator is commands. Three misses among three dictations
        is still three of three commands, not three of six outcomes."""
        for kind in ("dictate_committed", "dictate_staged", "dictate_draft_cleared"):
            app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind=kind))
        self._misses(app, 3)
        assert home_signals.recent_misses(app) == (3, 3)

    def test_a_commit_with_nothing_staged_is_not_a_command_miss(self, app):
        """The old reader counted dictate_commit_unavailable as a miss, which
        put a dictation outcome in a sentence about commands."""
        for _ in range(3):
            app._show_dispatch_outcome_chip(
                sm.DispatchOutcome(kind="dictate_commit_unavailable"))
        assert home_signals.recent_misses(app) == (0, 0)

    def test_a_command_that_ran_is_counted_but_is_not_a_miss(self, app):
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(
            kind="command_executed", detail={"phrase": "close this window"}))
        self._misses(app, 2)
        assert home_signals.recent_misses(app) == (2, 3)

    def test_an_accepted_but_unfinished_command_is_not_yet_an_outcome(self, app):
        """command_executed with state=queued is the 'working on it' chip;
        its real outcome arrives later and would be double-counted."""
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(
            kind="command_executed", detail={"phrase": "open notepad", "state": "queued"}))
        assert home_signals.recent_misses(app) == (0, 0)

    def test_a_pack_off_refusal_is_a_command_outcome_but_not_a_miss(self, app):
        """It was recognised perfectly well; its pack is switched off."""
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(
            kind="command_miss", detail={"pack": "media"}))
        assert home_signals.recent_misses(app) == (0, 1)

    def test_progress_chips_never_enter_the_ring(self, app):
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="dictate_staged"))
        app._show_outcome_chip("REC", "live", None)
        assert list(app._outcome_ring) == []

    def test_the_writer_writes_the_shared_record(self, app):
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="command_miss"))
        rec = app._outcome_ring[-1]
        assert isinstance(rec, outcome_ring.OutcomeRecord)
        assert (rec.label, rec.kind, rec.source) == ("MISS", "error", "command_miss")
        assert rec.at > 0

    def test_a_directly_raised_chip_still_classifies_from_its_label(self, app):
        """Plugins and device paths call _show_outcome_chip with no source;
        the label vocabulary is all they ever carried."""
        for _ in range(3):
            app._show_outcome_chip("MISS", "error")
        assert home_signals.recent_misses(app) == (3, 3)

    def test_the_positional_reader_is_unaffected(self, app):
        """home_qt.render_outcome unpacks the ring positionally. A NamedTuple
        is the one schema that is also the old tuple."""
        from samsara.ui import home_qt
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="command_miss"))
        rec = app._outcome_ring[-1]
        assert (rec[0], rec[1]) == ("MISS", "error")
        assert home_qt.classify_outcome(rec[0], rec[1]) == "miss"
        assert home_qt.render_outcome(app, rec)[0] == home_qt.KIND_MISSED
        assert home_qt.outcome_ring(app)[-1] is rec

    def test_the_old_shapes_are_still_readable(self, app):
        """A ring that survived a hot reload, or a hand-built test entry."""
        app._outcome_ring.extend([("MISS", "error", 1.0),
                                  {"label": "MISS", "kind": "error", "at": 2.0},
                                  outcome_ring.record("MISS", "error", 3.0, "command_miss")])
        assert home_signals.recent_misses(app) == (3, 3)

    def test_an_empty_ring_makes_no_claim(self, app):
        assert home_signals.recent_misses(app) == (0, 0)
        app._outcome_ring = None
        assert home_signals.recent_misses(app) == (0, 0)

    def test_the_window_counts_commands_not_chips(self, app):
        """An 8-entry ring holding six dictations and two commands describes
        two commands."""
        for _ in range(6):
            app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="dictate_committed"))
        self._misses(app, 2)
        assert home_signals.recent_misses(app) == (2, 2)


# ---------------------------------------------------------------------------
# G. The wiring itself -- production must not build the consumer bare
# ---------------------------------------------------------------------------

def test_production_constructs_the_consumer_with_on_fatal():
    """dictation.py:3024 built WakeConsumer(engine=..., app=self) and nothing
    else, which is the whole of F6. Read from the source so the wiring cannot
    be quietly removed again."""
    path = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'WakeConsumer']
    assert calls, "dictation.py no longer constructs a WakeConsumer"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert 'on_fatal' in kwargs, (
            "a WakeConsumer built without on_fatal falls back to one error beep")


def test_the_consumer_exposes_its_running_state():
    """Home has to be able to ask. Not a private flag read through the back."""
    assert isinstance(wake_module.WakeConsumer.running, property)
