"""Queue 126: three ways an effect used to happen without being authorized.

Each was reproduced with a real call before it was fixed; the reproductions
are in the queue 126 report's artifacts. This file is the gate that keeps
them shut.

  A. Effects that ran BEFORE authorize, off a SUBSTRING match. A dictated
     paragraph containing "command mode off" turned command matching off and
     wrote config.json; one containing "remind me in 5 minutes to ..."
     scheduled a reminder from the rest of the sentence. authorize() was
     called zero times in both.
  B. Pack/scope bound to whether the ID resolved into the registry, so a
     SYNTHETIC id walked past a restriction that stopped the identical
     spoken command.
  C. An approval bound a command and its arguments but not its TARGET, so a
     "Close the window?" answered after alt-tabbing closed the other window.
  D. A zero-second repeat interval could be staged.

Nothing here imports dictation.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from samsara import execution_policy as ep
from samsara.execution_policy import Allowed, Denied, Invocation, NeedsConfirmation, Route


# ---------------------------------------------------------------------------
# Fakes that RECORD effects rather than perform them
# ---------------------------------------------------------------------------

class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Notifications:
    def __init__(self, sink):
        self.sink = sink

    def parse_remind_command(self, text):
        import re
        for pattern in (r"remind me in (\d+) minutes?(?: to (.+))?",
                        r"set (?:a )?reminder (?:for )?(\d+) minutes?(?: to (.+))?",
                        r"(\d+) minute reminder(?: to (.+))?"):
            m = re.search(pattern, text.lower())
            if m:
                return (int(m.group(1)), m.group(2))
        return None

    def add_quick_reminder(self, minutes, message):
        self.sink.append(("reminder", minutes, message))


class _App:
    def __init__(self, executor=None):
        self.effects = []
        self.config = {"command_mode": {"command_matching_enabled": True}}
        self.command_matching_enabled = True
        self._config_lock = _Lock()
        self.notification_manager = _Notifications(self.effects)
        self.command_executor = executor
        self._execution_generation = 1
        self.spoken = []

    def save_config(self):
        self.effects.append(("save_config", dict(self.config.get("command_mode", {}))))

    def play_sound(self, name):
        self.effects.append(("sound", name))

    def _speak(self, text, *a, **k):
        self.spoken.append(text)


@pytest.fixture(scope="module")
def executor():
    import contextlib
    import io

    from samsara import commands as cmds

    with contextlib.redirect_stdout(io.StringIO()):
        return cmds.CommandExecutor()


@pytest.fixture
def spy_authorize(monkeypatch):
    """Records every authorize call so "an effect happened without one" is a
    thing a test can actually assert."""
    seen = []
    original = ep.authorize

    def _spy(inv, **kw):
        seen.append(inv)
        return original(inv, **kw)

    monkeypatch.setattr(ep, "authorize", _spy)
    return seen


# ===========================================================================
# A. No effect before authorization
# ===========================================================================

PROSE = [
    "Yesterday I finally worked out why it kept firing, so I turned command "
    "mode off and the problem went away entirely.",
    "The manual says you should enable command mode before you try any of this.",
    "I had to disable command mode on the other machine as well.",
    "Tell Sarah to remind me in 5 minutes to check whether the build passed.",
    "He said the phrase command mode on is what you say to switch it back.",
]


@pytest.mark.parametrize("text", PROSE)
def test_a_dictated_paragraph_changes_no_config_and_runs_no_effect(executor, text):
    """The defect, as a test. Each of these contains a command phrase as a
    SUBSTRING and is ordinary prose."""
    app = _App(executor)
    before = (app.command_matching_enabled, dict(app.config["command_mode"]))

    result = executor.process_text(text, app)

    assert app.effects == [], app.effects
    assert (app.command_matching_enabled, dict(app.config["command_mode"])) == before
    assert result.state.value == "miss"          # and it stays dictation
    assert result.result == text                 # returned unchanged


@pytest.mark.parametrize("text, expected", [
    ("command mode off", "command_mode_off"),
    ("Command mode off.", "command_mode_off"),
    ("disable command mode", "command_mode_off"),
    ("command mode on", "command_mode_on"),
    ("Enable command mode!", "command_mode_on"),
])
def test_the_deliberate_whole_utterance_still_works(executor, text, expected):
    """Fixing the substring must not cost the real command."""
    app = _App(executor)
    result = executor.process_text(text, app)
    assert result.state.value == "completed"
    assert result.result == expected
    assert ("save_config", {"command_matching_enabled": expected.endswith("on")}) in app.effects


def test_a_deliberate_reminder_still_works(executor):
    app = _App(executor)
    result = executor.process_text("Remind me in 5 minutes to stretch.", app)
    assert result.state.value == "completed"
    assert ("reminder", 5, "stretch") in app.effects


@pytest.mark.parametrize("text, cid", [
    ("command mode off", "session:command_mode_off"),
    ("command mode on", "session:command_mode_on"),
    ("remind me in 5 minutes to stretch", "session:reminder"),
])
def test_authorization_strictly_precedes_the_effect(executor, monkeypatch, text, cid):
    """Not "authorize was called" -- called, and called FIRST.

    Authorization and every effect append to ONE list, so the order is a fact
    the test can read rather than an arrangement it has to trust. Before 126
    this list would have held effects and no authorize at all."""
    order = []
    app = _App(executor)

    original_authorize = ep.authorize

    def _ordered_authorize(inv, **kw):
        order.append(("authorize", inv.command_id))
        return original_authorize(inv, **kw)

    monkeypatch.setattr(ep, "authorize", _ordered_authorize)

    original_save = app.save_config
    original_sound = app.play_sound
    original_add = app.notification_manager.add_quick_reminder

    monkeypatch.setattr(app, "save_config",
                        lambda: (order.append(("effect", "save_config")), original_save())[-1])
    monkeypatch.setattr(app, "play_sound",
                        lambda name: (order.append(("effect", "sound")), original_sound(name))[-1])
    monkeypatch.setattr(app.notification_manager, "add_quick_reminder",
                        lambda m, msg: (order.append(("effect", "reminder")),
                                        original_add(m, msg))[-1])

    executor.process_text(text, app)

    kinds = [kind for kind, _what in order]
    assert "authorize" in kinds, "the effect ran with no authorization at all"
    assert "effect" in kinds, "nothing happened -- the fixture is wrong, not the code"
    assert kinds.index("authorize") < kinds.index("effect"), order
    assert order[0] == ("authorize", cid), order


@pytest.mark.parametrize("text", ["command mode off", "remind me in 5 minutes to stretch"])
def test_a_refused_session_control_runs_nothing_and_is_not_dictation(executor, monkeypatch, text):
    """When authorize says no, the effect must not happen -- and the
    utterance was still CLAIMED, so it must not fall through to dictation
    either."""
    monkeypatch.setattr(ep, "authorize",
                        lambda inv, **kw: Denied("pack_disabled", detail="test"))
    app = _App(executor)

    result = executor.process_text(text, app)

    assert app.effects == []
    assert app.command_matching_enabled is True
    assert result.state.value == "rejected"
    assert result.detail.get("reason") == "pack_disabled"


def test_the_session_controls_have_real_identities():
    """They are effects, so they have ids the policy can decide on."""
    for cid in ("session:command_mode_on", "session:command_mode_off", "session:reminder"):
        assert ep.command_exists(cid), cid
        risk, reversible, _schema = ep.classify(cid)
        assert risk == ep.RISK_WRITE and reversible is True, cid


def test_a_model_cannot_toggle_command_mode_without_a_question(executor):
    """RISK_WRITE: a user route runs it, a model route has to be confirmed."""
    app = _App(executor)
    gen = ep.current_generation(app)
    spoken = ep.authorize(Invocation("session:command_mode_off", {}, Route.EXACT, gen, "", ""),
                          app=app, executor=executor, quiet=True)
    proposed = ep.authorize(Invocation("session:command_mode_off", {}, Route.MODEL, gen, "", ""),
                            app=app, executor=executor, quiet=True)
    assert isinstance(spoken, Allowed)
    assert not isinstance(proposed, Allowed)


# ===========================================================================
# B. The restriction binds to the EFFECT, not to whether an id resolved
# ===========================================================================

@pytest.fixture
def pack_off(executor):
    """Switch off the pack that governs the app verbs, and put it back."""
    matcher = executor._matcher
    governed = ep.governing_command_id("action2:open")
    entry = executor._registry_entry(governed)
    assert entry is not None and entry.pack, "the governor must be a real registry command"
    before = matcher._enabled_packs
    all_packs = {e.pack for e in matcher._sorted}
    matcher.set_enabled_packs(all_packs - {entry.pack})
    yield entry.pack
    matcher._enabled_packs = before


#: Every route in the enum. Parametrised from Route itself, so a NEW route
#: added without a guard fails this test rather than slipping through.
ALL_ROUTES = list(Route)


def test_the_route_list_is_the_enum_itself():
    """If this drifts, the parametrisation below has stopped covering
    everything and the guarantee is gone."""
    assert set(ALL_ROUTES) == set(Route)
    assert len(ALL_ROUTES) >= 6


@pytest.mark.parametrize("route", ALL_ROUTES, ids=[r.value for r in ALL_ROUTES])
@pytest.mark.parametrize("cid", ["action2:open", "action2:focus", "action2:close"])
def test_a_disabled_pack_refuses_the_effect_by_every_route(executor, pack_off, route, cid):
    """The same operation the spoken command is refused for must be refused
    however it arrives: grammar, model, macro, smart action, schedule."""
    app = _App(executor)
    inv = Invocation(cid, {"target": "notepad"}, route,
                     ep.current_generation(app), "", "")
    decision = ep.authorize(inv, app=app, executor=executor, quiet=True)
    assert isinstance(decision, Denied), f"{cid} via {route.value} was {decision}"
    assert decision.reason == "pack_disabled"


@pytest.mark.parametrize("route", ALL_ROUTES, ids=[r.value for r in ALL_ROUTES])
def test_the_registered_command_is_refused_the_same_way(executor, pack_off, route):
    """The other half of the comparison: the registered id and the synthetic
    id now give the same ANSWER.

    The reasons differ, and honestly so: a disabled pack removes a plugin
    command from the registry index entirely, so the registered id reads
    `unknown_command` while the synthetic one reads `pack_disabled`. Both are
    Denied, which is the contract -- the defect was one of them being
    Allowed."""
    app = _App(executor)
    governed = ep.governing_command_id("action2:open")
    inv = Invocation(governed, {}, route, ep.current_generation(app), "", "")
    decision = ep.authorize(inv, app=app, executor=executor, quiet=True)
    assert isinstance(decision, Denied), decision
    assert decision.reason in ("pack_disabled", "unknown_command"), decision.reason


def test_with_the_pack_on_the_synthetic_id_is_allowed_again(executor):
    """The guard must not be a blanket refusal -- it has to track the pack."""
    app = _App(executor)
    inv = Invocation("action2:focus", {"target": "notepad"}, Route.GRAMMAR,
                     ep.current_generation(app), "", "")
    decision = ep.authorize(inv, app=app, executor=executor, quiet=True)
    assert isinstance(decision, Allowed)


def test_every_synthetic_family_declares_its_governor():
    """A synthetic id that stands for a registered command must say which
    one. This is the list; adding a family without an entry is the bug."""
    assert ep.governing_command_id("action2:open") == "open"
    assert ep.governing_command_id("action2:focus") == "focus"
    assert ep.governing_command_id("action2:close") == "close"
    # A registry id governs itself.
    assert ep.governing_command_id("open chrome") == "open chrome"
    for cid, governor in ep.SYNTHETIC_GOVERNORS.items():
        assert cid != governor, cid


def test_an_out_of_scope_target_refuses_by_every_route(executor, monkeypatch):
    """Scope, not just packs: a command that is not live here must be refused
    on every route too."""
    monkeypatch.setattr(executor, "command_availability",
                        lambda cid, context=None: ("out_of_scope", "foreground is notepad.exe")
                        if cid in ("open", "focus", "close") else None)
    app = _App(executor)
    for route in ALL_ROUTES:
        inv = Invocation("action2:open", {"target": "x"}, route,
                         ep.current_generation(app), "", "")
        decision = ep.authorize(inv, app=app, executor=executor, quiet=True)
        assert isinstance(decision, Denied), route
        assert decision.reason == "out_of_scope", route


# ===========================================================================
# C. An approval binds the target it was granted for
# ===========================================================================

WINDOW_A = {"hwnd": 1001, "process": "bank.exe"}
WINDOW_B = {"hwnd": 2002, "process": "notepad.exe"}


def _staged(executor, windows, name="the window"):
    app = _App(executor)
    inv = Invocation("action2:close", {"target": name}, Route.GRAMMAR,
                     ep.current_generation(app), "", "")
    ran = []
    targets, probe = ep.bind_target(inv, resolver=lambda n: windows.get(n))
    op = ep.stage_pending(app, inv, "Close the window?",
                          on_approve=lambda o: ran.append(dict(windows.get(name) or {})),
                          record_type="action2", targets=targets, target_probe=probe)
    return app, op, ran, targets


def test_an_approval_binds_the_window_it_was_granted_for(executor):
    windows = {"the window": dict(WINDOW_A)}
    _app, _op, _ran, targets = _staged(executor, windows)
    assert targets == WINDOW_A


def test_a_target_changed_after_confirmation_is_refused_and_named(executor):
    """Astra's acceptance criterion. Nothing happens to the new window, and
    the refusal says what changed."""
    windows = {"the window": dict(WINDOW_A)}
    app, op, ran, _ = _staged(executor, windows)

    windows["the window"] = dict(WINDOW_B)        # the user alt-tabbed
    why = op.refusal(app)

    assert why is not None and why.startswith("target changed")
    assert "notepad.exe" in why and "bank.exe" in why
    assert op.target_change == "that is notepad.exe now, not bank.exe"

    ep.answer_pending(app, "yes")
    assert ran == [], "the effect ran on the window the user did not mean"


def test_the_target_is_never_silently_re_resolved(executor):
    """"Refused or re-resolved, never silently substituted" -- and this
    implementation refuses. The record is consumed, not retargeted."""
    windows = {"the window": dict(WINDOW_A)}
    app, op, ran, targets = _staged(executor, windows)
    windows["the window"] = dict(WINDOW_B)

    ep.answer_pending(app, "yes")

    assert op.targets == WINDOW_A                 # the binding did not move
    assert ran == []
    assert op.approved is False                   # single use: it is spent


def test_a_target_that_no_longer_exists_is_refused_and_says_so(executor):
    """What happens when the target is gone at all."""
    windows = {"the window": dict(WINDOW_A)}
    app, op, ran, _ = _staged(executor, windows)

    windows.pop("the window")
    why = op.refusal(app)

    assert why is not None and "not open any more" in why
    ep.answer_pending(app, "yes")
    assert ran == []


def test_an_unchanged_target_still_approves(executor):
    """The guard must not refuse the ordinary case."""
    windows = {"the window": dict(WINDOW_A)}
    app, op, ran, _ = _staged(executor, windows)

    assert op.refusal(app) is None
    ep.answer_pending(app, "yes")
    assert ran == [WINDOW_A]


def test_staging_binds_a_target_by_default_without_the_caller_asking(executor, monkeypatch):
    """The support existed before 126 and NO caller passed it, which is why
    the defect shipped. Binding is now the default for any invocation that
    names a target."""
    monkeypatch.setattr(ep, "resolve_target", lambda name: dict(WINDOW_A))
    app = _App(executor)
    inv = Invocation("action2:close", {"target": "the window"}, Route.GRAMMAR,
                     ep.current_generation(app), "", "")

    op = ep.stage_pending(app, inv, "Close the window?", record_type="action2")

    assert op.targets == WINDOW_A
    assert op._target_probe is not None


def test_an_invocation_with_no_target_binds_nothing(executor):
    """A command that names no target must not acquire a spurious one."""
    app = _App(executor)
    inv = Invocation("session:command_mode_off", {}, Route.EXACT,
                     ep.current_generation(app), "", "")
    targets, probe = ep.bind_target(inv)
    assert targets is None and probe is None


@pytest.mark.parametrize("key", list(ep.TARGET_ARG_NAMES))
def test_every_target_argument_name_is_bound(executor, key):
    inv = Invocation("action2:close", {key: "the window"}, Route.GRAMMAR, 1, "", "")
    targets, probe = ep.bind_target(inv, resolver=lambda n: dict(WINDOW_A))
    assert targets == WINDOW_A, key
    assert callable(probe)


def test_a_resolver_that_raises_cannot_approve_anything():
    """A probe that throws must refuse, not pass."""
    def _angry(_name):
        raise RuntimeError("no window server")

    inv = Invocation("action2:close", {"target": "x"}, Route.GRAMMAR, 1, "", "")
    targets, probe = ep.bind_target(inv, resolver=_angry)
    assert targets == {"gone": "x"}               # unresolvable at stage time
    assert probe() == {"gone": "x"}


# ===========================================================================
# D. Zero-interval schedules
# ===========================================================================

@pytest.fixture(scope="module")
def ava():
    from plugins.commands import ask_ollama
    return ask_ollama


@pytest.mark.parametrize("interval", [0, 1, 2, 4, -1, -600])
def test_a_sub_floor_interval_is_rejected(ava, interval):
    error = ava.schedule_interval_error(interval)
    assert error is not None, interval
    assert str(ava.MIN_SCHEDULE_INTERVAL_S) in error


@pytest.mark.parametrize("interval", [5, 30, 300, 3600])
def test_a_sensible_interval_is_accepted(ava, interval):
    assert ava.schedule_interval_error(interval) is None


@pytest.mark.parametrize("interval", [None, "soon", "", float("inf"), float("nan")])
def test_an_uninterpretable_interval_is_rejected(ava, interval):
    assert ava.schedule_interval_error(interval) is not None


def test_the_floor_is_stated_and_above_zero(ava):
    assert ava.MIN_SCHEDULE_INTERVAL_S >= 5


def test_a_sub_floor_interval_never_reaches_the_pending_slot(ava, monkeypatch):
    """Rejected BEFORE staging: it must not become a question the user could
    say yes to."""
    said = []
    monkeypatch.setattr(ava, "speak", lambda app, text, **k: said.append(text))
    monkeypatch.setattr(ava, "_pending_action", None, raising=False)

    outcome = ava.handle_response(
        None, "CONFIRM Refresh every zero seconds.\nSCHEDULE 0 refresh page",
        original_text="refresh every zero seconds", generation=None)

    assert ava._pending_action is None, "a sub-floor schedule was staged anyway"
    assert outcome.state == "refused"
    assert outcome.reason == "interval_below_floor"
    assert said and str(ava.MIN_SCHEDULE_INTERVAL_S) in said[-1]


def test_a_valid_interval_still_stages(ava, monkeypatch):
    said = []
    monkeypatch.setattr(ava, "speak", lambda app, text, **k: said.append(text))
    monkeypatch.setattr(ava, "_pending_action", None, raising=False)

    outcome = ava.handle_response(
        None, "CONFIRM Refresh every five minutes.\nSCHEDULE 300 refresh page",
        original_text="refresh every five minutes", generation=None)

    assert outcome.state == "queued"
    assert ava._pending_action is not None
    assert ava._pending_action["interval_seconds"] == 300
    ava._pending_action = None


def test_the_worker_refuses_a_sub_floor_task_whatever_door_it_came_through(ava):
    """Staging is where the user is told; this is the belt and braces."""
    assert ava._start_schedule(None, {"interval_seconds": 0, "confirm_text": "x"}) is None
    assert ava._start_schedule(None, {"interval_seconds": None, "confirm_text": "x"}) is None
