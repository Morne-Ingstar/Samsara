"""Queue 62: the five command_mode keys the app reads now have Modes-tab
controls (tts_char_limit landed with queue 57). Each control round-trips
through save, the engine reads the saved value, and a value that would
silence or break the feature is prevented or warned about.

Never imports dictation (Samsara may be running); dictation.py reads are
pinned by source text instead."""
import inspect
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import session_modes as sm
from samsara.ui.settings import modes_qt as mq

ROOT = Path(__file__).resolve().parents[1]


class _App:
    def __init__(self, config=None):
        self.config = config if config is not None else {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda p: None)
        self.hints = None
        self.alarm_manager = None
        self.command_matching_enabled = bool(
            (self.config.get('command_mode') or {}).get('command_matching_enabled', False))

    def play_sound(self, *a, **k):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}


def _win(config=None):
    from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
    app = _App(config)
    win = _SettingsWindow(app)
    return app, win, win._save_fns[_TAB_NAMES.index('Modes')]


def _reload(app):
    """Apply to the app's config as _apply_and_close does, then rebuild."""
    from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES
    win = _SettingsWindow(app)
    return win, win._save_fns[_TAB_NAMES.index('Modes')]


# -- the list is empty ----------------------------------------------------
def test_config_only_list_is_empty_and_note_is_gone(qapp):
    assert mq._MODES_CONFIG_ONLY_KEYS == ()
    _app, win, _save = _win()
    assert 'modes_config_only_note' not in win._widgets


# -- round trips ------------------------------------------------------------
def test_every_new_control_round_trips_through_apply_and_reload(qapp):
    app, win, _save = _win({"command_mode": {}})
    win._widgets['cmd_utterance_silence'].setValue(1.35)
    win._widgets['cmd_dictate_utterance_silence'].setValue(0.9)
    win._widgets['cmd_exit_earcon'].setChecked(False)
    win._widgets['cmd_matching_enabled'].setChecked(True)
    win._widgets['cmd_abort_phrases'].setPlainText("that's all folks\n\n  done for now \nthat's all folks")
    win._apply_and_close()

    cm = app.config['command_mode']
    assert cm['utterance_silence_s'] == 1.35
    assert cm['dictate_utterance_silence_s'] == 0.9
    assert cm['exit_earcon'] is False
    assert cm['command_matching_enabled'] is True
    assert cm['abort_phrases'] == ["that's all folks", "done for now"]
    assert app.command_matching_enabled is True          # live attribute kept in step

    win2, _ = _reload(app)
    assert win2._widgets['cmd_utterance_silence'].value() == pytest.approx(1.35)
    assert win2._widgets['cmd_dictate_utterance_silence'].value() == pytest.approx(0.9)
    assert win2._widgets['cmd_exit_earcon'].isChecked() is False
    assert win2._widgets['cmd_matching_enabled'].isChecked() is True
    assert win2._widgets['cmd_abort_phrases'].toPlainText() == "that's all folks\ndone for now"


def test_defaults_match_the_engine_fallbacks(qapp):
    _app, win, save = _win({})
    cm = save({})['command_mode']
    assert cm['utterance_silence_s'] == 1.0                   # wake_consumer fallback
    assert cm['dictate_utterance_silence_s'] == 0.65          # wake_consumer + schema fallback
    assert cm['exit_earcon'] is True                          # exit_command_mode fallback
    assert cm['command_matching_enabled'] is False
    assert cm['abort_phrases'] == []


def test_abort_phrases_stored_as_a_string_are_shown(qapp):
    _app, win, save = _win({"command_mode": {"abort_phrases": "over and out"}})
    assert win._widgets['cmd_abort_phrases'].toPlainText() == "over and out"
    assert save({})['command_mode']['abort_phrases'] == ["over and out"]


def test_unrelated_command_mode_keys_survive_save(qapp):
    _app, win, save = _win({"command_mode": {"session_streaming_preview": False, "tts_char_limit": 80}})
    cm = save({})['command_mode']
    assert cm['session_streaming_preview'] is False and cm['tts_char_limit'] == 80


# -- the engine reads the saved values -------------------------------------
def test_saved_abort_phrases_end_a_real_session(qapp):
    _app, win, save = _win({})
    win._widgets['cmd_abort_phrases'].setPlainText("over and out")
    saved = save({})['command_mode']['abort_phrases']
    aborted = []
    manager = sm.SessionModeManager(
        abort_phrases=list(sm.GLOBAL_SESSION_EXIT_PHRASES),
        foreground_exe_resolver=lambda: "obsidian.exe",
        foreground_hwnd_resolver=lambda: 7,
        inject_fn=lambda text, commit_focus_guard=None: text,
        remove_chars_fn=lambda n: None,
        command_dispatch_fn=lambda text: sm.CommandDispatchResult(matched=False),
        agent_dispatch_fn=lambda *a: None,
        on_abort=lambda: aborted.append(1),
        buffer_dictate_until_commit=True,
        extra_sleep_phrases=saved,
    )
    manager.reset(initial_mode=sm.SessionMode.DICTATE)
    signals = sm.UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
    assert manager.dispatch_utterance("Over and out.", signals).kind == "mode_switch"
    assert aborted == [1]


