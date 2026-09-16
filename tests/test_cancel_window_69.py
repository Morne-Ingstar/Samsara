"""Queue 69: make a wrong execution cheap instead of chasing a smarter gate.

A non-read command recognised inside the dictation lane waits behind a short
cancel window ("running <X>... say no") and runs when it elapses unless the
user cancels. Read commands run at once; destructive ones keep their yes/no
(queue 63); a window of 0 is today's behaviour exactly.

Never imports dictation (the app may be running from source); dictation.py's
wiring is pinned by source text. Timers are fakes fired by hand.
"""
import collections
import sys
import threading
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins.commands import ask_ollama  # noqa: E402
from samsara import execution_policy as ep, plugin_commands  # noqa: E402
from samsara.execution_policy import Invocation, NeedsConfirmation, Route  # noqa: E402
from samsara.session_modes import (  # noqa: E402
    HandsFreeCommandMatch, PendingTextPolicy, SessionMode, SessionModeManager,
    UtteranceSignals, is_mapped_outcome, outcome_chip,
)

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))


class FakeTimer:
    def __init__(self, seconds, fn, log):
        self.seconds, self.fn, self.cancelled = seconds, fn, False
        log.append(self)

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


@pytest.fixture
def timers():
    return []


@pytest.fixture(autouse=True)
def _clean():
    saved = dict(plugin_commands._REGISTRY)
    saved_modules = {k: dict(v) for k, v in plugin_commands._MODULE_ENTRIES.items()}
    ask_ollama.clear_pending_action()
    ep.clear_catalog_risk_cache()
    ep._speech.last_onset = ep._speech.last_dispatch_start = 0.0
    yield
    ask_ollama.clear_pending_action()
    ep.clear_catalog_risk_cache()
    ep._speech.last_onset = ep._speech.last_dispatch_start = 0.0
    plugin_commands._REGISTRY.clear()
    plugin_commands._REGISTRY.update(saved)
    plugin_commands._MODULE_ENTRIES.clear()
    plugin_commands._MODULE_ENTRIES.update(saved_modules)


def _register(phrase, calls, **kw):
    def handler(app, remainder):
        calls.append(phrase)
        return True
    handler.__module__ = "plugins.commands.fake69"
    handler.__name__ = "handle_" + phrase.replace(" ", "_")
    plugin_commands.command(phrase, **kw)(handler)


def _app(cancel_window_s=None, everyday=False, generation=0):
    a = types.SimpleNamespace()
    cm = {"cancel_window_all_commands": everyday}
    if cancel_window_s is not None:
        cm["cancel_window_s"] = cancel_window_s
    a.config = {"command_packs": {}, "command_mode": cm}
    a.audio_coordinator = Mock()
    a.command_mode_active = True
    a._ava_cmd_generation = generation
    a._ava_cmd_mode_lock = threading.Lock()
    a._ava_session_dispatch_queue = collections.deque(maxlen=3)
    a._ava_session_dispatch_lock = threading.Lock()
    a._ava_session_request_in_flight = False
    a._show_outcome_chip = Mock()
    a.command_executor = types.SimpleNamespace(commands={})
    return a


def _chips(app):
    return [c.args[0] for c in app._show_outcome_chip.call_args_list]


def _defer_fn(app, timers):
    """The same decision dictation.py's _stage_hands_free_cancel_window makes."""
    def defer(match, run):
        delay = ep.cancel_window_for(match.phrase, app, reserved=match.reserved)
        if delay <= 0:
            ep.flush_cancel_window()
            return 0.0
        inv = Invocation(match.phrase, route=Route.EXACT, generation=ep.current_generation(app),
                         source_text=match.dispatch_text)
        ep.stage_cancel_window(app, inv, match.phrase, delay, run, bind_foreground=False,
                               timer_factory=lambda s, f: FakeTimer(s, f, timers))
        return delay
    return defer


def _manager(app, timers, calls, *, probe, with_window=True, deferred=None, pasted=None):
    def dispatch(text):
        calls.append(text)
        return types.SimpleNamespace(matched=True, phrase=text, state="completed")
    kwargs = {}
    if with_window:
        kwargs = dict(
            pending_reply_fn=lambda t: ep.answer_cancel_window(app, t),
            cancel_window_fn=_defer_fn(app, timers),
            on_deferred_outcome=(deferred.append if deferred is not None else None),
        )
    manager = SessionModeManager(
        abort_phrases=["stop listening"],
        foreground_exe_resolver=lambda: "notepad.exe",
        foreground_hwnd_resolver=lambda: 111,
        inject_fn=(lambda text, check=None: (pasted.append(text) or True))
        if pasted is not None else (lambda text, check=None: True),
        remove_chars_fn=lambda n: None,
        command_dispatch_fn=dispatch,
        agent_dispatch_fn=lambda text, context: None,
        buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=probe,
        **kwargs,
    )
    manager.reset(initial_mode=SessionMode.DICTATE)
    return manager


