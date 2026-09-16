"""Queue 107 / Astra F1 + F2: what Ava is offered is exactly what she can run,
one execution API reports what really happened, and pack/scope restrictions
hold at the effect boundary -- not only in the matcher.

Everything here uses the REAL CommandExecutor, the real registry, the real
plugins and the real execution policy. The only things substituted are the
final OS effects: volume.py's audio device and the built-in handler registry.
dictation is never imported.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara import ava_command_session, command_scope, commands as commands_mod
from samsara import execution_policy as ep
from samsara.command_registry import DispatchState
from samsara.commands import CommandExecutor
from samsara.execution_policy import Invocation, Route

from plugins.commands import ask_ollama


# ---------------------------------------------------------------------------
# Fixtures: one real executor, a fake app, no OS effects
# ---------------------------------------------------------------------------

def _load_real_plugins():
    """Register the app's REAL plugin commands.

    tests/conftest.py clears the plugin registry and refuses to load
    plugins/commands/ for every test -- which is a large part of why F1
    survived the suite: no test could see a plugin reach the executor. These
    tests are about exactly that boundary, so they put the real plugins back
    (importing a module registers it; _reinstall_module_commands restores one
    the isolation fixture has since cleared)."""
    import importlib
    import pkgutil

    import plugins.commands as package
    from samsara import plugin_commands

    for info in pkgutil.iter_modules(package.__path__):
        try:
            module = importlib.import_module(f"plugins.commands.{info.name}")
        except Exception:                       # a plugin that cannot import here
            continue
        plugin_commands._reinstall_module_commands(module)


@pytest.fixture(scope="module")
def executor():
    _load_real_plugins()
    return CommandExecutor()


@pytest.fixture(autouse=True)
def _real_plugins_present(executor):
    """conftest's isolation fixture clears the registry before every test; put
    the real entries back so execution_policy's plugin lookups see them."""
    _load_real_plugins()
    yield


class _Coordinator:
    def __init__(self):
        self.said = []

    def speak(self, text, category=None, interruptible=None):
        self.said.append((text, category))
        return SimpleNamespace(utterance_id="test")


def _app(executor, **over):
    app = SimpleNamespace(
        command_executor=executor,
        config={'ollama': {'enabled': True}},
        audio_coordinator=_Coordinator(),
        _ava_cmd_generation=0,
    )
    for k, v in over.items():
        setattr(app, k, v)
    executor._app = app
    return app


@pytest.fixture
def app(executor):
    app = _app(executor)
    yield app
    pending = ep.pending_operation()
    if pending is not None:
        pending.cancel(app, reason="test teardown", chip=False)
    executor._app = None
    executor.rebuild_matcher()


@pytest.fixture
def gen(app):
    return ep.current_generation(app)


@pytest.fixture
def volume(monkeypatch):
    """The plugin's real handler runs; only the audio device is substituted."""
    from plugins.commands import volume as volume_plugin

    calls = []
    monkeypatch.setattr(volume_plugin._audio, "get_volume", lambda: 0.5)
    monkeypatch.setattr(volume_plugin._audio, "set_volume",
                        lambda level: calls.append(level) or True)
    return calls


@pytest.fixture
def builtin_effects(monkeypatch):
    """Substitute ONLY the final OS effect of built-ins (key presses, launches)."""
    ran = []

    class _Recorder:
        @staticmethod
        def execute(cmd, ctx):
            ran.append(cmd)
            return True

    monkeypatch.setattr(commands_mod, "get_handler", lambda cmd_type: _Recorder)
    return ran


def _disable_pack(executor, app, pack):
    app.config['command_packs'] = {pack: False}
    executor.rebuild_matcher()


# ---------------------------------------------------------------------------
# 1. Every offered name is one the executor will accept
# ---------------------------------------------------------------------------

