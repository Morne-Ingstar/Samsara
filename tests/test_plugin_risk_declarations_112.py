"""Queue 112: every plugin command declares an honest risk class, so the
policy can decide about it instead of withholding it for want of a decision.

Queue 107 made Ava's menu and her executor agree by putting every candidate
through execution_policy.authorize(quiet=True). That was right, and it
exposed this: 86 of 268 candidates were withheld as "unvalidated" -- plugin
commands that declared no risk_class, so a model was not allowed to call
them. "scroll down" and "page down" were among them.

This file holds the result in place:

  * the GATE -- every registered plugin command declares a risk class. A new
    command that forgets fails here, by name, with the file to edit.
  * no command that was already classified changed class;
  * nothing that was deliberately off the model allow-list became callable;
  * the commands the owner reported are offered AND run.

Like tests/test_ava_executable_contract_107.py, this uses the REAL registry,
the REAL plugins and the REAL policy. tests/conftest.py clears the plugin
registry for every test -- which is most of why this gap survived the suite
-- so the plugins are put back explicitly.
"""
import importlib
import pkgutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara import execution_policy as ep
from samsara.commands import CommandExecutor
from samsara.execution_policy import Invocation, Route

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "plugins" / "commands"

#: The vocabulary execution_policy._PLUGIN_RISK_CLASS understands. A value
#: outside this set silently becomes RISK_UNKNOWN, which is the bug this
#: file exists to prevent -- a declaration that looks present and is not.
KNOWN_RISK_WORDS = frozenset({"read", "ui", "write", "safe", "reversible", "destructive"})

#: Measured on this tree before 112 (queue 107 reported the same split).
BEFORE_OFFERED = 158
BEFORE_UNVALIDATED = 86
BEFORE_NOT_ALLOWED = 24
CANDIDATES = 268

#: The 24 write/destructive commands 107 found deliberately off the model
#: allow-list. 112 must not have moved one of them.
NOT_ALLOWED_BEFORE = frozenset({
    "backspace", "bookmark this", "delete", "duplicate tab", "full stop",
    "new paragraph", "new virtual desktop", "on screen keyboard",
    "open bookmarks", "open keyboard", "pin tab", "print", "private window",
    "read next sentence", "read this", "save", "scratch everything",
    "scratch that", "space", "start narrator", "stop narrator",
    "stop reading", "undo that", "virtual keyboard",
})

#: Declared before 112 and therefore NOT ours to touch. Pinned by value so a
#: later pass cannot quietly relabel somebody else's judgement.
ALREADY_CLASSIFIED = {
    "close": "reversible",
    "undo health log": "destructive",
}


def _load_real_plugins():
    """Register the app's REAL plugin commands (107's helper, same reason)."""
    import plugins.commands as package
    from samsara import plugin_commands
    for info in pkgutil.iter_modules(package.__path__):
        try:
            module = importlib.import_module(f"plugins.commands.{info.name}")
        except Exception:
            continue
        plugin_commands._reinstall_module_commands(module)


@pytest.fixture(scope="module")
def executor():
    _load_real_plugins()
    return CommandExecutor()


@pytest.fixture(autouse=True)
def _real_plugins_present(executor):
    _load_real_plugins()
    yield


@pytest.fixture
def app(executor):
    app = SimpleNamespace(
        command_executor=executor,
        config={'ollama': {'enabled': True}},
        audio_coordinator=SimpleNamespace(
            speak=lambda *a, **k: SimpleNamespace(utterance_id="test")),
        _ava_cmd_generation=0,
    )
    executor._app = app
    yield app
    executor._app = None
    executor.rebuild_matcher()


@pytest.fixture
def gen(app):
    return ep.current_generation(app)


def _registry():
    from samsara import plugin_commands
    return plugin_commands._REGISTRY


def _decision(executor, app, phrase, route=Route.MODEL):
    return ep.authorize(
        Invocation(phrase, {}, route, ep.current_generation(app), "", ""),
        app=app, executor=executor, quiet=True)


def _reason(executor, app, phrase):
    d = _decision(executor, app, phrase)
    return d.reason if isinstance(d, ep.Denied) else "ALLOWED"


