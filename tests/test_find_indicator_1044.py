from types import SimpleNamespace
import json
from pathlib import Path

from PySide6.QtCore import QRect

from samsara import display_recovery
from samsara.live_surface.controller import LiveSurfaceController
from samsara.help_intents import PHRASES
from samsara.command_registry import CommandMatcher
from plugins.commands import listening_display as commands
from samsara import plugin_commands


def test_each_display_synonym_resolves_to_find_or_reset_without_help_overlap():
    assert commands._find_aliases
    assert commands._reset_aliases
    assert not (set(commands._find_aliases) & set(commands._reset_aliases))
    assert not (set(commands._find_aliases + commands._reset_aliases) & set(PHRASES))
    builtin = json.loads((Path(__file__).resolve().parents[1] / "commands.json").read_text(encoding="utf-8"))
    builtin_phrases = {phrase.casefold() for command in builtin.values()
                       for phrase in (command.get("aliases", []) + [command.get("phrase", "")])}
    assert not ({phrase.casefold() for phrase in commands._find_aliases + commands._reset_aliases}
                & builtin_phrases)
    entries = plugin_commands._MODULE_ENTRIES[commands.__name__]
    find_entry = entries["find the listening indicator"]
    reset_entry = entries["reset the listening indicator"]
    assert {phrase.casefold() for phrase in commands._find_aliases} <= set(find_entry["aliases"])
    assert {phrase.casefold() for phrase in commands._reset_aliases} <= set(reset_entry["aliases"])
    assert all(commands._find_verbs[0] in phrase or any(
        phrase.startswith(verb + " ") for verb in commands._find_verbs)
        for phrase in commands._find_aliases)
    assert all(any(phrase.startswith(verb + " ") for verb in commands._reset_verbs)
               for phrase in commands._reset_aliases)
    for target in commands._objects:
        assert any(phrase.endswith(target) for phrase in commands._find_aliases)
        assert any(phrase.endswith(target) for phrase in commands._reset_aliases)


def test_find_corrects_off_screen_compact_geometry_to_last_good_normalized_point():
    point = display_recovery._compact_anchor(
        QRect(2500, 1400, 100, 36), QRect(0, 0, 1920, 1080),
        {"screen": "old-monitor", "cx": .62, "cy": .31})
    assert point == (.62, .31)


def test_full_reset_uses_bottom_left_on_primary_and_resets_active_form_size():
    controller = LiveSurfaceController.__new__(LiveSurfaceController)
    primary = SimpleNamespace(id="primary", primary=True)
    other = SimpleNamespace(id="other", primary=False)
    controller._screens = lambda: [other, primary]
    controller._placement_config = lambda: ({"preferred_monitor": "other"},
                                              {"other": {"edge": "top", "t": .7}})
    saved = []
    controller._save_placement = saved.append
    class Widget:
        def __init__(self): self.reset = False
        def reset_default_size(self): self.reset = True
        def show(self): self.visible = True
    controller.widget = Widget()
    controller._place_widget = lambda: setattr(controller, "placed", True)

    assert controller.reset_placement()
    assert controller.widget.reset and controller.widget.visible and controller.placed
    assert saved == [{"preferred_monitor": "primary", "monitors": {
        "other": {"edge": "top", "t": .7},
        "primary": {"edge": "bottom", "t": 0.0, "free_cx": .5, "free_cy": .5}}}]


def test_full_find_moves_placement_to_pointer_monitor_and_keeps_its_last_good_position(qapp):
    from PySide6.QtGui import QCursor, QGuiApplication
    from samsara.live_surface.placement import Rect, Screen

    current = QGuiApplication.primaryScreen()
    area = current.availableGeometry()
    QCursor.setPos(area.center())
    primary_id = current.name()
    controller = LiveSurfaceController.__new__(LiveSurfaceController)
    controller._screens = lambda: [
        Screen("other", Rect(-1920, 0, 1920, 1080)),
        Screen(primary_id, Rect(area.x(), area.y(), area.width(), area.height()), True),
    ]
    saved = []
    controller._placement_config = lambda: ({"preferred_monitor": "other"}, {
        "other": {"edge": "top", "t": .9},
        primary_id: {"edge": "bottom", "t": .25, "free_cx": .5, "free_cy": .5},
    })
    controller._save_placement = saved.append
    controller._placement_anchor = "old"
    controller.widget = None

    assert controller.find_on_current_monitor()
    assert saved[0]["preferred_monitor"] == primary_id
    assert saved[0]["monitors"][primary_id]["t"] == .25
    assert controller._placement_anchor is None