def _probe(table):
    """table: normalized text -> (phrase, policy, reserved)."""
    def probe(text):
        norm = " ".join((text or "").lower().strip(" .!?").split())
        if norm in table:
            phrase, policy, reserved = table[norm]
            return HandsFreeCommandMatch(dispatch_text=norm, phrase=phrase,
                                         pending_policy=policy, reserved=reserved)
        return None
    return probe


PRESERVE, COMMIT = PendingTextPolicy.PRESERVE, PendingTextPolicy.COMMIT


def _say(manager, app, text):
    """One utterance through the session, with the edges dictation.py adds."""
    ep.note_utterance_start()
    outcome = manager.dispatch_utterance(text, GOOD)
    ep.after_utterance(app, outcome.kind)
    return outcome


# ---------------------------------------------------------------------------
# The policy decision
# ---------------------------------------------------------------------------

class TestWhoGetsAWindow:
    def test_risk_classes(self):
        calls = []
        _register("open panel", calls, risk_class="ui")
        _register("paste it", calls, risk_class="write")
        _register("what time", calls, risk_class="read")
        _register("wipe drive", calls, risk_class="destructive", reversible=False)
        _register("close tab", calls, risk_class="destructive", reversible=True)
        app = _app()
        assert ep.cancel_window_for("open panel", app) == ep.CANCEL_WINDOW_DEFAULT_S == 3.0
        assert ep.cancel_window_for("paste it", app) == 3.0
        assert ep.cancel_window_for("close tab", app) == 3.0          # undoable
        assert ep.cancel_window_for("what time", app) == 0.0          # read: immediate
        assert ep.cancel_window_for("wipe drive", app) == 0.0         # keeps yes/no
        assert ep.cancel_window_for("no such command", app) == 0.0    # unknown: keeps yes/no

    def test_everyday_words_only_when_extended(self):
        _register("submit it", [], risk_class="write")
        assert ep.cancel_window_for("submit it", _app(), reserved=True) == 0.0
        assert ep.cancel_window_for("submit it", _app(everyday=True), reserved=True) == 3.0

    @pytest.mark.parametrize("raw,expected", [
        (0, 0.0), ("0", 0.0), (1.5, 1.5), (9, ep.CANCEL_WINDOW_MAX_S),
        (-1, 3.0), ("garbage", 3.0), (None, 3.0), (float("nan"), 3.0),
    ])
    def test_setting_is_clamped_to_an_honest_value(self, raw, expected):
        app = _app()
        app.config["command_mode"]["cancel_window_s"] = raw
        assert ep.cancel_window_settings(app)[0] == expected

    def test_off_gives_no_window_for_anything(self):
        _register("open panel", [], risk_class="ui")
        app = _app(cancel_window_s=0, everyday=True)
        assert ep.cancel_window_for("open panel", app) == 0.0
        assert ep.cancel_window_for("open panel", app, reserved=True) == 0.0


# ---------------------------------------------------------------------------
# Brief tests, through the session manager
# ---------------------------------------------------------------------------