def _buckets(executor, app):
    out = {}
    for entry in executor.executable_entries(None):
        if not executor._ai_visible(entry):
            continue
        reason = _reason(executor, app, entry.phrase)
        out.setdefault(reason, []).append(entry.phrase)
    return out


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class TestEveryPluginCommandDeclaresItsRisk:
    """The frozen gate. A new @command without a risk_class fails HERE,
    naming the phrase and its file, rather than being silently unreachable
    by Ava for months."""

    def test_no_ava_candidate_is_unclassified(self, executor, app):
        """The gate. Every command Ava could be offered declares its risk.

        Scoped to CANDIDATES -- AI-visible, in an enabled pack, live in this
        scope -- because that is the set where a missing declaration costs
        reachability. The wider registry is fenced by the next test.
        """
        missing = []
        for entry in executor.executable_entries(None):
            if not executor._ai_visible(entry):
                continue
            row = _registry().get(entry.phrase)
            if row is None:
                continue                       # a built-in, classified elsewhere
            declared = (row.get("metadata") or {}).get("risk_class")
            if declared in (None, "", "unknown"):
                source = str(row.get("source") or "?").rsplit(".", 1)[-1]
                missing.append(f"{entry.phrase!r} in plugins/commands/{source}.py")
        assert missing == [], (
            "every plugin command Ava can be offered must declare risk_class= "
            "on its @command decorator -- an unclassified command is withheld "
            "as 'unvalidated' (queue 112). Missing:\n  " + "\n  ".join(missing))

    def test_the_unclassified_backlog_does_not_spread_to_the_files_112_cleaned(self):
        """112 classified every candidate. Commands that are NOT candidates on
        this install (pack off, ai_visible=False, scope-gated) are a known
        backlog -- see the report. This pins the files 112 finished, so a new
        unclassified command in any of them fails immediately."""
        clean = {"scroll.py", "media_keys.py", "verbatim_toggle.py", "tab_finder.py",
                 "smart_actions.py", "app_verbs.py", "alarm_commands.py",
                 "core_utils.py", "health_tracker.py", "tasks.py", "text_marker.py",
                 "window_cube.py", "window_switcher.py", "windows.py"}
        offenders = {}
        for phrase, entry in sorted(_registry().items()):
            declared = (entry.get("metadata") or {}).get("risk_class")
            if declared not in (None, "", "unknown"):
                continue
            if phrase != entry.get("phrase"):
                continue                       # an alias shares its entry
            source = str(entry.get("source") or "?").rsplit(".", 1)[-1] + ".py"
            offenders.setdefault(source, []).append(phrase)
        regressions = {f: v for f, v in offenders.items() if f in clean}
        assert regressions == {}, (
            "these files were fully classified by queue 112 and have gained an "
            f"unclassified command: {regressions}")

    def test_every_declared_class_is_one_the_policy_understands(self):
        """A typo ('readonly', 'none') is not a declaration: the policy maps
        it to RISK_UNKNOWN, which looks classified and behaves unclassified."""
        wrong = []
        for phrase, entry in sorted(_registry().items()):
            declared = str((entry.get("metadata") or {}).get("risk_class") or "").lower()
            if declared and declared != "unknown" and declared not in KNOWN_RISK_WORDS:
                wrong.append(f"{phrase!r} declares {declared!r}")
        assert wrong == [], (
            f"risk_class must be one of {sorted(KNOWN_RISK_WORDS)}: " + "; ".join(wrong))

    def test_the_declaration_actually_reaches_the_policy(self, executor, app):
        """Declared, mapped, and not UNKNOWN on a model route -- the three
        steps between writing the word and Ava being allowed to consider it.
        A declaration the policy cannot read is not a declaration."""
        unknown = []
        for entry in executor.executable_entries(None):
            if not executor._ai_visible(entry) or entry.phrase not in _registry():
                continue
            risk, _rev, _schema = ep.classify(entry.phrase, executor=executor,
                                              app=app, declared_only=True)
            if risk == ep.RISK_UNKNOWN:
                unknown.append(entry.phrase)
        assert unknown == [], (
            "these declare something the policy still reads as unknown: " + str(unknown))

    def test_no_plugin_source_file_lost_its_declarations(self):
        """A crude but load-bearing check: the decorator text is in the file.
        It catches a declaration deleted by a merge that still imports."""
        for filename in ("scroll.py", "media_keys.py", "windows.py",
                         "window_switcher.py", "tasks.py", "health_tracker.py"):
            source = (PLUGIN_DIR / filename).read_text(encoding="utf-8")
            assert "risk_class" in source, filename


