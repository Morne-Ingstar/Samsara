"""tools/demo_rehearsal.py: the five-minute take rehearsed against a fixture
catalog. Covers WORKS / PARTIAL / MISSING classification, the lane model,
the argument grammars, determinism, the --live refusal, and the rule that
the tool never imports dictation.py at module level."""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara.session_modes import SessionMode  # noqa: E402
from tools import demo_rehearsal as dr  # noqa: E402

TOOL = ROOT / "tools" / "demo_rehearsal.py"


def _plugin(phrase, aliases=(), pack="core", **declared):
    meta = {k: "unknown" for k in ("ai_visible", "risk_class", "ai_composable", "side_effects",
                                   "preconditions", "voice_triggerable", "param_schema", "reversible",
                                   "preview_template")}
    meta.update(declared)
    return {"phrase": phrase, "source": "plugin", "type": "plugin", "pack": pack,
            "aliases": list(aliases), "description": "", "metadata": meta}


def _builtin(phrase, cmd_type="hotkey", pack="core"):
    return {"phrase": phrase, "source": "builtin", "type": cmd_type, "pack": pack,
            "aliases": [], "description": "", "metadata": {}}


FIXTURE_CATALOG = [
    _builtin("switch window"),
    _builtin("snap left"),
    _builtin("submit"),
    _builtin("go to top"),
    _plugin("send", ["move", "put", "throw"], pack="window-management"),
    _plugin("play music", ["play some", "put on"], pack="media", risk_class="safe"),
    _plugin("play", ["resume"], pack="media"),
    _plugin("focus", ["switch to"], pack="window-management"),
    _plugin("show numbers", ["show labels"], pack="accessibility"),
    _plugin("hey ava", ["ava"], pack="ai"),
]

FIXTURE_CONFIG = {
    "wake_word_config": {"phrase": "jarvis", "phrase_options": ["jarvis", "hey jarvis", "computer"]},
    "command_mode": {"mode": "toggle", "abort_phrases": []},
    "music_library": {},
}


@pytest.fixture
def matcher():
    return dr.build_matcher(FIXTURE_CATALOG)


@pytest.fixture
def results(matcher):
    return dr.rehearse(matcher, FIXTURE_CONFIG)


# ---------------------------------------------------------------------------
# The take itself
# ---------------------------------------------------------------------------

class TestTheTake:
    def test_script_is_fixed_and_verbatim(self):
        assert [s.utterance for s in dr.TAKE if s.kind != "dictation"] == [
            "wake up samsara",
            "play something from my alternative rock playlist",
            "put warp on the left screen and claude on the right",
            "tell claude the mode switch fix landed and ask what's next",
            "correct that",
            "scratch that",
            "go to sleep",
        ]
        assert [s.number for s in dr.TAKE] == list(range(1, 9))

    def test_paragraph_is_forty_words(self):
        assert len(dr.PARAGRAPH.split()) == 40
        assert dr.TAKE[4].utterance == dr.PARAGRAPH


# ---------------------------------------------------------------------------
# Classification against the fixture catalog
# ---------------------------------------------------------------------------

