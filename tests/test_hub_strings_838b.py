"""String regressions for the Home, Hub and Dictionary surfaces (838b)."""

import ast
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QLabel, QPushButton, QToolButton, QWidget

from samsara.dictionary_store import DictionaryStore
from samsara.ui import home_notice_qt, home_qt, home_signals
from samsara.ui import dictionary_panel_qt


REPO = Path(__file__).resolve().parents[1]
SINGLE_AMPERSAND = re.compile(r"(?<!&)&(?!&)")


def _literal_strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                yield part.value


def test_button_label_ampersands_are_escaped_in_ui_sources(qapp):
    control_calls = {"QPushButton", "QToolButton", "QCheckBox", "QRadioButton", "QAction"}
    for path in (REPO / "samsara" / "ui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        button_names = set()
        nodes = list(ast.walk(tree))
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Call):
                    name = value.func.id if isinstance(value.func, ast.Name) else None
                    if name in control_calls:
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if isinstance(target, ast.Name):
                                button_names.add(target.id)
                            elif isinstance(target, ast.Attribute):
                                button_names.add(target.attr)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name in control_calls and node.args:
                for text in _literal_strings(node.args[0]):
                    assert not SINGLE_AMPERSAND.search(text), (path, node.lineno, text)
            is_button_text = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "setText"
                and isinstance(node.func.value, (ast.Name, ast.Attribute))
                and (node.func.value.id if isinstance(node.func.value, ast.Name)
                     else node.func.value.attr) in button_names
            )
            if is_button_text and node.args:
                for text in _literal_strings(node.args[0]):
                    assert not SINGLE_AMPERSAND.search(text), (path, node.lineno, text)

    # Home passes this table label to QPushButton at runtime.
    source_label = next(label for label, *_ in home_qt.GUIDES if "support" in label.lower())
    button = QPushButton(source_label)
    assert button.text() == "Help && support"


def test_microphone_copy_uses_the_registered_audio_page_and_row():
    from samsara.ui.settings_qt import PAGE_MODULES

    audio_module = next(
        module for module in PAGE_MODULES
        if "open_mic_setup_guide" in inspect.getsource(module.build)
    )
    page_title = audio_module.TITLE
    tree = ast.parse(Path(audio_module.__file__).read_text(encoding="utf-8"))
    row_label = next(
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "SettingRow"
        and node.args and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "Microphone"
    )
    for state in (
        home_signals.dictation_state(SimpleNamespace(available_mics=None, config={})),
        home_signals.dictation_state(SimpleNamespace(available_mics=[], config={})),
        home_signals.hands_free_state(SimpleNamespace(
            available_mics=None, config={"wake_word_enabled": True})),
        home_signals.hands_free_state(SimpleNamespace(
            available_mics=[], config={"wake_word_enabled": True})),
    ):
        assert f"Settings > {page_title} > {row_label}" in state.detail or \
            f"Settings > {page_title} > {row_label}" in state.evidence

    app = SimpleNamespace(config={}, available_mics=[])
    notice = home_signals.problem_notice(app)
    card, text, button = QWidget(), QLabel(), QPushButton()
    home_notice_qt.refresh_problem_notice(
        card, text, button, notice, home_signals.DICTATION, home_qt.OPEN_VOICE_HELP)
    assert f"Settings > {page_title} > {row_label}" in text.text()


@pytest.mark.parametrize("state", [
    home_signals.AVA_ASLEEP, home_signals.AVA_AWAKE, home_signals.AVA_DIM,
    home_signals.AVA_CHECKING, home_signals.AVA_THINKING, home_signals.AVA_WARMING,
])
def test_ava_header_does_not_repeat_its_state_word(qapp, monkeypatch, state):
    word = home_qt.AVA_SHORT_STATE[state]
    monkeypatch.setattr(
        home_signals, "ava_presence",
        lambda _app: (state, f"Ava is {word}."))
    monkeypatch.setattr(home_signals, "ava_route_label", lambda _app: f"Ava: {word}")
    control = home_qt.AvaHeaderControl(SimpleNamespace())
    assert control.visible_text == f"Ava: {word}"
    assert control.visible_text.lower().split().count(word) == 1
    control.close()


def test_home_count_uses_three_vocabulary_entries_not_corrections():
    app = SimpleNamespace(dictionary_store=SimpleNamespace(
        custom_vocab=["Samsara", "Whisper", "Ollama"],
        corrections_dict={f"wrong{i}": f"right{i}" for i in range(15)},
    ))
    assert home_qt._user_word_count(app) == 3


def test_dictionary_more_button_uses_the_centered_dropdown_copy(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(dictionary_panel_qt, "QSettings", lambda *args: QSettings(
        str(tmp_path / "dictionary.ini"), QSettings.Format.IniFormat))
    (tmp_path / "training_data.json").write_text(
        '{"vocabulary": [], "corrections": {}}', encoding="utf-8")
    app = SimpleNamespace(config={}, config_path=tmp_path / "config.json")
    app.dictionary_store = DictionaryStore(app)
    panel = dictionary_panel_qt.DictionaryPanelQt(app)
    more = next(button for button in panel.findChildren(QToolButton)
                if button.accessibleName() == "More dictionary actions")
    assert more.text() == "More ▾"
    assert "padding-right: 8px" in more.styleSheet()
    panel.close()