def test_engine_reads_the_keys_the_controls_write():
    wake = (ROOT / "samsara" / "audio_engine" / "wake_consumer.py").read_text(encoding="utf-8")
    assert "cm_cfg.get('utterance_silence_s'" in wake
    assert "cm_cfg.get('dictate_utterance_silence_s'" in wake
    dictation_src = (ROOT / "dictation.py").read_text(encoding="utf-8")
    assert "cfg.get('exit_earcon', True)" in dictation_src
    assert ".get('abort_phrases', [])" in dictation_src
    from samsara.commands import CommandExecutor
    assert "effective_app.command_matching_enabled" in inspect.getsource(CommandExecutor.process_text)


# -- feature-breaking values are prevented or warned ------------------------
@pytest.mark.parametrize("widget", ['cmd_utterance_silence', 'cmd_dictate_utterance_silence'])
def test_silence_spins_cannot_reach_zero_or_extremes(qapp, widget):
    _app, win, _save = _win({})
    spin = win._widgets[widget]
    spin.setValue(0.0)
    assert spin.value() == mq._SILENCE_MIN_S
    spin.setValue(60.0)
    assert spin.value() == mq._SILENCE_MAX_S


@pytest.mark.parametrize("stored, shown", [(0, 0.3), (-1, 0.3), (99, 3.0), ("junk", None)])
def test_out_of_range_stored_silence_is_clamped_and_explained(qapp, stored, shown):
    _app, win, save = _win({"command_mode": {"utterance_silence_s": stored}})
    spin = win._widgets['cmd_utterance_silence']
    expected = shown if shown is not None else 1.0
    assert spin.value() == pytest.approx(expected)
    warn = win._widgets['cmd_utterance_silence_warn']
    assert not warn.isHidden() and "config.json has" in warn.text()
    assert save({})['command_mode']['utterance_silence_s'] == expected


def test_silence_warnings_follow_the_value(qapp):
    _app, win, _save = _win({})
    spin = win._widgets['cmd_utterance_silence']
    warn = win._widgets['cmd_utterance_silence_warn']
    assert warn.isHidden()                       # default 1.0 s is fine
    spin.setValue(0.35)
    assert not warn.isHidden() and "split" in warn.text()
    spin.setValue(2.5)
    assert "waits" in warn.text()
    spin.setValue(1.0)
    assert warn.isHidden()
    dspin = win._widgets['cmd_dictate_utterance_silence']
    dwarn = win._widgets['cmd_dictate_utterance_silence_warn']
    dspin.setValue(2.8)
    assert not dwarn.isHidden() and "'end'" in dwarn.text()


@pytest.mark.parametrize("phrase, fragment", [
    ("end", "paste word"),
    ("dictate", "switches lanes"),
    ("command mode", "switches lanes"),
    (sm.SESSION_STOP_PHRASES[0], "stop phrase"),
    ("scratch that", "scratching"),
    ("go to sleep", "already built in"),
    ("um", "ignored"),
])
def test_abort_phrase_that_breaks_a_built_in_is_warned(qapp, phrase, fragment):
    _app, win, _save = _win({})
    win._widgets['cmd_abort_phrases'].setPlainText(f"over and out\n{phrase}")
    warn = win._widgets['cmd_abort_phrases_warn']
    assert not warn.isHidden() and fragment in warn.text()
    win._widgets['cmd_abort_phrases'].setPlainText("over and out")
    assert warn.isHidden()


def test_descriptions_state_the_edges(qapp):
    _app, win, _save = _win({})
    from PySide6.QtWidgets import QLabel
    labels = [lbl.text() for lbl in win.findChildren(QLabel)]
    assert mq._CMD_MATCHING_DESC in labels and "typed as text" in mq._CMD_MATCHING_DESC
    assert mq._CMD_EXIT_EARCON_DESC in labels and "no sound" in mq._CMD_EXIT_EARCON_DESC
    assert mq._CMD_SILENCE_DESC in labels and mq._DICTATE_SILENCE_DESC in labels
    abort_desc = mq._ABORT_PHRASES_DESC.format(sleep=sm.SESSION_SLEEP_PHRASES[0])
    assert abort_desc in labels and "Empty means" in abort_desc and "next time Samsara starts" in abort_desc
