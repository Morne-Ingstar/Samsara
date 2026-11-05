"""1040: a single pause control, migration and tutorial round-trip."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QComboBox

from samsara import speech_pace as pace
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui.settings import dictation_page
from samsara.ui.settings_qt import _SettingsWindow
from samsara.ui.tutorial_try_steps import PaceSlider
from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.mark.parametrize('name,seconds,expected', [
    ('standard', 2, [.65, 2, 1, 5, .3]),
    ('relaxed', 5, [.65, 5, 2.5, 10, .2]),
    ('unhurried', 8, [.65, 8, 4, 15, .1]),
])
def test_stops_reproduce_legacy_outputs_without_double_scaling(name, seconds, expected):
    old = _schema_defaults()
    old.setdefault('accessibility', {})['speech_pace'] = name
    legacy = {key: pace.effective_value(old, key, 99) for key in pace._KIND}
    assert list(legacy.values()) == pytest.approx(expected)
    assert pace.derived_values(seconds) == pytest.approx(legacy)
    migrated = pace.with_pause(old, pace.pause_seconds(old))
    assert pace.profile(migrated) == name
    assert {key: pace.effective_value(migrated, key, 99) for key in pace._KIND} == pytest.approx(legacy)
    assert pace.with_pause(migrated, seconds) == migrated


def test_entire_slider_curve_is_bounded_and_directionally_monotonic():
    previous = None
    for tick in range(50, 801):
        values = pace.derived_values(tick / 100)
        for key, value in values.items():
            assert pace._BOUNDS[key][0] <= value <= pace._BOUNDS[key][1]
            if previous:
                if key == 'min_speech_duration':
                    assert value <= previous[key] + 1e-12
                else:
                    assert value >= previous[key] - 1e-12
        previous = values
    # 743 deliberately kept chunk timing flat at preset stops for responsive controls.
    assert pace.derived_values(8)['command_mode.dictate_utterance_silence_s'] == .65


@pytest.mark.parametrize('silence', [1.37, 3.26, 9.0])
def test_custom_migration_preserves_all_five_values_and_archives_on_movement(silence):
    raw = dict(zip(pace._KIND, [1.13, silence, 2.73, 17.23, .47]))
    old = {'accessibility': {'speech_pace': 'custom', 'speech_pace_custom': raw, 'other': 'kept'}}
    before = deepcopy(old)
    seconds = pace.pause_seconds(old)
    migrated = pace.with_pause(old, seconds)
    assert old == before
    assert pace.profile(migrated) == 'custom'
    for key, value in raw.items():
        assert pace.effective_value(migrated, key, 99) == value
    assert pace.with_pause(migrated, seconds) == migrated
    moved = pace.with_pause(migrated, 5 if seconds != 5 else 2)
    assert moved['accessibility']['speech_pace_custom'] == raw
    assert moved['accessibility']['speech_pause_legacy']['values'] == raw
    assert moved['accessibility']['other'] == 'kept'
    assert pace.profile(moved) == 'relaxed'
    assert {key: pace.effective_value(moved, key, 99) for key in raw} == pytest.approx(pace.derived_values(5))


@pytest.mark.parametrize('name', ['standard', 'relaxed', 'unhurried', 'slow', 'very slow'])
def test_hand_tuned_baselines_in_every_legacy_profile_survive_migration(name):
    old = {'accessibility': {'speech_pace': name}, 'silence_threshold': 1.3,
           'command_mode': {'dictate_utterance_silence_s': 1.21}, 'min_speech_duration': .47,
           'wake_word_config': {'quick_silence_timeout': 1.7, 'audio': {'wake_command_timeout': 4.6}}}
    expected = {key: pace.effective_value(old, key, 99) for key in pace._KIND}
    migrated = pace.with_pause(old, pace.pause_seconds(old))
    assert {key: pace.effective_value(migrated, key, 99) for key in pace._KIND} == pytest.approx(expected)
    assert pace.profile(migrated) == 'custom'


def test_five_raw_keys_absent_from_page_registry_and_schema_ui():
    keys = set(pace._KIND)
    for spec in dictation_page.BASE + dictation_page.ADVANCED:
        assert not keys.intersection(spec[3].split(','))
    for key in keys:
        assert SETTINGS_SCHEMA[key]['settings_ui'] is False
    assert SETTINGS_SCHEMA['accessibility.speech_pause_s']['min'] == .5
    assert SETTINGS_SCHEMA['accessibility.speech_pause_s']['max'] == 8
    # Absence must survive default merging so old profiles can be migrated.
    assert 'default' not in SETTINGS_SCHEMA['accessibility.speech_pause_s']


@pytest.fixture
def settings(qapp, qt_widget_scope, monkeypatch):
    from samsara import autostart
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    app = _StubApp(_schema_defaults())
    app.config.setdefault('accessibility', {}).update(other='kept')
    app.update_config_and_save = lambda changes: app.config.update(changes)
    app.command_executor._matcher = SimpleNamespace(list_commands=lambda: [])
    with qt_widget_scope(_SettingsWindow(app)) as window:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.apply_theme()
        window.show_tab('Dictation')
        window.resize(1000, 850)
        window.show()
        qapp.processEvents()
        yield window, app


def test_settings_save_derives_raw_keys_and_keeps_other_edits(settings):
    window, app = settings
    control = window._widgets['speech_pace']
    control.setSeconds(3.75)
    # 1040 FINISH IT 3: the source save itself must carry the slider and
    # nested derived keys; the complete Apply path writes all five below.
    modes = window._source_saves['modes'][0]({})
    assert modes['accessibility']['speech_pause_s'] == 3.75
    assert modes['accessibility']['other'] == 'kept'
    for key, expected in pace.derived_values(3.75).items():
        if '.' in key:
            assert pace._get(modes, key, -1) == pytest.approx(expected)
    updates = window._collect_destination_updates({})
    for key, expected in pace.derived_values(3.75).items():
        assert pace._get(updates, key, -1) == pytest.approx(expected)
    assert updates['accessibility']['speech_pause_s'] == 3.75
    assert updates['accessibility']['speech_pace'] == 'custom'
    assert updates['accessibility']['other'] == 'kept'
    assert all(not set(row.config_keys).intersection(pace._KIND) for row in window._destination_rows)
    assert not isinstance(control, QComboBox)


def test_try_it_starts_with_unsaved_value_and_keep_round_trips(settings, qapp):
    window, app = settings
    control = window._widgets['speech_pace']
    control.setSeconds(3.75)
    before = deepcopy(app.config)
    window._widgets['speech_pace_try'].click()
    trial = window._pace_trial
    try:
        page = trial._try_page
        assert page.stage == 2
        assert type(page.pace) is type(control) is PaceSlider
        assert page.pace.seconds() == 3.75
        assert app.config == before
        assert page.pace_sentence.text() and page.pace_sentence.isVisible()
        page.pace.setSeconds(6.25)
        assert app.config == before
        page.keep('pace')
        qapp.processEvents()
        assert control.seconds() == 6.25
        assert app.config['accessibility']['speech_pause_s'] == 6.25
        for key, expected in pace.derived_values(6.25).items():
            assert pace.effective_value(app.config, key, -1) == pytest.approx(expected)
        # Applying Settings after Keep must not restore stale hidden timing boxes.
        updates = window._collect_destination_updates({})
        assert updates['silence_threshold'] == 6.25
    finally:
        trial.close()
        trial.deleteLater()


def test_slider_readout_keyboard_and_scope_line(settings, qapp):
    window, _ = settings
    control = window._widgets['speech_pace']
    control.setSeconds(2)
    control.slider.setFocus()
    QTest.keyClick(control.slider, Qt.Key.Key_Right)
    assert control.seconds() == 2.05
    assert control.readout.text() == '2.05 s · Custom'
    assert [mark.text() for mark in control.stop_labels] == ['Standard', 'Relaxed', 'Unhurried']
    assert control.width() < window.width() - 100
    scope = window._rows_by_widget['speech_pace'].findChild(QLabel, 'speechPaceExplanation')
    assert scope.isVisible() and scope.wordWrap()
    assert 'Held dictation' in scope.text()
    assert 'recognizer' in scope.text()


def test_explicit_clamped_stop_replaces_custom_anchor_but_keeps_recovery(qapp, qt_widget_scope):
    old = {'accessibility': {'speech_pace': 'custom', 'speech_pace_custom': {'silence_threshold': 9}}}
    with qt_widget_scope(PaceSlider(old)) as control:
        assert control.currentData() == 'custom'
        assert control.seconds() == 8
        assert not control.changed
        control.setSeconds(8)
        assert control.currentData() == 'unhurried'
        chosen = pace.with_pause(old, control.seconds(), preserve_legacy=not control.changed)
        assert pace.profile(chosen) == 'unhurried'
        assert chosen['silence_threshold'] == 8
        assert chosen['accessibility']['speech_pause_legacy']['values']['silence_threshold'] == 9
