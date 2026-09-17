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

#: Flattened config keys each page's save fn wrote BEFORE the split, captured
#: from the monolithic settings_qt.py (aa948d3) with every schema default
#: loaded. Commands' pack keys are derived from PACKS below instead of pinned,
#: so adding a pack is not a split regression. Health and Help & Support
#: register no save fn.
KEYS_BEFORE_SPLIT = {
    "_build_general_tab": [
        'add_trailing_space', 'auto_capitalize', 'auto_paste', 'cleanup_mode', 'format_numbers',
        'hints_enabled', 'language', 'microphone', 'microphone_name', 'model_size',
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
        'ava_command_session.backend', 'ava_command_session.enabled',
        'ava_command_session.inactivity_timeout_s', 'ava_command_session.keep_warm',
        'ava_command_session.key', 'ava_command_session.miss_limit', 'ava_command_session.model',
        'ava_command_session.queue_depth_cap', 'ava_command_session.ready_cue_dir',
        'ava_command_session.ready_cue_enabled', 'ava_command_session.shortlist_size',
        'ava_mode_enabled', 'ava_mode_key', 'cancel_hotkey', 'command_hotkey',
        'command_mode.abort_phrases', 'command_mode.button',
        'command_mode.dictate_utterance_silence_s', 'command_mode.enabled',
        'command_mode.enter_debounce_ms', 'command_mode.inactivity_timeout_s',
        'command_mode.miss_limit', 'command_mode.mode', 'command_mode.session_streaming_preview',
        'command_mode.suppress_button', 'command_mode.tts_char_limit',   # queue 57: now has a control
        'command_mode.command_matching_enabled', 'command_mode.exit_earcon',   # queue 62: now have controls
        'command_mode.utterance_silence_s',
        'command_mode.cancel_window_s', 'command_mode.cancel_window_all_commands',   # queue 69
        'command_mode.preview_idle_delay_s', 'command_mode.preview_idle_opacity',    # queue 75
        'command_mode.stop_phrases',   # queue 116: the emergency stop's words
        'continuous_commit_hotkey', 'continuous_commit_trigger',
        'continuous_hotkey', 'correction_hotkey', 'dictate_commit_hotkey', 'hotkey',
        'hotkeys.capture_correction', 'memo_hotkey', 'mode', 'undo_hotkey',
        'wake_word_config.audio.speech_threshold', 'wake_word_config.audio.wake_command_timeout',
        'wake_word_config.opens_session', 'wake_word_config.oww_threshold',
        'wake_word_config.phrase', 'wake_word_config.quick_silence_timeout',
        'wake_word_config.wake_abort_phrase', 'wake_word_enabled', 'wake_word_hotkey',
    ],
    # Queue 103 added feedback.spoken_notices: the Sounds page now also
    # writes which of the session's own notices are spoken out loud.
    "_build_sounds_tab": ['audio_feedback', 'feedback.spoken_notices', 'sound_theme',
                          'sound_volume'],
    "_build_tts_tab": [
        'audio_coordinator.duck_factor', 'audio_coordinator.enabled', 'tts.enabled', 'tts.engine',
        'tts.pitch', 'tts.rate', 'tts.speed', 'tts.use_for_agent_responses',
        'tts.use_for_confirmations', 'tts.use_for_dictation_readback', 'tts.use_for_errors',
        'tts.use_for_status_updates', 'tts.use_for_warnings', 'tts.volume',
    ],
    "_build_ava_cloud_tab": [
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
        'silence_threshold', 'smart_corrections.allow_cloud_fallback', 'smart_corrections.backend',
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
    on_disk = {p.stem for p in PAGES_DIR.glob("*_qt.py")}
    assert on_disk == set(PAGE_MODULES.values())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_every_registry_entry_resolves_to_its_page_module():
    from samsara.ui import settings_qt
    builders = _registry_builders()
    assert len(builders) == len(settings_qt._TAB_NAMES)
    for tab_name, builder in zip(settings_qt._TAB_NAMES, builders):
        expected = f"samsara.ui.settings.{PAGE_MODULES[tab_name]}"
        method = getattr(settings_qt._SettingsWindow, builder)
        assert method.__module__ == expected, (tab_name, builder, method.__module__)
        # lives on the page mixin, not re-defined on the window
        assert builder not in vars(settings_qt._SettingsWindow), builder


def test_window_keeps_the_shared_surface():
    from samsara.ui import settings_qt
    own = vars(settings_qt._SettingsWindow)
    for name in ("__init__", "_apply_and_close", "_section_card", "_section_title", "_setting_row",
                 "_build_search_registry", "_apply_search_filter", "show_tab"):
        assert name in own, name
    for name in ("_HotkeyButton", "_AlarmHotkeyButton", "_HeightForWidthWidget", "stylesheet",
                 "_TAB_NAMES", "_SIDEBAR_GROUPS", "_CMD_BUTTON_OPTIONS", "_format_alarm_next",
                 "_collect_command_rows"):
        assert hasattr(settings_qt, name), name


def test_page_mixins_are_in_tab_order():
    from samsara.ui import settings_qt
    bases = [b.__module__.rsplit(".", 1)[-1] for b in settings_qt._SettingsWindow.__bases__[:-1]]
    assert bases == [PAGE_MODULES[name] for name in settings_qt._TAB_NAMES]


# ---------------------------------------------------------------------------
# Config keys: nothing a page wrote before the split went missing
# ---------------------------------------------------------------------------

def _keys_by_builder(window):
    produced: dict = {}
    updates: dict = {}
    for fn in window._save_fns:
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
        assert set(produced) == set(expected)            # no page lost (or gained) a save fn
        for builder, keys in expected.items():
            assert sorted(produced[builder]) == sorted(keys), builder
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
            if key in ("microphone", "microphone_name"):
                continue          # routed through switch_microphone only when it changes
            assert key in written, key


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