class TestOneExecutableSet:
    def test_every_offered_name_is_accepted_by_the_executor(self, executor, app, gen):
        """The whole offered set, not a sample: the menu and the executor must
        not disagree about a single name (F1)."""
        menu = executor.ava_menu("", app=app, limit=None, max_chars=0)
        assert len(menu) > 120, "the menu should be the executable set, not a slice"
        refused = []
        for name in menu:
            decision = ep.authorize(
                Invocation(name, {}, Route.MODEL, gen, "", ""), app=app, executor=executor)
            if isinstance(decision, ep.Denied):
                refused.append((name, decision.reason))
        assert refused == []

    def test_the_menu_carries_builtins_and_plugins(self, executor, app):
        menu = set(executor.ava_menu("", app=app, limit=None, max_chars=0))
        sources = {executor._registry_entry(n).source for n in menu}
        assert sources == {"builtin", "plugin"}

    def test_volume_up_and_submit_are_reachable_through_ava(self, executor, app, gen):
        """The owner's own report: Ava said there was no command for the
        volume and did not know "submit". Both were past the old
        `sorted(names)[:100]` alphabetical cut -- "volume up" is a plugin, so
        it was doubly invisible."""
        for utterance, wanted in (("turn the volume up", "volume up"),
                                  ("submit the form", "submit")):
            assert wanted in executor.ava_menu(utterance, app=app), wanted
            assert wanted in ava_command_session._build_shortlist(
                app, utterance, {"shortlist_size": 12}), wanted
            decision = ep.authorize(Invocation(wanted, {}, Route.MODEL, gen, "", ""),
                                    app=app, executor=executor)
            assert not isinstance(decision, ep.Denied), (wanted, decision)

    def test_the_bound_is_relevance_not_the_alphabet(self, executor, app):
        menu = executor.ava_menu("turn the volume up", app=app, limit=20)
        assert menu[0] == "volume up"
        assert menu != sorted(menu)
        # The old cut kept only names up to "mute tab"; the menu now reaches
        # the end of the alphabet.
        full = executor.ava_menu("", app=app, limit=None, max_chars=0)
        assert [n for n in full if n > "n"], "nothing past 'n' is the old truncation"
        # The old menu was the first 100 AI-visible BUILT-INS by name, ending
        # at "notifications": these are past that cut, and the owner could not
        # reach them.
        for beyond_the_old_cut in ("volume up", "submit", "zoom in"):
            assert beyond_the_old_cut in full, beyond_the_old_cut

    def test_a_tight_budget_drops_the_least_relevant_not_a_letter_range(self, executor, app):
        small = executor.ava_menu("turn the volume up", app=app, limit=None, max_chars=120)
        assert "volume up" in small
        assert len(", ".join(small)) <= 120

    def test_an_out_of_scope_command_is_not_offered(self, executor, app):
        scoped = [e for e in executor.executable_entries() if e.scope is not None]
        menu = executor.ava_menu("", app=app, limit=None, max_chars=0)
        for entry in executor._matcher._sorted:
            if entry.scope is not None and entry not in scoped:
                assert entry.phrase not in menu, entry.phrase


# ---------------------------------------------------------------------------
# 2. One execution API, honest results
# ---------------------------------------------------------------------------

class TestOneExecutionApi:
    def test_a_model_proposed_plugin_command_executes_for_real(self, executor, app, gen, volume):
        result = executor.execute_canonical("volume up", app, route=Route.MODEL, generation=gen)
        assert result.state is DispatchState.COMPLETED
        assert volume == [0.7], "the real handler ran; only the device was substituted"

    def test_the_same_command_through_the_model_response_parser(self, executor, app, gen, volume):
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Turning the volume up\nACTION volume up",
            original_text="turn it up", generation=gen)
        assert outcome.state == "completed" and outcome.name == "volume up"
        assert volume == [0.7]
        assert ask_ollama._outcome_chip(app, outcome)[1] == "success"

    def test_an_alias_resolves_to_its_canonical_command(self, executor, app, gen, volume):
        result = executor.execute_canonical("turn it up", app, route=Route.MODEL, generation=gen)
        assert result.state is DispatchState.COMPLETED and result.phrase == "volume up"

    def test_execute_command_is_the_boolean_face_of_the_same_call(self, executor, app, gen, volume):
        assert executor.execute_command("volume up", app, route=Route.MODEL, generation=gen) is True
        assert executor.execute_command("no such command", app, route=Route.MODEL, generation=gen) is False

    def test_an_unknown_name_is_refused_not_silently_dropped(self, executor, app, gen):
        result = executor.execute_canonical("fly to the moon", app, route=Route.MODEL, generation=gen)
        assert result.state is DispatchState.REJECTED
        assert result.detail['reason'] == "unknown_command"

    def test_a_stale_response_executes_nothing(self, executor, app, gen, volume):
        ep.bump_generation(app, "cancel")          # the user cancelled mid-flight
        result = executor.execute_canonical("volume up", app, route=Route.MODEL, generation=gen)
        assert result.state is DispatchState.REJECTED and result.detail['reason'] == "stale"
        assert volume == []


# ---------------------------------------------------------------------------
# 3. No success chip for a rejected action
# ---------------------------------------------------------------------------

