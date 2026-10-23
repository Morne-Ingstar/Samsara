"""801: shipped words share the decoder seam, never the personal dictionary."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara.builtin_vocabulary import builtin_entries, prompt_words, relevant_words
from samsara.command_packs import PACKS
from samsara.dictionary_store import DictionaryStore


def store(tmp_path, config=None, words=()):
    (tmp_path / "training_data.json").write_text(
        json.dumps({"vocabulary": list(words), "corrections": {}}), encoding="utf-8")
    app = SimpleNamespace(config_path=tmp_path / "config.json", config=config or {})
    return DictionaryStore(app)


def test_merge_user_wins_and_no_personal_list_pollution(tmp_path):
    dictionary = store(tmp_path, words=["gPt", "MyTerm"])
    before = (tmp_path / "training_data.json").read_bytes()
    prompt = dictionary.get_initial_prompt()
    assert "Common terms: gPt, MyTerm" in prompt
    assert "GPT" not in prompt.split("Built-in words: ")[1].split(", ")
    assert "Samsara" in prompt and "notepad" in prompt
    assert dictionary.custom_vocab == ["gPt", "MyTerm"]
    assert dictionary.corrections_dict == {}
    assert dictionary.save_training_data()
    assert (tmp_path / "training_data.json").read_bytes() == before


def test_disable_persists_without_disabling_explicit_user_word(tmp_path):
    dictionary = store(tmp_path)
    assert dictionary.set_builtin_word_enabled("NOTEPAD", False)
    assert "notepad" not in dictionary.get_initial_prompt().lower()
    assert "notepad" not in DictionaryStore(dictionary.app).get_initial_prompt().lower()
    assert dictionary.set_builtin_word_enabled("notepad", True)
    assert "notepad" in dictionary.get_initial_prompt()
    assert dictionary.set_builtin_word_enabled("notepad", False)
    dictionary.custom_vocab.append("Notepad")
    assert "Common terms: Notepad" in dictionary.get_initial_prompt()
    assert not dictionary.set_builtin_word_enabled("not-a-built-in", False)


def test_optout_also_filters_auto_derived_terms(tmp_path):
    dictionary = store(tmp_path, {"web_shortcuts": {"youtube": "unused"}})
    assert "youtube" in dictionary.get_initial_prompt()
    assert dictionary.set_builtin_word_enabled("youtube", False)
    assert "youtube" not in dictionary.get_initial_prompt()


def test_wake_phrase_is_dynamic_first_and_pack_filtered(tmp_path):
    config = {"wake_word_config": {"phrase": "Hello Computer"},
              "command_packs": {name: False for name in PACKS}}
    words = relevant_words(config, [])
    assert words[0] == "Hello Computer"
    assert "Samsara" in words and "scratch that" in words
    assert "Hermes" not in words and "youtube" not in words
    config["command_packs"]["hermes"] = True
    assert "Hermes" in relevant_words(config, [])
    assert builtin_entries(config)[0]["word"] == "Hello Computer"
    config["wake_word_config"]["phrase"] = "Computer"
    assert builtin_entries(config)[0]["word"] == "Computer"


def test_all_assistants_in_catalog_and_builtin_reasons():
    entries = builtin_entries({})
    words = {entry["word"] for entry in entries}
    assert {"Samsara", "Ava", "Hermes", "Claude", "GPT", "ChatGPT", "Codex",
            "Ollama", "OpenRouter", "DeepSeek", "hands-free", "show numbers",
            "cube", "scratch that", "Command Reference"} <= words
    assert all(entry["why"] and set(entry["packs"]) <= set(PACKS) for entry in entries)
    assert not any(name.casefold() in {word.casefold() for word in words}
                   for name in ("Morne", "Daily", "demo", "samsara-dev"))


def test_shipped_apps_and_sites_derived_from_sources():
    root = Path(__file__).resolve().parents[1]
    words = {entry["word"].casefold() for entry in builtin_entries({})}
    for path, target in (("plugins/commands/windows.py", "APP_ALIASES"),
                         ("plugins/commands/app_verbs.py", "LAUNCH_OVERRIDES")):
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
        aliases = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                       if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == target for t in node.targets))
        assert set(aliases) <= words
    tree = ast.parse((root / "samsara/config_seed.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "web_shortcuts":
                    assert set(ast.literal_eval(value)) <= words


def test_prompt_budget_and_freeform_guard(tmp_path):
    dictionary = store(tmp_path, {"initial_prompt": "Explicit context."}, ["MyTerm"])
    assert len(dictionary.get_initial_prompt()) <= 800
    assert dictionary.get_initial_prompt(False) == "Explicit context."
    dictionary.app.config.clear()
    assert dictionary.get_initial_prompt(False) is None
    for budget in range(100):
        prompt = prompt_words({}, [], [], budget)
        assert len(prompt) <= budget
        if prompt:
            assert prompt.split(": ", 1)[1].split(", ")[0] == builtin_entries({})[0]["word"]


def test_optout_save_failure_rolls_back(tmp_path, monkeypatch):
    import samsara.builtin_vocabulary as module
    dictionary = store(tmp_path)
    def fail(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(module.os, "replace", fail)
    assert not dictionary.set_builtin_word_enabled("notepad", False)
    assert "notepad" in dictionary.get_initial_prompt()


def test_wake_survives_oversize_user_list(tmp_path):
    dictionary = store(tmp_path, {"wake_word_config": {"phrase": "Hello Computer"}},
                       words=["VeryLongPersonalWord" * 40])
    assert dictionary.get_initial_prompt().startswith("Built-in words: Hello Computer")
    assert len(dictionary.get_initial_prompt()) <= 800


def test_loaded_tokenizer_keeps_user_and_wake_before_lower_priority_hints(tmp_path):
    dictionary = store(tmp_path, {"initial_prompt": "User context."}, ["MyTerm"])
    # A deliberately costly tokenizer makes the char proxy insufficient.
    dictionary.app.model = SimpleNamespace(hf_tokenizer=SimpleNamespace(
        encode=lambda text, **kwargs: SimpleNamespace(ids=list(text))))
    prompt = dictionary.get_initial_prompt()
    assert len(" " + prompt.strip()) <= 223
    assert prompt.startswith("User context. Common terms: MyTerm Built-in words: jarvis")
    dictionary.app.config["initial_prompt"] = "x" * 230
    assert dictionary.get_initial_prompt() == "x" * 230  # Existing explicit override contract.


@pytest.mark.parametrize("contents", ["{broken", "{}", '[123]'])
def test_corrupt_optouts_are_preserved(tmp_path, contents):
    path = tmp_path / "builtin_vocabulary_disabled.json"
    path.write_text(contents, encoding="utf-8")
    dictionary = store(tmp_path)
    assert not dictionary.set_builtin_word_enabled("notepad", False)
    assert path.read_text(encoding="utf-8") == contents


def test_builtin_entries_are_copied_not_mutable_cache():
    entries = builtin_entries({})
    entries[1]["packs"].clear()
    entries[1]["word"] = "changed"
    assert builtin_entries({})[1]["word"] == "Samsara"
    assert builtin_entries({})[1]["packs"] == ["core"]


def test_master_off_preserves_personal_words_and_freeform_guard(tmp_path):
    from samsara.config_schema import SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA["builtin_vocabulary_enabled"]["default"] is True
    dictionary = store(tmp_path, {"builtin_vocabulary_enabled": False}, ["MyTerm"])
    assert dictionary.get_initial_prompt() == "Common terms: MyTerm"
    assert dictionary.get_initial_prompt(False) is None
    assert relevant_words(dictionary.app.config, []) == []
    assert dictionary.get_builtin_words()  # Preferences remain available while off.


@pytest.fixture
def builtin_panel(tmp_path, monkeypatch, qapp):
    from samsara.ui import dictionary_panel_qt
    dictionary = store(tmp_path, words=["MyTerm"])
    app = dictionary.app
    app.dictionary_store = dictionary

    def update(updates):
        app.config.update(updates)
        app.config_path.write_text(json.dumps(app.config), encoding="utf-8")

    app.update_config_and_save = update
    monkeypatch.setattr(dictionary_panel_qt.thread_registry, "spawn",
                        lambda name, target, daemon=True: target())
    panel = dictionary_panel_qt.DictionaryPanelQt(app)
    yield panel, dictionary
    panel.close()
    panel.deleteLater()
    qapp.processEvents()


def test_real_panel_master_and_word_switches_persist_separately(builtin_panel, qapp):
    panel, dictionary = builtin_panel
    panel.resize(760, 640)
    panel.show()
    qapp.processEvents()
    assert not panel._builtin_expander.isChecked()
    assert panel._builtin_scroll.isHidden()
    assert panel._vocab_list.count() == 1
    assert panel._builtin_enabled.isChecked()
    assert panel._builtin_enabled.width() < 100
    panel._builtin_expander.click()
    qapp.processEvents()
    assert panel._builtin_scroll.isVisible()
    panel._builtin_switches["notepad"].click()
    qapp.processEvents()
    assert "notepad" not in dictionary.get_initial_prompt().lower()
    assert "notepad" not in DictionaryStore(dictionary.app).get_initial_prompt().lower()
    panel._builtin_enabled.click()
    qapp.processEvents()
    saved_config = json.loads(dictionary.app.config_path.read_text(encoding="utf-8"))
    assert saved_config["builtin_vocabulary_enabled"] is False
    assert dictionary.get_initial_prompt() == "Common terms: MyTerm"
    assert not panel._builtin_scroll.isEnabled()
    panel._builtin_enabled.click()
    qapp.processEvents()
    assert "Samsara" in dictionary.get_initial_prompt()
    assert "notepad" not in dictionary.get_initial_prompt().lower()
    assert panel._vocab_list.count() == 1
    assert dictionary.custom_vocab == ["MyTerm"]


@pytest.mark.parametrize("master", [False, True], ids=["word", "master"])
def test_panel_failed_save_restores_switch_and_effective_preference(builtin_panel, qapp, monkeypatch, master):
    panel, dictionary = builtin_panel
    if master:
        def fail(updates):
            dictionary.app.config.update(updates)
            raise OSError("simulated save failure")
        monkeypatch.setattr(dictionary.app, "update_config_and_save", fail)
        control = panel._builtin_enabled
    else:
        monkeypatch.setattr(dictionary, "set_builtin_word_enabled", lambda *args: False)
        control = panel._builtin_switches["notepad"]
    control.click()
    qapp.processEvents()
    assert control.isChecked()
    assert panel._builtin_enabled.isEnabled()
    assert "Could not save" in panel._vocab_status.text()
    assert "notepad" in dictionary.get_initial_prompt()
