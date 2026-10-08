"""Settings window split into one module per page (queue 21).

settings_qt.py keeps the window, the registry (_TAB_NAMES + the
_stack.addWidget(self._build_*_tab()) calls), the shared widgets, the card/row
helpers and _apply_and_close; each page's builder and page-only helpers live
in samsara/ui/settings/<page>_qt.py as a mixin of _SettingsWindow.

These tests catch the ways a split goes wrong silently:
  * a page module that only imports inside the running app (cycle, or a
    dictation.py dependency);
  * a registry entry whose builder no longer lives in a page module;
  * a page whose save fn stopped writing keys it wrote before the split
    (pinned from the pre-split file, enumerated against the schema);
  * moved code whose repo-relative paths re-based onto samsara/ui/settings/.
"""

import ast
import inspect
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PAGES_DIR = REPO / "samsara" / "ui" / "settings"

#: _TAB_NAMES entry -> page module that must own its builder.
PAGE_MODULES = {
    "General": "general_qt",
    "Modes": "modes_qt",
    "Commands": "commands_qt",
    "Sounds": "sounds_qt",
    "TTS": "tts_qt",
    "Ava / Cloud": "ava_cloud_qt",
    "Alarms": "alarms_qt",
    "Health": "health_qt",
    "Advanced": "advanced_qt",
    "Help & Support": "help_qt",
    "Music": "music_qt",
}


def test_dictation_rows_supply_distinct_plain_language_help(qapp):
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.ui.settings import dictation_page
    from samsara.ui.settings_qt import _SettingsWindow

    specs = dictation_page.BASE + dictation_page.ADVANCED
    assert all(len(spec) == 4 for spec in specs)

    window = _SettingsWindow(_StubApp())
    try:
        rows = [row for row in window._destination_rows if row.page_name == 'Dictation']
        # 312 added a custom profile editor; 359 made help schema-owned and optional.
        assert len(rows) == len(specs) + 1
        for _title, description, names, config in specs:
            row = window._rows_by_widget[names.split(',')[0]]
            assert row.description == description
            assert row.config_keys == tuple(config.split(','))
        assert window._rows_by_widget['wake_profiles'] in rows
        assert any(row.help is None for row in rows)
        assert any(row.help is not None for row in rows)
        for row in rows:
            expected = next((SETTINGS_SCHEMA[key]['help'] for key in row.config_keys
                             if key in SETTINGS_SCHEMA and 'help' in SETTINGS_SCHEMA[key]), None)
            assert row.help_data == expected
            assert (row.help is not None) == (expected is not None)
            assert row.full_help
            if expected is not None:
                assert expected['why'] != row.description
                sentences = [sentence for sentence in expected['why'].split('.') if sentence.strip()]
                assert 2 <= len(sentences) <= 4, expected['why']
                assert row.help.accessibleDescription() == expected['why']
                row.help.click()
                assert row.help_copy.text() == expected['why']
                assert expected['docs'] in row.help_link.text()
                row.hide_help()
    finally:
        window.deleteLater()