class TestDictationLane:
    def test_non_read_command_opens_the_window_and_runs_after_it_elapses(self, timers):
        _register("open panel", [], risk_class="ui")
        app, calls, deferred = _app(), [], []
        m = _manager(app, timers, calls, deferred=deferred,
                     probe=_probe({"open panel": ("open panel", PRESERVE, False)}))

        outcome = _say(m, app, "Open panel.")
        assert outcome.kind == "command_cancel_window"
        assert outcome.detail["delay_s"] == 3.0
        assert outcome_chip(outcome.kind, outcome.detail) == ("running open panel" + chr(0x2026) + " say no", "pending")
        assert calls == []                                   # nothing ran yet
        assert isinstance(ep.pending_operation(), ep.TimedOperation)
        assert len(timers) == 1 and timers[0].seconds == 3.0

        timers[0].fire()
        assert calls == ["open panel"]
        assert [o.kind for o in deferred] == ["hands_free_command_executed"]
        assert ep.pending_operation() is None

    @pytest.mark.parametrize("word", ["No.", "no", "Cancel", "stop", "Nope!", "don't"])
    def test_cancel_inside_the_window_prevents_execution_and_is_logged(self, timers, caplog, word):
        _register("open panel", [], risk_class="ui")
        app, calls, deferred = _app(), [], []
        m = _manager(app, timers, calls, deferred=deferred,
                     probe=_probe({"open panel": ("open panel", PRESERVE, False)}))
        _say(m, app, "open panel")
        with caplog.at_level("INFO", logger=ep.logger.name):
            outcome = _say(m, app, word)
        assert outcome.kind == "pending_reply" and outcome.detail == {"answer": "rejected"}
        assert outcome_chip(outcome.kind, outcome.detail) == ("cancelled", "warning")
        timers[0].fire()                                     # late timer: disarmed
        assert calls == [] and deferred == []
        assert ep.pending_operation() is None
        assert "cancel window open panel cancelled: you said no" in caplog.text
        assert "cancelled open panel: you said no" in _chips(app)

    def test_cancel_leaves_the_staged_draft_unpasted(self, timers):
        _register("send it", [], risk_class="write")
        app, calls, pasted = _app(), [], []
        m = _manager(app, timers, calls, pasted=pasted,
                     probe=_probe({"send it": ("send it", COMMIT, False)}))
        _say(m, app, "hello there")
        assert m.dictate_pending_buffer
        assert _say(m, app, "send it").kind == "command_cancel_window"
        assert pasted == []                                  # the paste waits with the command
        _say(m, app, "no")
        assert pasted == [] and calls == [] and m.dictate_pending_buffer

    def test_read_command_is_unaffected(self, timers):
        _register("what time", [], risk_class="read")
        app, calls = _app(), []
        m = _manager(app, timers, calls, probe=_probe({"what time": ("what time", PRESERVE, False)}))
        outcome = _say(m, app, "what time")
        assert outcome.kind == "hands_free_command_executed"
        assert calls == ["what time"] and timers == [] and ep.pending_operation() is None

    def test_destructive_command_still_requires_yes_no(self, timers):
        _register("wipe drive", [], risk_class="destructive", reversible=False)
        app = _app()
        assert ep.cancel_window_for("wipe drive", app) == 0.0
        decision = ep.authorize(Invocation("wipe drive", route=Route.EXACT, generation=0), app=app)
        assert isinstance(decision, NeedsConfirmation)

        calls = []

        def dispatch(text):
            calls.append(text)
            return types.SimpleNamespace(matched=True, phrase=text, state="queued",
                                         awaiting_confirmation=True)
        m = _manager(app, timers, [], probe=_probe({"wipe drive": ("wipe drive", PRESERVE, False)}))
        m._command_dispatch_fn = dispatch
        outcome = _say(m, app, "wipe drive")
        assert outcome.kind == "command_awaiting_confirmation"   # the 58/63 yes/no path
        assert timers == [] and calls == ["wipe drive"]

    def test_disabled_reproduces_todays_behaviour_exactly(self, timers):
        _register("open panel", [], risk_class="ui")
        _register("what time", [], risk_class="read")
        table = {"open panel": ("open panel", PRESERVE, False),
                 "what time": ("what time", PRESERVE, False),
                 "submit": ("submit", COMMIT, True)}
        script = ["hello there", "open panel", "no", "what time", "cancel", "more words", "submit", "stop"]

        def run(with_window):
            app, calls, pasted = _app(cancel_window_s=0, everyday=True), [], []
            m = _manager(app, timers, calls, probe=_probe(table), with_window=with_window, pasted=pasted)
            kinds = [_say(m, app, t).kind for t in script]
            return kinds, calls, pasted, m.dictate_pending_buffer, _chips(app)

        today = run(with_window=False)
        off = run(with_window=True)
        assert off == today
        assert timers == []
        assert "command_cancel_window" not in off[0]

    def test_curated_everyday_words_are_immediate_by_default(self, timers):
        _register("submit", [], risk_class="write")
        app, calls = _app(), []
        m = _manager(app, timers, calls, probe=_probe({"submit": ("submit", PRESERVE, True)}))
        assert _say(m, app, "submit").kind == "hands_free_command_executed"
        assert calls == ["submit"] and timers == []


# ---------------------------------------------------------------------------
# Holding, answering, flushing
# ---------------------------------------------------------------------------