class TestClassification:
    def test_status_per_step(self, results):
        assert {r.step.number: r.status for r in results} == {
            1: dr.MISSING, 2: dr.PARTIAL, 3: dr.MISSING, 4: dr.MISSING,
            5: dr.PARTIAL, 6: dr.MISSING, 7: dr.WORKS, 8: dr.MISSING,
        }

    def test_every_missing_step_names_its_owner(self, results):
        owners = {r.step.number: r.owner for r in results if r.status == dr.MISSING}
        assert "dictation.py" in owners[1] and "wake_word_config" in owners[1]
        assert "plugins/commands/windows.py" in owners[3]
        assert "app_verbs.py" in owners[4]
        assert "text_marker.py" in owners[6]
        assert "GLOBAL_SESSION_EXIT_PHRASES" in owners[8]

    def test_step2_resolves_to_smtc_play_not_a_playlist(self, results):
        r = results[1]
        assert r.resolves_to.startswith("'play'")
        assert "command mode" in r.missing            # the entry lane would type it
        assert "playlist" in r.missing

    def test_step3_names_the_destination_grammar(self, results):
        r = results[2]
        assert r.resolves_to.startswith("'send'")
        assert "left/right screen" in r.missing

    def test_step5_is_the_dictate_lane(self, results):
        r = results[4]
        assert r.mode_required == "dictate"
        assert "'end'" in r.missing and "obsidian" in r.missing.lower()

    def test_step7_scratch_that_works_in_any_mode(self, results):
        assert results[6].status == dr.WORKS and results[6].mode_required == "any latched mode"

    def test_sleep_works_once_configured_as_abort_phrase(self, matcher):
        cfg = json.loads(json.dumps(FIXTURE_CONFIG))
        cfg["command_mode"]["abort_phrases"] = ["go to sleep"]
        res = dr.rehearse(matcher, cfg)
        assert res[7].status == dr.WORKS and "exit_command_mode" in res[7].resolves_to

    def test_wake_word_that_matches_is_partial_not_works(self, matcher):
        cfg = json.loads(json.dumps(FIXTURE_CONFIG))
        cfg["wake_word_config"]["phrase_options"].append("wake up samsara")
        res = dr.rehearse(matcher, cfg)
        assert res[0].status == dr.PARTIAL
        assert "one-command wake window" in res[0].resolves_to

    def test_command_step_works_when_grammar_and_lane_line_up(self, matcher, monkeypatch):
        """A WORKS row: the take spoken in the COMMAND lane with a parseable
        destination -- the classifier can say yes, not only no."""
        monkeypatch.setattr(dr, "TAKE", (dr.Step(3, "put warp on monitor 2", "command", "move"),))
        res = dr.rehearse(matcher, FIXTURE_CONFIG, entry_mode=SessionMode.COMMAND)
        assert res[0].status == dr.WORKS and res[0].live_text == "put warp on monitor 2"

    def test_command_step_partial_when_only_the_lane_is_wrong(self, matcher, monkeypatch):
        monkeypatch.setattr(dr, "TAKE", (dr.Step(3, "put warp on monitor 2", "command", "move"),))
        res = dr.rehearse(matcher, FIXTURE_CONFIG)          # entry lane: dictate
        assert res[0].status == dr.PARTIAL and "command mode" in res[0].missing


# ---------------------------------------------------------------------------
# Argument grammars and the lane model
# ---------------------------------------------------------------------------

class TestGrammars:
    @pytest.mark.parametrize("remainder,expected", [
        ("warp to monitor 2", dr.WORKS),
        ("chrome to the tv", dr.WORKS),
        ("this here", dr.WORKS),
        ("warp on the left screen and claude on the right", dr.MISSING),
        ("warp to the left screen", dr.MISSING),
    ])
    def test_send_destination_grammar(self, remainder, expected):
        assert dr._args_send(remainder, {})[0] == expected

    def test_play_music_grammar(self):
        assert dr._args_play_music("", {})[0] == dr.PARTIAL
        assert dr._args_play_music("my jam", {"music_library": {"My Jam": "spotify:x"}})[0] == dr.WORKS
        status, _d, missing, owner = dr._args_play_music("something from my alternative rock playlist", {})
        assert status == dr.PARTIAL and "playlist" in missing and "music.py" in owner

    def test_play_resume_ignores_its_remainder(self):
        assert dr._args_play_resume("", {})[0] == dr.WORKS
        assert dr._args_play_resume("anything", {})[0] == dr.PARTIAL


