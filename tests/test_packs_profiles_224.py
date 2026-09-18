"""Queue 224: honest command packs, discoverable profiles, and one dictionary bundle."""

import json
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QFileDialog, QLabel

from samsara.command_packs import PACKS
from samsara.profiles import ProfileManager
from samsara.ui import theme
from samsara.ui.profile_manager_qt import _ProfileManagerWindow
from samsara.ui.settings.commands_qt import CommandsPage


ARTIFACTS = Path(r"C:\Users\Morne\Documents\Claude\reports\224\artifacts")


def _write(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _commands_host(tmp_path):
    commands_path = tmp_path / "commands.json"
    _write(commands_path, {"commands": {"say hello": {"type": "text", "pack": "core"}}})
    executor = SimpleNamespace(
        commands={
            "say hello": {"type": "text", "pack": "core", "description": "Types hello."},
            "open discord": {"type": "launch", "pack": "utilities", "description": "Opens Discord."},
        },
        commands_path=commands_path,
        _matcher=None,
    )
    host = CommandsPage()
    host.app = SimpleNamespace(config={}, config_path=tmp_path / "config.json", command_executor=executor)
    host._widgets = {}
    host._save_fns = []
    host._section_title = lambda text: QLabel(text)
    return host


def test_mouse_pack_is_removed_and_discord_explains_setup():
    assert "mouse" not in PACKS
    assert PACKS["discord"]["description"] == "Needs a Discord webhook — Set up"


def test_commands_page_has_no_empty_pack_count_and_has_profiles_entry(qapp, tmp_path):
    host = _commands_host(tmp_path)
    page = host._build_commands_tab()
    try:
        labels = [label.text() for label in page.findChildren(QLabel)]
        assert not any("(0)" in text for text in labels)
        assert any(text.startswith("Discord") for text in labels)
        assert any("Needs a Discord webhook" in text for text in labels)
        assert host._widgets["_command_profiles_button"].text() == "Manage Profiles…"
        assert any(text.startswith("Profiles: export / import / manage") for text in labels)
    finally:
        page.deleteLater()


def test_default_command_profile_is_read_only_revert_and_backup(tmp_path):
    commands_path = tmp_path / "commands.json"
    original = {"commands": {"say hello": {"type": "text", "text": "hello"}}}
    _write(commands_path, original)
    pm = ProfileManager(tmp_path, commands_path=commands_path)
    assert pm.list_command_profiles() == ["Default"]
    _write(commands_path, {"commands": {"custom": {"type": "text", "text": "custom"}}})

    ok, message = pm.load_command_profile("Default")
    assert ok and "backup" in message.lower()
    assert json.loads(commands_path.read_text(encoding="utf-8")) == original
    assert list(tmp_path.glob("commands.json.bak-*"))
    assert pm.save_command_profile("Default")[0] is False
    assert pm.delete_command_profile("Default")[0] is False


def test_dictionary_bundle_carries_all_four_stores_and_replace_backs_up(tmp_path):
    _write(tmp_path / "training_data.json", {
        "vocabulary": ["Morne"], "corrections": {"teh": "the"},
    })
    _write(tmp_path / "user_wake_corrections.json", {"charvis": "jarvis"})
    _write(tmp_path / "user_aliases.json", {"version": 1, "aliases": {"tab won": "tab one"}})
    pm = ProfileManager(tmp_path)
    bundle_path = tmp_path / "export.json"
    ok, _ = pm.export_dictionary_bundle(bundle_path)
    assert ok
    exported = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert exported["format"] == "samsara.dictionary"
    assert exported["version"] == 1
    assert exported["vocabulary"] == ["Morne"]
    assert exported["corrections"] == {"teh": "the"}
    assert exported["wake_word_corrections"] == {"charvis": "jarvis"}
    assert exported["personal_aliases"] == {"tab won": "tab one"}

    incoming = dict(exported)
    incoming["vocabulary"] = ["NewName"]
    incoming["corrections"] = {"teh": "a different correction", "new": "word"}
    incoming["wake_word_corrections"] = {"new wake": "jarvis"}
    incoming["personal_aliases"] = {"new alias": "say hello"}
    _write(bundle_path, incoming)
    ok, message = pm.import_dictionary_bundle(bundle_path, mode="merge")
    assert ok and "conflicting" in message
    assert json.loads((tmp_path / "training_data.json").read_text(encoding="utf-8"))["corrections"]["teh"] == "the"

    ok, message = pm.import_dictionary_bundle(bundle_path, mode="replace")
    assert ok and "backup" in message.lower()
    assert json.loads((tmp_path / "training_data.json").read_text(encoding="utf-8"))["vocabulary"] == ["NewName"]
    assert list((tmp_path / "backups").glob("dictionary-*"))


def test_profile_manager_shows_default_as_read_only(qapp, tmp_path):
    commands_path = tmp_path / "commands.json"
    _write(commands_path, {"commands": {"say hello": {"type": "text"}}})
    pm = ProfileManager(tmp_path, commands_path=commands_path)
    window = _ProfileManagerWindow(pm)
    try:
        assert window._cmd_combo.itemText(0) == "Default"
        assert any("read-only" in label.text().lower() for label in window.findChildren(QLabel))
    finally:
        window.deleteLater()


def test_dark_and_light_widget_grabs_are_real_widget_evidence(qapp, tmp_path):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    original_theme = theme.active_theme()
    try:
        for palette in ("dark", "light"):
            theme.set_theme(palette, refresh=False)
            palette_tmp = tmp_path / palette
            palette_tmp.mkdir(exist_ok=True)
            host = _commands_host(palette_tmp)
            page = host._build_commands_tab()
            page.resize(760, 720)
            page.widget().layout().activate()
            qapp.processEvents()
            pack_scroll = host._widgets["_pack_scroll"]
            pack_scroll.resize(700, 270)
            profile_entry = host._widgets["_command_profiles_entry"]
            profile_entry.resize(700, 70)
            assert page.grab().save(str(ARTIFACTS / f"commands_page_{palette}.png"))
            assert pack_scroll.grab().save(str(ARTIFACTS / f"pack_list_{palette}.png"))
            assert profile_entry.grab().save(str(ARTIFACTS / f"profiles_entry_{palette}.png"))

            dialog = QFileDialog()
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setWindowTitle("Export dictionary")
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setNameFilter("Samsara dictionary (*.json)")
            dialog.resize(700, 480)
            assert dialog.grab().save(str(ARTIFACTS / f"dictionary_export_dialog_{palette}.png"))
            dialog.deleteLater()
            page.deleteLater()
            qapp.processEvents()
    finally:
        theme.set_theme(original_theme, refresh=False)
