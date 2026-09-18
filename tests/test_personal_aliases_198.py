"""Queue 198: user aliases learn only from a MISS followed by a command."""

import ast
from pathlib import Path
from types import SimpleNamespace

from samsara import command_catalog as catalog
from samsara.session_modes import DispatchOutcome, outcome_chip


ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = Path(r"C:\Users\Morne\Documents\Claude\reports\198\artifacts")


class _Entry:
    def __init__(self, phrase):
        self.phrase = phrase
        self.aliases = []


class _Matcher:
    def __init__(self):
        entry = _Entry("tab one")
        self._entries = {"tab one": entry}
        self._match_table = []


def _app():
    return SimpleNamespace(config={}, _personal_alias_offer=None)


def test_miss_followed_by_command_offers_the_canonical_phrase():
    app = _app()
    assert catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="command_miss"), "tab won", now=100.0
    ) is None
    offer = catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="command_executed", detail={"phrase": "tab one"}), "tab one", now=101.0
    )
    assert offer["miss"] == "tab won"
    assert offer["canonical"] == "tab one"
    assert outcome_chip("personal_alias_offer", offer)[0].endswith("say save alias")


def test_timeout_and_dictation_do_not_offer_an_alias():
    app = _app()
    catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="command_miss"), "tab won", now=100.0
    )
    assert catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="command_executed", detail={"phrase": "tab one"}), "tab one", now=109.0
    ) is None

    catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="command_miss"), "tab won", now=110.0
    )
    assert catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="dictate_injected"), "ordinary dictation", now=111.0
    ) is None
    assert app._personal_alias_offer is None

    catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="command_miss"), "tab won", now=112.0
    )
    assert catalog.personal_alias_offer_after_outcome(
        app, DispatchOutcome(kind="command_miss"), "no thanks", now=113.0
    ) is None
    assert app._personal_alias_offer is None


def test_persistence_reload_and_removal_are_per_user(tmp_path):
    assert catalog.save_user_alias("tab won", "tab one", tmp_path)
    assert catalog.load_user_aliases(tmp_path) == {"tab won": "tab one"}

    matcher = _Matcher()
    assert catalog.install_user_aliases(matcher, tmp_path) == 1
    assert matcher._entries["tab won"].phrase == "tab one"
    assert "tab won" in matcher._entries["tab one"].aliases
    assert catalog.remove_user_alias("tab won", tmp_path, matcher)
    assert "tab won" not in matcher._entries
    assert catalog.load_user_aliases(tmp_path) == {}


def test_catalog_marks_personal_aliases_as_yours(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "user_aliases_path", lambda _home=None: tmp_path / "user_aliases.json")
    assert catalog.save_user_alias("tab won", "tab one")
    assert catalog.display_aliases({"phrase": "tab one", "aliases": ["tab one", "tab won"]}) == [
        "tab won (yours)"
    ]


def test_cheatsheet_personal_alias_row_carries_muted_foreground_role(
        qapp, monkeypatch, tmp_path):
    from PySide6.QtCore import Qt
    from samsara.ui import command_cheatsheet_qt as cheatsheet
    from samsara.ui import theme

    row = {
        "phrase": "tab one",
        "aliases": ["tab one", "tab won"],
        "shown_aliases": ["tab won (yours)"],
        "canonical_id": "builtin.tab_one",
        "plugin": "builtin",
        "pack": "core",
        "risk": "ui",
        "whole_utterance": False,
    }
    monkeypatch.setattr(cheatsheet, "catalog_rows", lambda _commands: [row])
    monkeypatch.setattr(cheatsheet, "_disabled_packs", lambda: set())
    win = cheatsheet._CheatSheetWindow(
        lambda _phrase: None, lambda: object(), tmp_path / "palette.json")
    try:
        item = win._list.item(0)
        assert "tab won (yours)" in item.text()
        assert item.data(Qt.ItemDataRole.ForegroundRole) == theme.qcolor(
            theme.TEXT_SECONDARY)
    finally:
        win.deleteLater()


def test_boot_loader_and_voice_handler_install_and_remove_aliases(tmp_path, monkeypatch):
    from plugins.commands import core_utils
    from samsara import plugin_commands

    matcher = _Matcher()
    app = SimpleNamespace(
        config_path=tmp_path / "config.json",
        command_executor=SimpleNamespace(_matcher=matcher),
        _personal_alias_offer={"miss": "tab won", "canonical": "tab one", "expires": float("inf")},
    )
    monkeypatch.setattr(plugin_commands, "_shared_matcher", matcher)
    core_utils.personal_alias(app)
    assert matcher._entries["tab won"].phrase == "tab one"

    reloaded = _Matcher()
    monkeypatch.setattr(plugin_commands, "_shared_matcher", reloaded)
    core_utils.start_services(app)
    assert reloaded._entries["tab won"].phrase == "tab one"

    app.command_executor = SimpleNamespace(_matcher=reloaded)
    core_utils.personal_alias(app, "tab won")
    assert "tab won" not in reloaded._entries


def test_dispatcher_contains_only_the_cross_utterance_offer_seam():
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    method = next(
        node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "DictationApp"
        for node in cls.body if isinstance(node, ast.FunctionDef)
        and node.name == "_handle_session_dispatch_outcome"
    )
    source = ast.unparse(method)
    assert "personal_alias_offer_after_outcome" in source
    assert "_show_outcome_chip(*chip, 8000)" in source


def test_offer_chip_grabs_in_light_and_dark(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel
    from samsara.ui import theme

    original = theme.active_theme()
    try:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        label, _kind = outcome_chip("personal_alias_offer", {"miss": "tab won", "canonical": "tab one"})
        for palette in ("light", "dark"):
            theme.set_theme(palette, refresh=False)
            chip = QLabel(label)
            chip.setWordWrap(True)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setFixedSize(520, 64)
            chip.setStyleSheet(
                f"background:{theme.ACCENT_DIM};border:1px solid {theme.ACCENT};"
                f"border-radius:10px;color:{theme.TEXT_PRIMARY};padding:8px;"
            )
            image = chip.grab()
            assert not image.isNull()
            assert image.save(str(ARTIFACTS / f"personal_alias_offer_{palette}.png"))
    finally:
        theme.set_theme(original, refresh=False)