# ---------------------------------------------------------------------------
# The withheld count
# ---------------------------------------------------------------------------

class TestTheUnvalidatedBacklogIsGone:
    def test_nothing_is_withheld_for_want_of_a_declaration(self, executor, app):
        """The deliverable. "unvalidated" can still be returned for a DECLARED
        write command that carries no argument schema -- that is 107's rule,
        not a missing classification -- so every survivor is required to have
        a class on record."""
        buckets = _buckets(executor, app)
        undeclared = []
        for phrase in sorted(buckets.get("unvalidated", [])):
            row = _registry().get(phrase) or {}
            declared = (row.get("metadata") or {}).get("risk_class")
            if declared in (None, "", "unknown"):
                undeclared.append(phrase)
        assert undeclared == [], (
            f"{len(undeclared)} commands are still withheld for want of a "
            f"declaration (was {BEFORE_UNVALIDATED}): {undeclared}")

    def test_the_offered_set_grew(self, executor, app):
        buckets = _buckets(executor, app)
        offered = buckets.get("ALLOWED", [])
        total = sum(len(v) for v in buckets.values())
        assert abs(total - CANDIDATES) <= 2, (
            f"the candidate set moved a lot: {total} vs {CANDIDATES}")
        assert len(offered) > BEFORE_OFFERED, (
            f"112 should offer more than 107's {BEFORE_OFFERED}, got {len(offered)}")

    def test_everything_still_withheld_is_write_or_destructive(self, executor, app):
        """After 112 there is exactly one honest reason left to withhold a
        registered, in-scope command from a model: it is write or destructive
        and not on the allow-list. Whether the policy words that refusal
        "not_allowed_for_model" or "unvalidated" depends on whether the
        command also declares an argument schema -- either way it must never
        be a read or ui command, because those are what Ava is for."""
        buckets = _buckets(executor, app)
        withheld = [p for reason, names in buckets.items()
                    if reason != "ALLOWED" for p in names]
        for phrase in withheld:
            risk, _rev, _schema = ep.classify(phrase, executor=executor, app=app,
                                              declared_only=True)
            assert risk in (ep.RISK_WRITE, ep.RISK_DESTRUCTIVE), (
                f"{phrase!r} is withheld from the model but classifies "
                f"as {risk} -- a read/ui command should be offered")


# ---------------------------------------------------------------------------
# Nothing regressed
# ---------------------------------------------------------------------------

class TestNothingPreviouslyDecidedMoved:
    @pytest.mark.parametrize("phrase,expected", sorted(ALREADY_CLASSIFIED.items()))
    def test_a_command_that_already_declared_a_class_kept_it(self, phrase, expected):
        entry = _registry().get(phrase)
        assert entry is not None, phrase
        assert (entry.get("metadata") or {}).get("risk_class") == expected

    def test_nothing_off_the_model_allowlist_became_model_callable(self, executor, app):
        leaked = sorted(p for p in NOT_ALLOWED_BEFORE
                        if _reason(executor, app, p) == "ALLOWED")
        assert leaked == [], (
            "112 may not put a write/destructive command on the model's "
            f"allow-list; these leaked: {leaked}")

    def test_the_deliberately_withheld_set_did_not_shrink(self, executor, app):
        buckets = _buckets(executor, app)
        still = set(buckets.get("not_allowed_for_model", []))
        assert NOT_ALLOWED_BEFORE <= still, (
            f"missing from the withheld set: {sorted(NOT_ALLOWED_BEFORE - still)}")
        assert len(still) >= BEFORE_NOT_ALLOWED

    def test_a_write_command_is_not_model_callable_just_because_it_is_declared(
            self, executor, app):
        """The point of classifying honestly: saying "write" out loud must
        cost the command its model reach, not buy it one."""
        for phrase in ("note", "pain level", "save layout", "window close"):
            assert _reason(executor, app, phrase) == "not_allowed_for_model", phrase

    def test_a_destructive_command_is_not_model_callable(self, executor, app):
        for phrase in ("restart samsara", "clear health log", "delete layout",
                       "remove task", "clear completed"):
            assert _reason(executor, app, phrase) == "not_allowed_for_model", phrase


