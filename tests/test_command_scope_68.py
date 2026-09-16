"""Queue 68 -- app-scoped command sets.

  * a scoped command matches only when its app / title / tags are live
  * unresolvable foreground, Samsara's own window, a failing provider: the
    documented fallback (app-scoped not candidates, logged), globals untouched
  * every unscoped phrase of the LIVE registry matches exactly as before, in
    every context
  * the window-cube numbers are not candidates while the cube is hidden;
    the shadow utterance "To, um..." no longer resolves to window_cube.two
  * the catalog carries the scope and regenerates byte-identically
  * the cheat sheet shows what is live here

Never imports dictation (Samsara may be running).
"""
import ctypes
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from samsara import command_catalog as cc  # noqa: E402
from samsara import command_scope as cs  # noqa: E402
from samsara.command_registry import CommandMatcher, INVALID_SCOPE_TAG  # noqa: E402

OBSIDIAN = cs.MatchContext.for_app("obsidian.exe", title="Daily note - Vault - Obsidian v1.9")
WARP = cs.MatchContext.for_app("warp.exe", title="warp")
BRAVE = cs.MatchContext.for_app("brave.exe", title="GitHub - Brave")
CLAUDE = cs.MatchContext.for_app("claude.exe", title="Claude")
UNRESOLVED = cs.MatchContext.unresolved(cs.UNRESOLVED_NO_WINDOW)
OWN = cs.MatchContext(exe="python.exe", resolved=False, own_window=True, reason=cs.OWN_WINDOW)
ALL_CONTEXTS = [OBSIDIAN, WARP, BRAVE, CLAUDE, UNRESOLVED, OWN]


@pytest.fixture(autouse=True)
def _fresh_scope_state():
    cs.reset_for_tests()
    yield
    cs.reset_for_tests()


def _matcher(commands, provider=None):
    m = CommandMatcher()
    m.load_builtins(commands)
    if provider is not None:
        m.set_context_provider(provider)
    m.freeze()
    return m


COMMANDS = {
    "open palette": {"type": "hotkey", "keys": ["ctrl", "p"], "scope": {"apps": ["obsidian.exe"]}},
    "next note": {"type": "hotkey", "keys": ["ctrl", "tab"],
                  "scope": {"apps": ["obsidian.exe"], "title": r" - Obsidian v"}},
    "open": {"type": "hotkey", "keys": ["ctrl", "o"]},
    "save": {"type": "hotkey", "keys": ["ctrl", "s"]},
}


# ---------------------------------------------------------------------------
# Scope syntax
# ---------------------------------------------------------------------------