#: Flattened config keys each page's save fn wrote BEFORE the split, captured
#: from the monolithic settings_qt.py (aa948d3) with every schema default
#: loaded. Commands' pack keys are derived from PACKS below instead of pinned,
#: so adding a pack is not a split regression. Health registers no save fn;
#: queue 300 added diagnostics to Help & Support after the split.
KEYS_BEFORE_SPLIT = {
    "_build_general_tab": [
        'add_trailing_space', 'auto_capitalize', 'auto_paste', 'cleanup_mode', 'format_numbers',
        'hints_enabled', 'language', 'microphone', 'microphone_name', 'model_size',
        'launch_at_login', 'start_hidden',
        # Queue 129: the theme control. The page writes the whole `ui` section
        # so a sibling key set elsewhere (ui.idle_animation) survives a save
        # rather than being dropped, which is why both appear here.
        'ui.idle_animation', 'ui.theme',
        'ui_scale', 'updates.automatic_checks',
        # Queue 176: Ava captions and Ava audio mute, the two controls of the
        # first accessibility mode. Same reason as `ui` above -- the page
        # writes the whole `accessibility` section so the caption panel's
        # dragged position (written by the panel itself, not by Settings)
        # survives a save instead of being dropped.
        'accessibility.ava_captions', 'accessibility.ava_mute_audio',
        'accessibility.ava_captions_linger_s', 'accessibility.ava_captions_position',
    ],
    "_build_modes_tab": [
        'ava_command_session.backend', 'ava_command_session.keep_warm',
        'ava_command_session.model',
        'ava_mode_key', 'cancel_hotkey', 'command_hotkey',
        'command_mode.abort_phrases', 'command_mode.ava_tts_half_duplex',  # 9dc560f
        'command_mode.button',
        'command_mode.dictate_commit_word',  # 3da3d0b
        'command_mode.dictate_utterance_silence_s', 'command_mode.enabled',
        'command_mode.enter_debounce_ms', 'command_mode.inactivity_timeout_s',
        'command_mode.miss_limit', 'command_mode.mode', 'command_mode.session_streaming_preview',
        'command_mode.suppress_button', 'command_mode.tts_char_limit',   # queue 57: now has a control
        'command_mode.command_matching_enabled', 'command_mode.exit_earcon',   # queue 62: now have controls
        'command_mode.utterance_silence_s',
        'command_mode.cancel_window_s', 'command_mode.cancel_window_all_commands',   # queue 69
        'command_mode.preview_idle_delay_s', 'command_mode.preview_idle_opacity',    # queue 75
        'command_mode.preview_position',  # 03956c8
        'command_mode.stop_phrases',   # queue 116: the emergency stop's words
        'continuous_commit_hotkey', 'continuous_commit_trigger',
        'continuous_hotkey', 'correction_hotkey', 'dictate_commit_hotkey', 'hotkey',
        'hotkeys.capture_correction', 'memo_hotkey', 'mode', 'undo_hotkey',
        'wake_word_config.audio.bypass_adaptive_gate_in_session',  # 885707f
        'wake_word_config.audio.speech_threshold', 'wake_word_config.audio.wake_command_timeout',
        'wake_word_config.opens_session', 'wake_word_config.oww_threshold',
        'wake_word_config.phrase', 'wake_word_config.quick_silence_timeout',
        'wake_word_config.wake_abort_phrase', 'wake_word_enabled', 'wake_word_hotkey',
    ],
    # 372: speech-level persistence moved from Sounds to TTS.
    "_build_sounds_tab": ['audio_feedback', 'sound_theme',
                          'sound_volume'],
    "_build_tts_tab": [
        'audio_coordinator.duck_factor', 'tts.enabled', 'tts.engine', 'tts.feedback_level',  # 372: replacement control.
        'tts.rate', 'tts.speed',
        'tts.volume',
    ],
    "_build_ava_cloud_tab": [
        # Queue 183's consent record and queue 175e's warm choice are both
        # preserved when this page writes the existing `ava` section.
        'ava.consent.accepted_at', 'ava.consent.cloud_version', 'ava.consent.version',
        'ava.warm_on_boot',
        'ava_memory.max_turns', 'ava_memory.mode', 'ava_personality', 'cloud_llm.api_key',
        'cloud_llm.enabled', 'cloud_llm.provider', 'cloud_llm.timeout_seconds',
        'cloud_llm.web_search',   # queue 59
    ],
    "_build_alarms_tab": [
        'alarms.complete_hotkey', 'alarms.dismiss_hotkey', 'alarms.enabled',
        'alarms.nag_interval_seconds',
    ],
    "_build_advanced_tab": [
        'benchmark.collect_samples', 'benchmark.max_samples', 'cal_multiplier', 'compute_type',
        'device', 'ducking.enabled', 'ducking.level', 'ducking.hands_free_enabled',
        'ducking.hands_free_idle_level', 'ducking.hands_free_level',
        'echo_cancellation.enabled',
        'echo_cancellation.latency_ms', 'gesture.enabled',
        # Queue 93: the command-word escape hatch -- the one config key that
        # lets a one-word command run despite intent execution rule 1. The
        # page writes the whole `intent` section, so shadow_enabled (which
        # has no widget, by design) is preserved through a save rather than
        # dropped -- hence both keys here, only one of them editable.
        'intent.command_prefix', 'intent.shadow_enabled', 'listening_indicator_enabled',
        'listening_indicator_position', 'min_speech_duration', 'performance_mode',
        'silence_threshold',
        'smart_corrections.enabled', 'smart_corrections.keep_alive', 'smart_corrections.min_words',
        'smart_corrections.modes.hotkey', 'smart_corrections.modes.streaming',
        'smart_corrections.modes.wake', 'smart_corrections.ollama_model',
        'smart_corrections.repair_disfluencies', 'smart_corrections.timeout_s', 'threshold_mode',
    ],
    "_build_music_tab": ['music_library'],
}


def _commands_keys_before_split():
    from samsara.command_packs import PACKS
    return sorted(f"command_packs.{pack_id}" for pack_id, meta in PACKS.items()
                  if not meta.get("always_on"))


def _schema_defaults():
    from samsara import config_defaults
    cfg: dict = {}
    for key, value in config_defaults.DEFAULTS.items():
        node = cfg
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return cfg


def _flatten(d, prefix=""):
    out = {}
    for key, value in d.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(_flatten(value, path + "."))
        else:
            out[path] = value
    return out


class _StubApp:
    def __init__(self, config=None):
        self.config = config if config is not None else {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda p: None)
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *a, **k):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