# ---------------------------------------------------------------------------
# The owner's two examples, offered AND run
# ---------------------------------------------------------------------------

@pytest.fixture
def scroll_device(monkeypatch):
    """The plugin's real handler runs; only SendInput is substituted."""
    from plugins.commands import scroll as scroll_plugin

    sent = []

    class _User32:
        @staticmethod
        def SendInput(count, _data, _size):
            sent.append(count)
            return count

    monkeypatch.setattr(scroll_plugin, "user32", _User32)
    return sent


class TestScrollDownAndPageDownReachAva:
    @pytest.mark.parametrize("phrase", ["scroll down", "page down"])
    def test_it_is_offered(self, executor, app, phrase):
        assert _reason(executor, app, phrase) == "ALLOWED"
        assert phrase in executor.ava_menu("", app=app, limit=None, max_chars=0)

    @pytest.mark.parametrize("phrase", ["scroll down", "page down"])
    def test_it_is_classified_as_ui(self, phrase):
        risk, _rev, _schema = ep.classify(phrase, declared_only=True)
        assert risk == ep.RISK_UI

    @pytest.mark.parametrize("phrase", ["scroll down", "page down", "scroll up",
                                        "scroll left", "scroll right"])
    def test_a_model_proposal_executes_for_real(self, executor, app, gen, phrase,
                                                scroll_device):
        from samsara.command_registry import DispatchState
        result = executor.execute_canonical(phrase, app, route=Route.MODEL,
                                            generation=gen)
        assert result.state == DispatchState.COMPLETED, (phrase, result)
        assert sent_something(scroll_device), phrase

    def test_the_whole_menu_is_still_executable(self, executor, app, gen):
        """107's contract, re-run over the larger menu: not one offered name
        is refused by the policy the executor asks."""
        refused = []
        for phrase in executor.ava_menu("", app=app, limit=None, max_chars=0):
            if isinstance(_decision(executor, app, phrase), ep.Denied):
                refused.append(phrase)
        assert refused == []


def sent_something(calls):
    return bool(calls) and all(c > 0 for c in calls)


# ---------------------------------------------------------------------------
# Argument-taking commands can actually be given their argument
# ---------------------------------------------------------------------------

class TestArgumentSchemas:
    #: Commands whose handler needs the spoken words to do anything.
    TAKES_TEXT = ("find tab", "window switch", "snap", "cursor to", "bring",
                  "send", "restore layout", "cube page", "focus", "open")

    @pytest.mark.parametrize("phrase", TAKES_TEXT)
    def test_it_declares_a_schema_for_its_spoken_remainder(self, phrase):
        _risk, _rev, schema = ep.classify(phrase, declared_only=True)
        assert schema, f"{phrase!r} takes words but declares no param_schema"
        assert ep.SPOKEN_REMAINDER_KEY in schema

    @pytest.mark.parametrize("phrase", TAKES_TEXT)
    def test_a_model_may_pass_that_remainder(self, executor, app, gen, phrase):
        """Without a schema this is Denied('unvalidated'); the whole reason
        the declaration is worth making."""
        decision = ep.authorize(
            Invocation(phrase, {"remainder": "something"}, Route.MODEL, gen, "", ""),
            app=app, executor=executor, quiet=True)
        if isinstance(decision, ep.Denied):
            assert decision.reason != "unvalidated", (phrase, decision)
            assert decision.reason != "invalid_args", (phrase, decision)

    def test_a_non_string_argument_is_still_refused(self, executor, app, gen):
        decision = ep.authorize(
            Invocation("find tab", {"remainder": 7}, Route.MODEL, gen, "", ""),
            app=app, executor=executor, quiet=True)
        assert isinstance(decision, ep.Denied)
        assert decision.reason == "invalid_args"

    def test_an_undeclared_argument_is_still_refused(self, executor, app, gen):
        decision = ep.authorize(
            Invocation("scroll down", {"clicks": 5}, Route.MODEL, gen, "", ""),
            app=app, executor=executor, quiet=True)
        assert isinstance(decision, ep.Denied)
        # 112 declared no schema for the zero-argument scroll commands, so an
        # invented argument is refused. 107's exemption covers the EMPTY
        # argument case only.
        assert decision.reason in ("invalid_args", "unvalidated"), decision