class TestScopeSyntax:
    def test_parse_forms(self):
        assert cs.parse_scope(None) is None and cs.parse_scope({}) is None
        s = cs.parse_scope({"apps": "Obsidian.EXE", "title": "Vault", "tags": ["a"]})
        assert s.apps == {"obsidian.exe"} and s.title == "Vault" and s.tags == {"a"}
        assert s.to_json() == {"apps": ["obsidian.exe"], "tags": ["a"], "title": "Vault"}
        assert cs.parse_scope({"app": "warp.exe"}).apps == {"warp.exe"}

    @pytest.mark.parametrize("bad", [
        {"apps": []} if False else {"apps": [""]}, {"title": "("}, {"window": "x"}, "obsidian.exe", {"tags": [3]},
    ])
    def test_malformed_scope_raises(self, bad):
        with pytest.raises(ValueError):
            cs.parse_scope(bad)

    def test_decorator_rejects_a_bad_scope_at_import(self):
        from samsara.plugin_commands import command
        with pytest.raises(ValueError):
            command("scoped thing", scope={"apps": "x", "colour": "blue"})

    def test_invalid_builtin_scope_is_loaded_but_never_live_and_logged(self, caplog):
        with caplog.at_level(logging.ERROR):
            m = _matcher({"odd": {"type": "hotkey", "keys": ["a"], "scope": {"title": "["}}})
        entry = m._entries["odd"]
        assert INVALID_SCOPE_TAG in entry.scope.tags
        assert all(m.match("odd", ctx)[0] is None for ctx in ALL_CONTEXTS)
        assert any("invalid command scope" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

class TestScopedMatching:
    def test_scoped_matches_only_in_its_app(self):
        m = _matcher(COMMANDS)
        assert m.match("open palette", OBSIDIAN)[0].phrase == "open palette"
        for ctx in (WARP, BRAVE, CLAUDE):
            assert m.match("open palette", ctx)[0] is None
            entry, why = m.out_of_scope_for("Open palette.", ctx)
            assert entry.phrase == "open palette" and ctx.exe in why

    def test_title_regex_is_required_too(self):
        m = _matcher(COMMANDS)
        assert m.match("next note", OBSIDIAN)[0].phrase == "next note"
        other_title = cs.MatchContext.for_app("obsidian.exe", title="Settings")
        assert m.match("next note", other_title)[0] is None

    def test_prefix_scan_skips_out_of_scope_entries(self):
        m = _matcher(COMMANDS)
        entry, remainder = m.match("open palette please", OBSIDIAN)
        assert (entry.phrase, remainder) == ("open palette", "please")
        # Not an exact out-of-scope phrase: the longest LIVE candidate wins,
        # exactly as a disabled pack's longer phrase is skipped (queue 58).
        entry, remainder = m.match("open palette please", WARP)
        assert (entry.phrase, remainder) == ("open", "palette please")

    def test_exact_out_of_scope_phrase_is_a_miss_not_a_shorter_command(self):
        m = _matcher(COMMANDS)
        assert m.match("open palette", WARP) == (None, "")

    @pytest.mark.parametrize("ctx", ALL_CONTEXTS, ids=lambda c: c.exe or c.reason)
    def test_unscoped_commands_are_unaffected(self, ctx):
        m = _matcher(COMMANDS)
        assert m.match("save", ctx)[0].phrase == "save"
        assert m.match("open the file", ctx)[0].phrase == "open"

    def test_nothing_scoped_costs_no_context(self):
        calls = []
        m = _matcher({"save": {"type": "hotkey", "keys": ["ctrl", "s"]}},
                     provider=lambda: calls.append(1) or WARP)
        assert m.current_context() is None
        assert m.match("save")[0].phrase == "save" and calls == []

    def test_live_phrase_count(self):
        m = _matcher(COMMANDS)
        assert m.live_phrase_count(OBSIDIAN) == (4, 4)
        assert m.live_phrase_count(WARP) == (2, 4)


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

class TestFallback:
    @pytest.mark.parametrize("ctx", [UNRESOLVED, OWN, cs.MatchContext.unresolved("process_name_unavailable:AccessDenied")],
                             ids=["no_window", "samsara_window", "elevated_or_protected"])
    def test_app_scoped_not_candidates_globals_live(self, ctx):
        m = _matcher(COMMANDS)
        assert m.match("open palette", ctx)[0] is None
        assert m.match("save", ctx)[0].phrase == "save"
        _entry, why = m.out_of_scope_for("open palette", ctx)
        assert why

    def test_failing_provider_logs_and_keeps_globals(self, caplog):
        def boom():
            raise RuntimeError("win32 gone")
        m = _matcher(COMMANDS, provider=boom)
        with caplog.at_level(logging.WARNING):
            assert m.match("open palette")[0] is None
            assert m.match("save")[0].phrase == "save"
        assert any("context provider failed" in r.getMessage() for r in caplog.records)

    def test_tag_scope_ignores_foreground_resolution(self):
        m = _matcher({"two": {"type": "hotkey", "keys": ["2"], "scope": {"tags": ["cube"]}}})
        on = cs.MatchContext.unresolved(cs.UNRESOLVED_NO_WINDOW, tags={"cube"})
        assert m.match("two", on)[0].phrase == "two"
        assert m.match("two", cs.MatchContext.for_app("warp.exe"))[0] is None


class _FakeUser32:
    def __init__(self, hwnd, pid, title=""):
        self.hwnd, self.pid, self.title = hwnd, pid, title

    def GetForegroundWindow(self):
        return self.hwnd

    def GetWindowThreadProcessId(self, hwnd, pid_ref):
        pid_ref._obj.value = self.pid
        return 1

    def GetWindowTextLengthW(self, hwnd):
        return len(self.title)

    def GetWindowTextW(self, hwnd, buf, n):
        buf.value = self.title
        return len(self.title)


class TestCaptureContext:
    def _patch(self, monkeypatch, user32, name=None, name_error=None):
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
        import psutil

        class _P:
            def __init__(self, pid):
                if name_error:
                    raise name_error
            def name(self):
                return name
        monkeypatch.setattr(psutil, "Process", _P)

    def test_no_foreground_window_is_unresolved_and_logged_once(self, monkeypatch, caplog):
        self._patch(monkeypatch, _FakeUser32(0, 0))
        with caplog.at_level(logging.INFO):
            a = cs.capture_context()
            b = cs.capture_context()
        assert not a.resolved and a.reason == cs.UNRESOLVED_NO_WINDOW and b.reason == a.reason
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SCOPE]")]
        assert len(lines) == 1 and "unresolved (no_foreground_window)" in lines[0]
        assert "unscoped and tag-scoped commands are unaffected" in lines[0]

    def test_process_name_unreadable_is_unresolved(self, monkeypatch):
        import psutil
        self._patch(monkeypatch, _FakeUser32(10, 4242, "Admin tool"), name_error=psutil.AccessDenied(4242))
        ctx = cs.capture_context()
        assert not ctx.resolved and ctx.reason.startswith(cs.UNRESOLVED_NO_NAME)

    def test_samsara_own_window(self, monkeypatch, caplog):
        self._patch(monkeypatch, _FakeUser32(10, os.getpid(), "Samsara Settings"), name="python.exe")
        with caplog.at_level(logging.INFO):
            ctx = cs.capture_context()
        assert ctx.own_window and not ctx.resolved
        assert any("Samsara's own window is focused" in r.getMessage() for r in caplog.records)

    def test_resolved_app_and_title_never_logged(self, monkeypatch, caplog):
        self._patch(monkeypatch, _FakeUser32(10, 999999, "Secret Doc - Obsidian v1.9"), name="Obsidian.exe")
        with caplog.at_level(logging.INFO):
            ctx = cs.capture_context()
        assert ctx.resolved and ctx.exe == "obsidian.exe" and ctx.title.startswith("Secret")
        assert not any("Secret" in r.getMessage() for r in caplog.records)
        assert cs.display_context().exe == "obsidian.exe"