class TestWindowLifecycle:
    def _open(self, timers, *, deferred=None):
        _register("open panel", [], risk_class="ui")
        app, calls = _app(), []
        m = _manager(app, timers, calls, deferred=deferred,
                     probe=_probe({"open panel": ("open panel", PRESERVE, False),
                                   "close panel": ("close panel", PRESERVE, False)}))
        _say(m, app, "open panel")
        return app, calls, m

    def test_speech_inside_the_window_holds_it_and_more_dictation_cancels(self, timers):
        app, calls, m = self._open(timers)
        ep.note_speech_onset(app)
        op = ep.pending_operation()
        assert op.held
        timers[0].fire()                                     # the original window: disarmed
        assert calls == []
        outcome = _say(m, app, "and then we went to the shops")
        assert outcome.kind == "dictate_staged"
        assert calls == [] and ep.pending_operation() is None
        assert "cancelled open panel: you kept talking" in _chips(app)

    def test_held_window_runs_when_the_utterance_was_not_dictation(self, timers):
        app, calls, m = self._open(timers)
        ep.note_speech_onset(app)
        assert ep.after_utterance(None, "dropped_hallucination_segments") == "ran"
        assert calls == ["open panel"]

    def test_held_window_with_no_answer_is_cancelled(self, timers):
        app, calls, m = self._open(timers)
        ep.note_speech_onset(app)
        hold_timer = timers[-1]
        assert hold_timer.seconds == ep.CANCEL_HOLD_MAX_S
        hold_timer.fire()
        assert calls == [] and ep.pending_operation() is None

    def test_speech_already_under_way_when_staged_holds_at_once(self, timers):
        _register("open panel", [], risk_class="ui")
        app, calls = _app(), []
        m = _manager(app, timers, calls, probe=_probe({"open panel": ("open panel", PRESERVE, False)}))
        ep.note_utterance_start()
        ep.note_speech_onset(app)                            # next utterance began mid-decode
        m.dispatch_utterance("open panel", GOOD)
        assert ep.pending_operation().held

    def test_yes_runs_now(self, timers):
        deferred = []
        app, calls, m = self._open(timers, deferred=deferred)
        outcome = _say(m, app, "yes")
        assert outcome.detail == {"answer": "approved"}
        assert calls == ["open panel"] and [o.kind for o in deferred] == ["hands_free_command_executed"]

    def test_wait_turns_it_into_a_yes_no_question(self, timers):
        app, calls, m = self._open(timers)
        assert _say(m, app, "wait").detail == {"answer": "extended"}
        assert "open panel? yes or no" in _chips(app)
        assert ep.pending_operation().converted
        timers[0].fire()
        assert calls == []                                   # the clock stopped
        assert _say(m, app, "yes").detail == {"answer": "approved"}
        assert calls == ["open panel"]

    def test_next_command_flushes_the_open_window_in_order(self, timers):
        app, calls, m = self._open(timers)
        _register("close panel", [], risk_class="ui")
        assert _say(m, app, "close panel").kind == "command_cancel_window"
        assert calls == ["open panel"]                       # the first ran, not superseded
        timers[-1].fire()
        assert calls == ["open panel", "close panel"]

    def test_an_immediate_command_runs_after_the_open_window(self, timers):
        app, calls, m = self._open(timers)
        _register("what time", [], risk_class="read")
        m._hands_free_command_probe_fn = _probe({"what time": ("what time", PRESERVE, False)})
        assert _say(m, app, "what time").kind == "hands_free_command_executed"
        assert calls == ["open panel", "what time"]
        assert ep.pending_operation() is None

    def test_stop_generation_refuses_the_run(self, timers):
        app, calls, m = self._open(timers)
        app._ava_cmd_generation += 1                          # execution stop bumped it
        timers[0].fire()
        assert calls == []
        assert any("stale" in c for c in _chips(app))

    def test_session_end_refuses_the_run(self, timers):
        deferred = []
        app, calls, m = self._open(timers, deferred=deferred)
        m.reset(initial_mode=SessionMode.DICTATE)
        timers[0].fire()
        assert calls == []
        assert [o.kind for o in deferred] == ["hands_free_command_refused"]

    def test_focus_change_refuses_the_run(self, timers):
        _register("open panel", [], risk_class="ui")
        app, calls = _app(), []
        target = {"hwnd": 1}
        ep.stage_cancel_window(app, Invocation("open panel", route=Route.EXACT, generation=0),
                               "open panel", 3.0, lambda: calls.append("ran"),
                               bind_foreground=False, timer_factory=lambda s, f: FakeTimer(s, f, timers))
        op = ep.pending_operation()
        op.targets, op._target_probe = {"hwnd": 1}, lambda: dict(target)
        target["hwnd"] = 2
        timers[0].fire()
        assert calls == [] and op.cancel_reason == "target changed"

    def test_cancel_word_with_nothing_open_is_not_consumed(self):
        assert ep.answer_cancel_window(_app(), "no") is None
        assert ep.answer_cancel_window(_app(), "stop") is None

    def test_cancel_word_inside_a_sentence_is_not_a_cancel(self, timers):
        app, calls, m = self._open(timers)
        assert ep.answer_cancel_window(app, "no way that happened") is None
        assert ep.answer_cancel_window(app, '"no"') is None