class TestLaneModel:
    @pytest.mark.parametrize("text,mode,kind,phrase", [
        ("put warp on monitor 2", SessionMode.COMMAND, "command", "send"),
        ("put warp on monitor 2", SessionMode.DICTATE, "dictation", None),
        ("switch window", SessionMode.DICTATE, "command", "switch window"),
        ("focus obsidian", SessionMode.DICTATE, "command", "focus"),
        ("show numbers", SessionMode.DICTATE, "command", "show numbers"),
        ("some words to type", SessionMode.COMMAND, "miss", None),
        ("some words to type", SessionMode.AVA, "agent", None),
        ("scratch that", SessionMode.DICTATE, "scratch", None),
        ("stop listening", SessionMode.COMMAND, "abort", None),
        ("dictate mode", SessionMode.COMMAND, "switch", None),
        ("hey ava", SessionMode.DICTATE, "switch", None),
        ("literal switch window", SessionMode.DICTATE, "dictation", None),
    ])
    def test_resolve_in_session(self, matcher, text, mode, kind, phrase):
        lane = dr.resolve_in_session(text, mode, matcher, FIXTURE_CONFIG)
        assert lane.kind == kind
        if phrase is not None:
            assert lane.phrase == phrase

    def test_switch_word_moves_the_simulated_mode(self, matcher, monkeypatch):
        monkeypatch.setattr(dr, "TAKE", (
            dr.Step(1, "command mode", "command", "switch"),
            dr.Step(2, "put warp on monitor 2", "command", "move"),
        ))
        res = dr.rehearse(matcher, FIXTURE_CONFIG)
        assert res[1].status == dr.WORKS

    def test_prefix_copy_matches_dictation_py(self):
        """The one constant copied out of dictation.py must not drift."""
        import dictation  # noqa: PLC0415  (tests may; the tool must not)
        assert dr.HANDS_FREE_COMMIT_PREFIXES == dictation._HANDS_FREE_COMMIT_PREFIXES

    def test_disabled_pack_hides_its_commands(self):
        m = dr.build_matcher(FIXTURE_CATALOG, enabled_packs={"core"})
        lane = dr.resolve_in_session("show numbers", SessionMode.COMMAND, m, FIXTURE_CONFIG)
        assert lane.kind == "miss"


# ---------------------------------------------------------------------------
# Output, determinism, hygiene
# ---------------------------------------------------------------------------

class TestOutput:
    def test_report_is_deterministic(self, tmp_path):
        catalog = tmp_path / "catalog.json"
        catalog.write_text(json.dumps({"commands": FIXTURE_CATALOG}), encoding="utf-8")
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps(FIXTURE_CONFIG), encoding="utf-8")
        outs = []
        for i in range(2):
            out = tmp_path / f"r{i}.md"
            assert dr.main(["--catalog", str(catalog), "--config", str(cfg), "--out", str(out)]) == 0
            outs.append(out.read_text(encoding="utf-8"))
        assert outs[0] == outs[1]
        assert "| step | utterance | resolves to | mode required | status | what is missing (owner) |" in outs[0]
        assert "Result: 1 WORKS, 2 PARTIAL, 5 MISSING of 8 steps." in outs[0]
        assert "Catalog: 10 commands (4 builtin, 6 plugin)" in outs[0]

    def test_rows_have_the_documented_columns(self, results):
        row = results[0].as_row()
        assert set(row) == {"step", "utterance", "resolves_to", "mode_required", "status", "missing", "owner"}

    def test_live_refuses_without_a_tty_or_samsara(self, monkeypatch):
        assert dr.live_preconditions(is_interactive=False, running=True)
        assert dr.live_preconditions(is_interactive=True, running=False)
        assert dr.live_preconditions(is_interactive=True, running=True) is None

    def test_live_flag_refuses_under_pytest_and_executes_nothing(self, tmp_path, monkeypatch):
        catalog = tmp_path / "catalog.json"
        catalog.write_text(json.dumps({"commands": FIXTURE_CATALOG}), encoding="utf-8")
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(dr, "run_live", lambda *a, **k: pytest.fail("run_live must not be reached"))
        rc = dr.main(["--catalog", str(catalog), "--config", str(tmp_path / "none.json"),
                      "--out", str(tmp_path / "r.md"), "--live"])
        assert rc == 2

    def test_never_imports_dictation_at_module_level(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert all(a.name != "dictation" for a in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "dictation"
        # And transitively: importing the tool in a fresh interpreter never loads it.
        code = "import sys; sys.path.insert(0, %r); import tools.demo_rehearsal; print('dictation' in sys.modules)" % str(ROOT)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "False", out.stderr[-500:]