# ---------------------------------------------------------------------------
# The live registry
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def executor():
    from tools.dump_command_metadata import build_executor
    return build_executor()


def _scope_free_copy(matcher):
    """The same registry with every scope removed: what matching was before."""
    import copy
    base = CommandMatcher()
    base._enabled_packs = matcher._enabled_packs
    clones = {}
    for phrase, entry in matcher._entries.items():
        if id(entry) not in clones:
            clone = copy.copy(entry)
            clone.scope = None
            clones[id(entry)] = clone
        base._entries[phrase] = clones[id(entry)]
    base.freeze()
    return base


def test_every_unscoped_phrase_matches_as_before_in_every_context(executor):
    m = executor._matcher
    base = _scope_free_copy(m)
    scoped_phrases = {e.phrase for e in m._sorted if e.scope is not None}
    checked, swallowed_before = 0, set()
    for phrase, entry in m._entries.items():
        if entry.scope is not None:
            continue
        for variant in (phrase, phrase + " something after", phrase.title() + "."):
            before = base.match_detail(variant)
            before = (before.entry.phrase, before.remainder) if before else None
            for ctx in ALL_CONTEXTS:
                now = m.match_detail(variant, ctx)
                now = (now.entry.phrase, now.remainder) if now else None
                if before is not None and before[0] in scoped_phrases:
                    # Before scoping a cube number prefix-matched this utterance
                    # ("three d print a gun ..." -> "three"). That is the false
                    # match scoping removes: it must not come back, and nothing
                    # else changes.
                    swallowed_before.add(variant)
                    assert now is None or now[0] not in scoped_phrases, (variant, now)
                    continue
                assert now == before, (variant, ctx.exe or ctx.reason)
        checked += 1
    assert checked > 900
    assert swallowed_before, "expected at least one utterance a cube number used to swallow"