class TestNoSuccessChipAfterFailure:
    def _refused_outcome(self, executor, app, gen):
        _disable_pack(executor, app, "media")
        return ask_ollama.handle_response(
            app, "CONFIRM Turning the volume up\nACTION volume up",
            original_text="turn it up", generation=gen)

    def test_a_refused_action_is_spoken_and_chipped_as_a_refusal(self, executor, app, gen, volume):
        outcome = self._refused_outcome(executor, app, gen)
        assert outcome.state == "refused" and outcome.reason == "pack_disabled"
        assert volume == [], "nothing ran"
        label, kind = ask_ollama._outcome_chip(app, outcome)
        assert kind == "error" and "volume up" in label
        spoken = " ".join(text for text, _c in app.audio_coordinator.said)
        assert "volume up" in spoken and "pack" in spoken

    def test_the_ava_turn_chip_follows_the_refusal_not_the_response(
            self, executor, app, gen, volume, monkeypatch):
        """End of the F1 path: handle_ask_ava used to stamp "Ava ✓" whatever
        handle_response did -- including a command that never ran."""
        _disable_pack(executor, app, "media")
        monkeypatch.setattr(ask_ollama, "ask_ollama",
                            lambda prompt, app_, model=None, system=None:
                            "CONFIRM Turning the volume up\nACTION volume up")
        monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda a, t: False)
        monkeypatch.setattr(ask_ollama.thread_registry, "spawn",
                            lambda name, fn, args=(), daemon=True: fn(*args))
        ask_ollama.handle_ask_ava(app, "turn the volume up", generation=gen)
        label, kind = app._ava_turn_outcome
        assert kind == "error" and "refused" in label
        assert ask_ollama._CHIP_CHECK not in label
        assert volume == []

    def test_a_completed_action_still_reports_success(self, executor, app, gen, volume, monkeypatch):
        monkeypatch.setattr(ask_ollama, "ask_ollama",
                            lambda prompt, app_, model=None, system=None:
                            "CONFIRM Turning the volume up\nACTION volume up")
        monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda a, t: False)
        monkeypatch.setattr(ask_ollama.thread_registry, "spawn",
                            lambda name, fn, args=(), daemon=True: fn(*args))
        ask_ollama.handle_ask_ava(app, "turn the volume up", generation=gen)
        label, kind = app._ava_turn_outcome
        assert kind == "success" and volume == [0.7]

    def test_a_staged_confirmation_is_not_reported_as_done(self, executor, app, gen, builtin_effects):
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Closing the window\nACTION close window",
            original_text="close the window", generation=gen)
        assert outcome.state == "queued"
        assert builtin_effects == [], "nothing runs before the yes"
        label, kind = ask_ollama._outcome_chip(app, outcome)
        assert kind == "warning" and "yes" in label

    def test_the_command_session_counts_a_refused_proposal_as_a_miss(
            self, executor, app, gen, volume, monkeypatch):
        _disable_pack(executor, app, "media")
        monkeypatch.setattr(ask_ollama, "ask_ollama",
                            lambda prompt, app_, model=None, system=None:
                            "CONFIRM Turning the volume up\nACTION volume up")
        monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda a: "{COMMAND_LIST}")
        hit = ava_command_session._stage_c_llm_fallback(
            app, "turn the volume up", ["volume up"], gen, ava_command_session._DEFAULTS)
        assert hit is False, "a refused proposal is a miss, not a hit"
        assert volume == []

    def test_the_command_session_still_counts_a_real_hit(self, executor, app, gen, volume, monkeypatch):
        monkeypatch.setattr(ask_ollama, "ask_ollama",
                            lambda prompt, app_, model=None, system=None:
                            "CONFIRM Turning the volume up\nACTION volume up")
        monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda a: "{COMMAND_LIST}")
        hit = ava_command_session._stage_c_llm_fallback(
            app, "turn the volume up", ["volume up"], gen, ava_command_session._DEFAULTS)
        assert hit is True and volume == [0.7]


# ---------------------------------------------------------------------------
# 4. Scope and pack hold at the effect boundary (F2)
# ---------------------------------------------------------------------------