@pytest.fixture(autouse=True)
def _settings_tests_never_touch_windows_registry(monkeypatch):
    from samsara import autostart
    monkeypatch.setattr(autostart, "sync_run_entry", lambda enabled, **kwargs: None)


def _registry_builders():
    """Builder method names in _stack.addWidget order, read from __init__."""
    from samsara.ui import settings_qt
    src = inspect.getsource(settings_qt._SettingsWindow.__init__)
    tree = ast.parse(src.replace("\n    ", "\n").lstrip())
    builders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "addWidget" and node.args
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Attribute)
                and node.args[0].func.attr.startswith("_build_")):
            builders.append((node.lineno, node.args[0].func.attr))
    return [name for _line, name in sorted(builders)]


# ---------------------------------------------------------------------------
# Import isolation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", sorted(PAGE_MODULES.values()))
def test_page_module_imports_standalone_without_dictation(module):
    """Fresh interpreter, page module first: no cycle, no dictation.py."""
    code = (
        "import sys\n"
        f"import samsara.ui.settings.{module} as m\n"
        "assert 'dictation' not in sys.modules, 'page import pulled in dictation.py'\n"
        "page_classes = [v for v in vars(m).values() if isinstance(v, type) and v.__module__ == m.__name__]\n"
        "assert any(c.__name__.endswith('Page') for c in page_classes), page_classes\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


def test_settings_qt_imports_without_dictation():
    code = ("import sys\nimport samsara.ui.settings_qt\n"
            "assert 'dictation' not in sys.modules\nprint('ok')\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]


def test_every_page_module_on_disk_is_registered():
    on_disk = {p.stem for p in PAGES_DIR.glob("*_qt.py")} - {"shared_qt", "preview_285_qt"}
    assert on_disk == set(PAGE_MODULES.values())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_every_registry_entry_resolves_to_its_page_module():
    from samsara.ui import settings_qt
    assert len(settings_qt.PAGE_MODULES) == 6
    for module, name in zip(settings_qt.PAGE_MODULES, settings_qt._TAB_NAMES):
        assert module.TITLE == name
        assert module.build.__module__ == module.__name__
        assert Path(module.__file__).parent == PAGES_DIR
    assert {p.stem for p in PAGES_DIR.glob("*_page.py")} == {
        m.__name__.rsplit(".", 1)[-1] for m in settings_qt.PAGE_MODULES
    } | {"tools_page"}


def test_window_keeps_the_shared_surface():
    from samsara.ui import settings_qt
    own = vars(settings_qt._SettingsWindow)
    for name in ("__init__", "_apply_and_close", "_section_card", "_section_title", "_setting_row",
                 "_build_search_registry", "_apply_search_filter", "show_tab"):
        assert hasattr(settings_qt._SettingsWindow, name), name
    for name in ("_HotkeyButton", "_AlarmHotkeyButton", "_HeightForWidthWidget", "stylesheet",
                 "_TAB_NAMES", "_SIDEBAR_GROUPS", "_CMD_BUTTON_OPTIONS", "_format_alarm_next",
                 "_collect_command_rows"):
        assert hasattr(settings_qt, name), name


def test_source_factory_mixins_remain_registered():
    from samsara.ui import settings_qt
    bases = {b.__module__.rsplit(".", 1)[-1] for b in settings_qt._SettingsWindow.__bases__}
    assert set(PAGE_MODULES.values()) <= bases


# ---------------------------------------------------------------------------
# Config keys: nothing a page wrote before the split went missing
# ---------------------------------------------------------------------------

def _keys_by_builder(window):
    produced: dict = {}
    updates: dict = {}
    for fn in (save for saves in window._source_saves.values() for save in saves):
        builder = next(part for part in fn.__qualname__.split(".") if part.startswith("_build_"))
        part = fn(dict(updates))
        produced.setdefault(builder, set()).update(_flatten(part))
        updates.update(part)
        assert fn.__module__.startswith("samsara.ui.settings."), (builder, fn.__module__)
    return produced, updates


def test_every_key_written_before_the_split_is_still_written(qapp):
    from samsara.ui import settings_qt
    window = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    try:
        produced, _updates = _keys_by_builder(window)
        expected = dict(KEYS_BEFORE_SPLIT, _build_commands_tab=_commands_keys_before_split())
        expected['_build_support_tab'] = ['diagnostics.stage_timing', 'diagnostics.write_jsonl']
        assert set(produced) == set(expected)            # no page lost (or gained) a save fn
        for builder, keys in expected.items():
            # Nested dictionaries preserve newer sibling defaults. Their addition
            # is not a lost-key regression; all former keys must still survive.
            assert set(keys) <= produced[builder], (builder, set(keys) - produced[builder])
            from tests.test_settings_row_inventory_285 import AUDITED_KEYS
            known = set(_flatten(_schema_defaults())) | set(AUDITED_KEYS)
            assert produced[builder] - set(keys) <= known, builder
    finally:
        window.deleteLater()


def test_schema_keys_written_before_are_all_still_written(qapp):
    """Enumerated from the schema: every SETTINGS_SCHEMA key the old file
    wrote is written after the split."""
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.ui import settings_qt
    before = set().union(*map(set, KEYS_BEFORE_SPLIT.values()), _commands_keys_before_split())
    schema_before = sorted(k for k in SETTINGS_SCHEMA if k in before)
    assert len(schema_before) > 50                     # the pin is real, not vacuous

    window = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    try:
        produced, updates = _keys_by_builder(window)
        after = set(_flatten(updates))
        assert [k for k in schema_before if k not in after] == []
    finally:
        window.deleteLater()


def test_apply_and_close_writes_through_the_page_save_fns(qapp):
    from samsara.ui import settings_qt
    written = {}

    class _App(_StubApp):
        def save_config(self):
            written.update(_flatten(self.config))

        def switch_microphone(self, *a):
            pass

        def switch_output_device(self, *a):
            pass

    window = settings_qt._SettingsWindow(_App(_schema_defaults()))
    window._apply_and_close()
    for keys in KEYS_BEFORE_SPLIT.values():
        for key in keys:
            if key in ("microphone", "microphone_name", "gesture.enabled") or key.startswith(("alarms.", "music_library.", "tasks.", "command_packs.")):
                continue          # routed through switch_microphone only when it changes
            assert key in written, key


def test_general_page_startup_options_round_trip(qapp):
    from samsara.ui import settings_qt

    window = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    try:
        login = window._widgets["launch_at_login"]
        hidden = window._widgets["start_hidden"]
        assert not login.isChecked()
        assert not hidden.isChecked()
        assert login.accessibleName() == "Start Samsara when I sign in to Windows"
        assert hidden.accessibleName() == "Start hidden (tray only)"

        login.setChecked(True)
        hidden.setChecked(True)
        updates = window._save_fns[0]({})
        assert updates["launch_at_login"] is True
        assert updates["start_hidden"] is True
    finally:
        window.deleteLater()


def test_registry_failure_reverts_login_checkbox_and_notifies_once(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui import settings_qt

    notices = []
    monkeypatch.setattr(
        autostart, "sync_run_entry",
        lambda enabled, **kwargs: (_ for _ in ()).throw(PermissionError("policy")),
    )
    monkeypatch.setattr(
        settings_qt.QMessageBox, "warning",
        lambda *args: notices.append(args[2]),
    )
    window = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    try:
        checkbox = window._widgets["launch_at_login"]
        checkbox.setChecked(True)
        updates = window._save_fns[0]({})
        assert checkbox.isChecked() is False
        assert updates["launch_at_login"] is False
        assert len(notices) == 1
        assert "Windows could not update" in notices[0]
    finally:
        window.deleteLater()


# ---------------------------------------------------------------------------
# Repo-relative paths did not move with the code
# ---------------------------------------------------------------------------

def test_moved_file_anchors_still_point_at_settings_qt():
    from samsara.ui import settings_qt
    import importlib
    anchored = []
    for module in PAGE_MODULES.values():
        mod = importlib.import_module(f"samsara.ui.settings.{module}")
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "Path(__file__)" not in src, module
        if "_SETTINGS_QT_FILE" in src:
            assert Path(mod._SETTINGS_QT_FILE) == Path(settings_qt.__file__), module
            anchored.append(module)
    assert set(anchored) == {"general_qt", "commands_qt", "sounds_qt", "health_qt"}
    repo_root = Path(settings_qt.__file__).parent.parent.parent
    assert (repo_root / "commands.json").exists()
    assert (repo_root / "sounds").is_dir()
    assert (repo_root / "dictionaries" / "medications.json").exists()


def test_page_offscreen_render_and_collision_checker_see_page_hotkeys(qapp):
    from samsara.ui import settings_qt
    window = settings_qt._SettingsWindow(_StubApp())
    try:
        for index, name in enumerate(settings_qt._TAB_NAMES):
            window._stack.setCurrentIndex(index)
            qapp.processEvents()
            assert not window._stack.widget(index).grab().isNull(), name
        hotkeys = [key for key, w in window._widgets.items() if isinstance(w, settings_qt._HotkeyButton)]
        for key in hotkeys:
            window._widgets[key]._combo = "ctrl+f9"
        window._check_modes_collisions()
        banner = window._widgets["modes_collision_warn"]
        assert not banner.isHidden()
        for label in ("Record", "Toggle continuous", "Toggle wake word", "Command only",
                      "Cancel recording", "Undo", "Paste staged thought", "Voice memo",
                      "Correction report", "Correction capture", "Continuous commit"):
            assert label in banner.text(), label
    finally:
        window.deleteLater()