def test_scoped_commands_are_exactly_the_cube_numbers_and_the_mouse_grid(executor):
    scoped = {e.phrase: e.scope.to_json() for e in executor._matcher._sorted if e.scope is not None}
    expected = {w: {"tags": ["window_cube.visible"]}
                for w in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")}
    # Queue 71 reuses this mechanism for the mouse grid's own phrases, so they
    # are candidates only while the grid is on screen.
    expected.update({p: {"tags": ["mouse_grid.visible"]}
                     for p in ("hide grid", "grid back", "move here")})
    assert scoped == expected


class TestWindowCubeNumbers:
    @pytest.fixture(autouse=True)
    def _cube(self):
        from plugins.commands import window_cube
        window_cube._reset_for_tests()
        yield window_cube
        window_cube._reset_for_tests()

    def test_not_candidates_while_hidden_candidates_while_shown(self, executor, _cube):
        m = executor._matcher
        for phrase in ("two", "2", "nine", "9"):
            assert m.match(phrase)[0] is None, phrase
            entry, why = m.out_of_scope_for(phrase)
            assert "window_cube.visible" in why
        _cube._pinned = True
        assert m.match("two")[0].phrase == "two" and m.match("2")[0].phrase == "two"
        assert m.match("window two")[0].phrase == "window two"     # global form, unaffected

    def test_process_text_out_of_scope_is_a_logged_miss_without_rebuild(self, executor, caplog, monkeypatch):
        rebuilds = []
        monkeypatch.setattr(executor, "rebuild_matcher", lambda: rebuilds.append(1))
        app = SimpleNamespace(command_matching_enabled=True, command_mode_active=False, config={})
        with caplog.at_level(logging.INFO):
            result = executor.process_text("Two.", app, force_commands=True)
        assert result.state.value == "miss"
        assert result.detail["reason"] == "out_of_scope" and result.detail["phrase"] == "two"
        assert rebuilds == [], "an out-of-scope phrase must not look like registry drift"
        assert any("'two' is only while the window cube is on screen" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("utterance", ["To, um...", "To..."])
    def test_shadow_utterance_no_longer_resolves_to_cube_two(self, executor, utterance, _cube):
        from samsara.intent.resolve import IntentResolver
        from samsara.phonetic_wash import apply_phonetic_wash
        resolver = IntentResolver(rows=executor._matcher.list_commands())
        claude_hidden = cs.MatchContext.for_app("claude.exe")
        claude_shown = cs.MatchContext.for_app("claude.exe", tags={"window_cube.visible"})

        now = resolver.resolve(utterance, context=claude_hidden)
        assert now.canonical_id != "window_cube.two", now
        shown = resolver.resolve(utterance, context=claude_shown)
        assert shown.kind == "dictation" and shown.blocked == "one_word_utterance"
        # The real matcher does not claim it either.
        assert executor._matcher.match(apply_phonetic_wash(utterance))[0] is None

    def test_shadow_record_uses_the_state_at_observe_time(self, executor, tmp_path, _cube):
        from samsara.intent.resolve import IntentResolver
        from samsara.intent.shadow import IntentShadow
        resolver = IntentResolver(rows=executor._matcher.list_commands())
        shadow = IntentShadow(lambda: resolver, lambda: {"intent": {"shadow_dir": str(tmp_path)}},
                              spawn=lambda *a, **k: None, name_fn=lambda pid: "claude.exe")
        from datetime import datetime
        hidden = shadow.record("To, um...", "staged", datetime(2026, 9, 15, 3, 0), 1234, frozenset())
        shown = shadow.record("To, um...", "staged", datetime(2026, 9, 15, 3, 0), 1234,
                              frozenset({"window_cube.visible"}))
        assert hidden["would"] != "command:window_cube.two" and hidden["app"] == "claude.exe"
        assert shown["would"] == "dictate"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_catalog_carries_scope_and_regenerates_identically(executor):
    specs = cc.build_catalog(executor)
    doc = cc.to_document(specs)
    assert cc.validate_catalog(doc) == []
    scoped = {c["canonical_id"]: c["scope"] for c in doc["commands"] if "scope" in c}
    # 9 window-cube numbers + queue 71's 3 mouse-grid phrases.
    assert len(scoped) == 12 and scoped["window_cube.two"] == {"tags": ["window_cube.visible"]}
    assert scoped["show_numbers.hide_grid"] == {"tags": ["mouse_grid.visible"]}


def test_catalog_validation_rejects_bad_scope():
    good = json.loads((ROOT / "commands_catalog.json").read_text(encoding="utf-8"))
    rec = dict(good["commands"][0], scope={"title": "("})
    problems = cc.validate_catalog({"version": 1, "commands": [rec]})
    assert any("scope" in p for p in problems)


# ---------------------------------------------------------------------------
# Cheat sheet
# ---------------------------------------------------------------------------

def test_cheatsheet_annotation_and_filter():
    from samsara.ui import command_cheatsheet_qt as sheet
    rows = [{"phrase": "save", "canonical_id": "b.save", "risk": "write", "scope": None},
            {"phrase": "two", "canonical_id": "window_cube.two", "risk": "ui",
             "scope": {"tags": ["window_cube.visible"]}}]
    out = sheet.annotate_scope(rows, cs.MatchContext.for_app("warp.exe"))
    assert out[0]["live"] and out[0]["scope_note"] == ""
    assert not out[1]["live"] and out[1]["scope_note"] == "only while the window cube is on screen"
    assert "not live here" in sheet.row_text(out[1])
    shown, hidden = sheet.live_filter(out, True)
    assert [r["phrase"] for r in shown] == ["save"] and hidden == 1
    assert sheet.live_filter(out, False) == (out, 0)


def test_cheatsheet_window_hides_not_live_and_says_how_many(qapp, executor, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from samsara.ui import command_cheatsheet_qt as sheet
    monkeypatch.setattr(sheet, "_scope_context", lambda: cs.MatchContext.for_app("warp.exe"))
    rows = executor._matcher.list_commands()
    win = sheet._CheatSheetWindow(lambda p: None, lambda: rows, tmp_path / "palette.json")
    try:
        ids = {win._list.item(i).data(Qt.ItemDataRole.UserRole + 1) for i in range(win._list.count())}
        assert "window_cube.two" not in ids and "window_cube.window_two" in ids
        # 9 window-cube numbers + queue 71's 3 mouse-grid phrases.
        assert win._category_bar._hidden_lbl.text() == "(12 not live here)"
        win._category_bar._live_only.setChecked(False)
        ids = {win._list.item(i).data(Qt.ItemDataRole.UserRole + 1) for i in range(win._list.count())}
        assert "window_cube.two" in ids and win._category_bar._hidden_lbl.text() == ""
    finally:
        win.deleteLater()