class TestRestrictionsAtTheEffectBoundary:
    def test_a_disabled_pack_is_refused_at_execution(self, executor, app, gen, volume):
        _disable_pack(executor, app, "media")
        result = executor.execute_canonical("volume up", app, route=Route.MODEL, generation=gen)
        assert result.state is DispatchState.REJECTED
        assert result.detail['reason'] == "pack_disabled" and result.detail['detail'] == "media"
        assert volume == []

    def test_a_disabled_pack_is_refused_on_every_route(self, executor, app, gen, volume):
        _disable_pack(executor, app, "media")
        for route in (Route.EXACT, Route.GRAMMAR, Route.MODEL, Route.SCHEDULE):
            result = executor.execute_canonical("volume up", app, route=route, generation=gen)
            assert result.state is DispatchState.REJECTED, route
        assert volume == []

    def test_a_scope_that_is_not_live_is_refused_at_execution(self, executor, app, gen, monkeypatch):
        scoped = next((e for e in executor._matcher._sorted
                       if e.scope is not None and e.scope.tags), None)
        assert scoped is not None, "this test needs a tag-scoped command"
        tag = sorted(scoped.scope.tags)[0]
        monkeypatch.setitem(command_scope._tag_sources, tag, lambda: False)
        assert executor.command_availability(scoped.phrase)[0] == "out_of_scope"
        result = executor.execute_canonical(scoped.phrase, app, route=Route.MODEL, generation=gen)
        assert result.state is DispatchState.REJECTED
        assert result.detail['reason'] == "out_of_scope"

    def test_a_confirmed_delayed_callback_re_checks_the_restriction(
            self, executor, app, gen, builtin_effects):
        """Astra's specific case: the confirmation re-authorizes risk but used
        to skip scope/pack, so a "yes" could run what the matcher would now
        refuse."""
        staged = executor.execute_canonical("delete next word", app, route=Route.MODEL,
                                            generation=gen)
        assert staged.state is DispatchState.QUEUED
        assert ep.pending_operation() is not None
        _disable_pack(executor, app, "text-editing")
        answer = ep.answer_pending(app, "yes")
        assert answer == "approved"
        assert builtin_effects == [], "the effect must not run after the pack was switched off"

    def test_a_confirmed_callback_still_runs_when_nothing_changed(
            self, executor, app, gen, builtin_effects):
        staged = executor.execute_canonical("delete next word", app, route=Route.MODEL,
                                            generation=gen)
        assert staged.state is DispatchState.QUEUED
        assert ep.answer_pending(app, "yes") == "approved"
        assert [c.get('description') for c in builtin_effects] != []

    def test_a_scheduled_repeat_is_refused_and_stopped(self, executor, app, gen, volume, monkeypatch):
        stopped = []
        monkeypatch.setattr(ask_ollama, "_stop_schedule", lambda: stopped.append(True))
        _disable_pack(executor, app, "media")
        ask_ollama._execute_safe(app, {"command": "volume up", "generation": gen,
                                       "confirm_text": "Repeat volume up every 5 seconds?"})
        assert volume == [] and stopped == [True]

    def test_a_scheduled_repeat_still_runs_when_nothing_blocks_it(
            self, executor, app, gen, volume, monkeypatch):
        stopped = []
        monkeypatch.setattr(ask_ollama, "_stop_schedule", lambda: stopped.append(True))
        ask_ollama._execute_safe(app, {"command": "volume up", "generation": gen})
        assert volume == [0.7] and stopped == []

    def test_the_spoken_path_still_refuses_a_disabled_pack(self, executor, app, gen, volume):
        _disable_pack(executor, app, "media")
        result = executor.process_text("volume up", app, force_commands=True)
        assert result.state is DispatchState.MISS          # the matcher filters first
        assert result.detail.get('reason') == "pack_disabled"
        assert volume == []


# ---------------------------------------------------------------------------
# 5. The sound boundary Astra asked to keep
# ---------------------------------------------------------------------------

class TestClaimedCommandsNeverBecomeDictation:
    def test_a_failed_command_stays_claimed(self, executor, app, monkeypatch):
        class _Failing:
            @staticmethod
            def execute(cmd, ctx):
                return False

        monkeypatch.setattr(commands_mod, "get_handler", lambda cmd_type: _Failing)
        result = executor.process_text("copy", app, force_commands=True)
        assert result.state is DispatchState.FAILED
        assert result.claimed is True and result.result == "copy"

    def test_a_raising_plugin_stays_claimed(self, executor, app, monkeypatch):
        entry = executor._registry_entry("volume up")
        monkeypatch.setattr(entry, "handler", lambda a, r: (_ for _ in ()).throw(RuntimeError("boom")))
        result = executor.process_text("volume up", app, force_commands=True)
        assert result.state is DispatchState.FAILED and result.claimed is True

    def test_a_refused_command_stays_claimed(self, executor, app, volume):
        _disable_pack(executor, app, "media")
        result = executor.execute_canonical("volume up", app, route=Route.MODEL,
                                            generation=ep.current_generation(app))
        assert result.claimed is True and result.state is DispatchState.REJECTED

    def test_a_plugin_that_declines_gives_the_words_back(self, executor, app, monkeypatch):
        entry = executor._registry_entry("volume up")
        monkeypatch.setattr(entry, "handler", lambda a, r: False)
        result = executor.process_text("volume up", app, force_commands=True)
        assert result.state is DispatchState.MISS
        assert result.result == "volume up" and result.detail.get('declined_by') == "volume up"
