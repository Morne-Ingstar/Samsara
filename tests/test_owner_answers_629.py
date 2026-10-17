"""Owner-approved aliases and OpenRouter defaults, without app bootstrap or I/O."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit

from samsara import cloud_llm, command_catalog as cc
from samsara.config_seed import build_default_config
from samsara.ui.settings.ava_cloud_qt import _OpenRouterPicker

MODEL = "google/gemini-3.1-flash-lite"
ALIASES = {
    "mute the spotify": ("music.mute", "mute"),
    "skip the track": ("music.next", "next"),
    "hide the numbers": ("show_numbers.hide_numbers", "hide numbers"),
    "hide the labels": ("show_numbers.hide_numbers", "hide numbers"),
    "refresh the numbers": ("show_numbers.refresh_numbers", "refresh numbers"),
    "show the labels": ("show_numbers.show_numbers", "show numbers"),
    "show the numbers": ("show_numbers.show_numbers", "show numbers"),
    "show the clickable": ("show_numbers.show_numbers", "show numbers"),
}


@pytest.fixture(scope="module")
def registry():
    from tools.dump_command_metadata import build_executor
    executor = build_executor()
    return executor, cc.raw_phrase_claims(executor)


@pytest.mark.parametrize("phrase", ALIASES)
def test_approved_alias_resolves_exactly_without_collision(registry, phrase):
    executor, claims = registry
    canonical_id, canonical_phrase = ALIASES[phrase]
    assert claims[phrase] == {canonical_id}
    entry, remainder = executor._matcher.match(phrase)
    assert entry is not None and entry.phrase == canonical_phrase
    assert remainder == ""


@pytest.mark.parametrize("saved", [None, "", "openrouter/auto", "custom/saved-model", MODEL])
def test_fresh_default_and_saved_model_are_preserved(tmp_path, saved):
    config = build_default_config(home=tmp_path, macro_defaults={}, notification_defaults={})
    config.setdefault("cloud_llm", {})["provider"] = "openrouter"
    if saved is not None:
        config["cloud_llm"]["model"] = saved
    before = deepcopy(config)
    assert cloud_llm._get_provider_config(SimpleNamespace(config=config))[2] == (saved or MODEL)
    assert config == before


@pytest.mark.parametrize("saved", ["", "openrouter/auto", "custom/saved-model"])
def test_picker_uses_fresh_default_and_keeps_explicit_choices(qapp, saved):
    entry = QLineEdit(saved)
    picker = _OpenRouterPicker(entry, lambda: True)
    try:
        picker.populate()
        selected = picker.models.currentItem().data(Qt.ItemDataRole.UserRole)
        assert selected == (saved or MODEL)
        assert entry.text() == saved
        picker.select_model()
        assert entry.text() == (saved or MODEL)
        picker.models.setCurrentRow(0)
        picker.select_model()
        assert entry.text() == "openrouter/auto"
        assert cloud_llm._get_provider_config(SimpleNamespace(
            config={"cloud_llm": {"provider": "openrouter", "model": entry.text()}}))[2] == "openrouter/auto"
    finally:
        picker.deleteLater()
        entry.deleteLater()
