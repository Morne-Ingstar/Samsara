"""Queue 191: hands-free diagnostics and honest wake state surfaces.

The tests use the real Qt widgets but never import dictation.py. The
recogniser seam is asserted from its source contract; the UI consumes the
same diagnostics ring that production writes.
"""

import threading
import types
from pathlib import Path

from PySide6.QtWidgets import QLabel, QComboBox

from samsara import diagnostics
from samsara.ui import home_signals, main_window_qt, voice_help_qt


def _app(**overrides):
    app = types.SimpleNamespace(
        config={
            "mode": "hold",
            "wake_word_enabled": True,
            "wake_word_config": {"phrase": "jarvis"},
            "command_mode": {"mode": "toggle"},
        },
        available_mics=[{"id": 7, "name": "USB mic"}],
        command_mode_active=False,
        ava_command_session_active=False,
        continuous_active=False,
        wake_word_active=True,
        snoozed=False,
        recording=False,
        _wake_consumer=types.SimpleNamespace(running=True),
        _hands_free_fault=None,
        _wake_start_pending=False,
        audio_coordinator=object(),
    )
    app.config["wake_word_config"].setdefault(
        "phrase_options", ["jarvis", "hey jarvis", "computer"])
    app.config.update(overrides.pop("config", {}))
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


def _record(*, mode="hands_free", lane="dictate", text="check one two", outcome="ok"):
    diagnostics.record(diagnostics.DiagRecord(
        mode=mode,
        lane=lane,
        audio_s=1.2,
        model_name="small",
        device="cpu",
        compute_type="int8",
        t_transcribe_ms=120,
        t_total_ms=120,
        n_segments=1,
        text=text,
        outcome=outcome,
    ))


def test_fake_hands_free_capture_reaches_voice_help_audio_check(qapp):
    diagnostics.clear()
    _record()
    page = voice_help_qt.VoiceHelpPage(_app())
    status, evidence = page.run_stage("audio")
    assert status == home_signals.READY
    assert "last 1 recordings reached the recogniser" in evidence
    diagnostics.clear()


def test_hands_free_record_contract_is_at_the_recogniser_boundary():
    source = Path("dictation.py").read_text(encoding="utf-8")
    assert 'mode="hands_free"' in source
    assert 'lane=_capture_mode' in source
    assert "diagnostics.record(diagnostics.DiagRecord(" in source


def test_wake_check_explains_that_hands_free_suspends_wake_detection(qapp):
    app = _app(command_mode_active=True)
    page = voice_help_qt.VoiceHelpPage(app)
    status, evidence = page.run_stage("wake")
    assert status == home_signals.OFF
    assert evidence == voice_help_qt.HANDS_FREE_WAKE_EVIDENCE
    assert "wake phrase is not used" in page.stage_texts()["wake"]


def test_status_bar_suspends_wake_only_during_hands_free():
    app = _app()
    target = types.SimpleNamespace(
        _app=app,
        _ava=types.SimpleNamespace(refresh=lambda: None),
        _panel_cache={},
        _lbl_mode=QLabel(),
        _lbl_wake=QLabel(),
        _lbl_mic=QLabel(),
        _paused_btn=QLabel(),
        _badge=QLabel(),
    )

    app.command_mode_active = True
    main_window_qt._MainWindow._refresh_status(target)
    assert target._lbl_wake.text() == '"jarvis" (suspended)'
    assert main_window_qt.theme.ICON_IDLE in target._lbl_wake.styleSheet()

    app.command_mode_active = False
    main_window_qt._MainWindow._refresh_status(target)
    assert target._lbl_wake.text() == '"jarvis" (on)'
    assert main_window_qt.theme.TEXT_PRIMARY in target._lbl_wake.styleSheet()


def test_wake_phrase_dropdown_lists_models_and_saves_selection(qapp):
    from samsara.ui.settings_qt import _SettingsWindow

    class StubApp:
        def __init__(self):
            self.config = {
                "wake_word_config": {
                    "phrase": "jarvis",
                    "phrase_options": ["jarvis", "computer"],
                    "audio": {"wake_command_timeout": 5.0},
                },
            }
            self._config_lock = threading.Lock()
            self.command_executor = types.SimpleNamespace(
                commands={}, find_command=lambda _text: None)
            self.hints = None
            self.alarm_manager = None

        def save_config(self):
            pass

        def play_sound(self, *args, **kwargs):
            pass

        def load_commands(self):
            return {}

        def load_training_data(self):
            pass

        def _load_sound_cache(self):
            pass

    window = _SettingsWindow(StubApp())
    combo = window._widgets["wake_word_phrase"]
    assert isinstance(combo, QComboBox)
    assert [combo.itemText(i) for i in range(combo.count())] == ["jarvis", "computer"]
    assert combo.currentText() == "jarvis"

    combo.setCurrentText("computer")
    updates = window._save_fns[1]({})
    assert updates["wake_word_config"]["phrase"] == "computer"
    labels = " ".join(label.text() for label in window.findChildren(QLabel))
    assert "Wake phrases are trained models. Custom phrases need a custom model." in labels


def test_one_wake_model_still_uses_a_dropdown(qapp):
    from samsara.ui.settings_qt import _SettingsWindow

    class StubApp:
        def __init__(self):
            self.config = {"wake_word_config": {"phrase_options": ["jarvis"]}}
            self._config_lock = threading.Lock()
            self.command_executor = types.SimpleNamespace(
                commands={}, find_command=lambda _text: None)
            self.hints = None
            self.alarm_manager = None

        def save_config(self):
            pass

        def play_sound(self, *args, **kwargs):
            pass

        def load_commands(self):
            return {}

        def load_training_data(self):
            pass

        def _load_sound_cache(self):
            pass

    window = _SettingsWindow(StubApp())
    combo = window._widgets["wake_word_phrase"]
    assert isinstance(combo, QComboBox)
    assert combo.count() == 1
    assert combo.currentText() == "jarvis"