# ---------------------------------------------------------------------------
# Ambiguity: numbered alternatives on the chip
# ---------------------------------------------------------------------------

class TestChoice:
    def _stage(self, timers, ran):
        app = _app()
        options = [("focus Chrome", lambda: ran.append(1)), ("focus Claude", lambda: ran.append(2))]
        ep.stage_choice(app, Invocation("choice", route=Route.EXACT, generation=0), options,
                        timer_factory=lambda s, f: FakeTimer(s, f, timers))
        return app

    def test_chip_lists_numbered_options(self, timers):
        app = self._stage(timers, [])
        assert _chips(app)[-1] == "1 focus Chrome " + chr(0xB7) + " 2 focus Claude -- say a number"

    @pytest.mark.parametrize("said,expected", [("two", [2]), ("2", [2]), ("Number one.", [1]), ("to", [2])])
    def test_a_number_picks(self, timers, said, expected):
        ran = []
        app = self._stage(timers, ran)
        assert ep.answer_cancel_window(app, said) == "approved"
        assert ran == expected and ep.pending_operation() is None

    def test_out_of_range_and_cancel_and_timeout(self, timers):
        ran = []
        app = self._stage(timers, ran)
        assert ep.answer_cancel_window(app, "seven") == "refused:not an option"
        assert ep.answer_cancel_window(app, "cancel") == "rejected"
        assert ran == [] and ep.pending_operation() is None
        app = self._stage(timers, ran)
        timers[-1].fire()
        assert ran == [] and ep.pending_operation() is None

    def test_needs_two_to_nine_options(self):
        with pytest.raises(ValueError):
            ep.stage_choice(_app(), Invocation("x", generation=0), [("only", lambda: None)])


# ---------------------------------------------------------------------------
# Wiring pins
# ---------------------------------------------------------------------------

def test_outcome_kind_is_mapped():
    assert is_mapped_outcome("command_cancel_window")
    assert outcome_chip("command_cancel_window", {})[1] == "pending"


def test_dictation_wires_the_window_into_the_lane():
    src = (ROOT / "dictation.py").read_text(encoding="utf-8")
    assert "pending_reply_fn=self._answer_cancel_window" in src
    assert "cancel_window_fn=self._stage_hands_free_cancel_window" in src
    assert "on_deferred_outcome=self._handle_deferred_session_outcome" in src
    assert "_policy.cancel_window_for(match.phrase, self, reserved=" in src
    assert "_policy.flush_cancel_window()" in src
    assert "_cancel_windows.note_speech_onset(self)" in src
    assert "_cancel_windows.note_utterance_start()" in src
    assert "_cancel_windows.after_utterance(self, outcome.kind)" in src
    assert "reserved=reserved," in src


def test_settings_control_round_trips(qapp):
    from tests.test_modes_config_keys_62 import _reload, _win
    app, win, save = _win({"command_mode": {}})
    assert win._widgets['cmd_cancel_window'].value() == 3.0
    assert win._widgets['cmd_cancel_window'].specialValueText() == "Off"
    assert win._widgets['cmd_cancel_window_all'].isChecked() is False
    cm = save({})['command_mode']
    assert cm['cancel_window_s'] == 3.0 and cm['cancel_window_all_commands'] is False

    win._widgets['cmd_cancel_window'].setValue(0.0)
    win._widgets['cmd_cancel_window_all'].setChecked(True)
    win._apply_and_close()
    assert app.config['command_mode']['cancel_window_s'] == 0.0
    assert app.config['command_mode']['cancel_window_all_commands'] is True
    assert ep.cancel_window_settings(app) == (0.0, True)
    win2, _ = _reload(app)
    assert win2._widgets['cmd_cancel_window'].value() == 0.0
    assert win2._widgets['cmd_cancel_window'].text().strip() == "Off"
    assert win2._widgets['cmd_cancel_window_all'].isChecked() is True