def test_full_display_find_outline_pulses_three_times(qapp):
    from PySide6.QtWidgets import QWidget
    from samsara.ui.live_surface_qt import _FindOutline

    host = QWidget()
    outline = _FindOutline(host)
    outline.setGeometry(0, 0, 200, 44)
    outline.flash()
    states = [outline._on]
    for _ in range(6):
        outline._advance()
        states.append(outline._on)
    assert states == [True, False, True, False, True, False, False]
    assert not outline.isVisible()
    host.deleteLater()


def test_compact_reset_uses_bottom_left_without_changing_display_choice():
    class App:
        config = {"ui": {"listening_display": "compact"},
                  "listening_indicator_position": "top-right",
                  "listening_indicator_custom_position": {"screen": "away", "cx": .8, "cy": .2}}
        def update_config_and_save(self, changes): self.config.update(changes)
        def apply_listening_indicator_settings(self): self.applied = True

    app = App()
    from samsara.ui.listening_indicator import reset_indicator_placement
    assert reset_indicator_placement(app)
    assert app.config["listening_indicator_position"] == "bottom-left"
    assert app.config["listening_indicator_custom_position"] is None
    assert app.config["ui"]["listening_display"] == "compact"
    assert app.applied


def test_bring_back_shows_hidden_compact_display_and_flashes_it(monkeypatch):
    class Screen:
        def name(self): return "screen-1"
        def availableGeometry(self): return QRect(0, 0, 1920, 1080)

    class Indicator:
        def frameGeometry(self): return QRect(2500, 1400, 100, 36)
        def isVisible(self): return True
        def flash_find(self): self.flashes = getattr(self, "flashes", 0) + 1

    indicator = Indicator()
    app = SimpleNamespace(config={}, listening_indicator=indicator,
                          update_config_and_save=lambda updates: app.config.update(updates),
                          apply_listening_indicator_settings=lambda: setattr(app, "applied", True))
    monkeypatch.setattr(display_recovery, "_current_screen", lambda: Screen())
    monkeypatch.setattr("samsara.listening_display.display_visible", lambda app: False)
    monkeypatch.setattr("samsara.listening_display.run_display",
                        lambda app: SimpleNamespace(full=False))
    assert display_recovery.find(app)
    assert app._listening_display_visible is True
    assert app.applied is True
    assert app.config["listening_indicator_custom_position"] == {
        "screen": "screen-1", "cx": .04, "cy": .96}
    assert indicator.flashes == 1  # flash_find runs the three-pulse timer.


def test_find_phrase_is_distinct_from_settings_navigation_phrase():
    assert "show listening display" not in commands._find_aliases
    assert "show me the listening display" not in commands._find_aliases
    assert "where is the indicator" in commands._find_aliases
    assert "bring back the indicator" in commands._find_aliases
    assert "reset listening indicator" in commands._reset_aliases


def test_whole_utterance_find_dispatches_but_longer_text_falls_through(monkeypatch):
    registry = {}
    for entry in plugin_commands._MODULE_ENTRIES[commands.__name__].values():
        entry = dict(entry)
        if entry["phrase"] in {"find the listening indicator", "reset the listening indicator"}:
            entry["func"] = lambda app, remainder: True
            entry["func"].whole_utterance = True
        for phrase in (entry["phrase"], *entry["aliases"]):
            registry[phrase] = entry
    monkeypatch.setattr(plugin_commands, "_REGISTRY", registry)
    matcher = CommandMatcher()
    matcher.load_builtins({"samsara listening display": {
        "aliases": ["show listening display"], "type": "method"}})
    matcher.load_plugins(registry)
    matcher.freeze()
    monkeypatch.setattr(plugin_commands, "_shared_matcher", matcher)

    phrase, succeeded = plugin_commands.execute_command("BRING BACK THE BAR!")
    assert (phrase, succeeded) == ("find the listening indicator", True)
    assert plugin_commands.execute_command("RESET THE BAR!") == (
        "reset the listening indicator", True)
    assert plugin_commands.execute_command("I lost the bar yesterday.") == (None, False)
    assert plugin_commands.execute_command("Reset the bar after lunch.") == (None, False)
    assert matcher.match("I lost the bar yesterday.") == (None, "")
    entry, remainder = matcher.match("show listening display")
    assert entry.phrase == "samsara listening display" and remainder == ""
