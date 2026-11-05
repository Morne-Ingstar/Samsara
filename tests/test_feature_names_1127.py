"""1127: inventory of text on paths that specifically describe the wake listener."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

from samsara.audio_engine import wake_consumer
from samsara.ui import home_signals, main_window_qt
from samsara import help_terms, presentation_state

ROOT = Path(__file__).resolve().parents[1]


def strings(node):
    # Ignore docstrings and internal identifier names; inspect actual copy.
    parents = {c: p for p in ast.walk(node) for c in ast.iter_child_nodes(p)}
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
            and isinstance(n.value, str) and not isinstance(parents.get(n), ast.Expr)]


def test_wake_listener_string_inventory_never_calls_it_hands_free():
    source = ast.parse((ROOT / 'samsara/ui/home_signals.py').read_text(encoding='utf-8'))
    state = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'hands_free_state')
    inventory = [home_signals.HANDS_FREE, home_signals.RESTART_HANDS_FREE_LABEL,
                 *wake_consumer.FATAL_REASONS.values(), *main_window_qt._EYE_WORDS.values(),
                 *strings(state)]
    for text in inventory:
        assert 'hands-free' not in text.lower(), text
        assert 'hands free' not in text.lower(), text
    assert home_signals.HANDS_FREE == 'Wake word'
    assert home_signals.RESTART_HANDS_FREE_LABEL == 'Restart wake word'


def test_missing_mic_and_stopped_wake_have_working_actions():
    app = SimpleNamespace(config={'wake_word_enabled': True},
        _hands_free_fault=wake_consumer.fault_from(OSError('device'), at=0))
    state = home_signals.hands_free_state(app)
    assert state.name == 'Wake word'
    assert state.action == home_signals.ACTION_RESTART_HANDS_FREE
    assert 'Restart wake word' in state.detail
    app._hands_free_fault = None
    app._no_microphone = True
    state = home_signals.hands_free_state(app)
    assert 'Wake word' in state.detail
    assert state.action == home_signals.ACTION_SETUP_MICROPHONE


def test_hands_free_definition_never_requires_holding_a_key():
    text = help_terms.explanation('hands-free').body
    assert 'without holding a key' in text
    assert 'Hold keeps it open' not in text
    assert 'Hold to talk records only while you hold' in text


def test_hold_and_wake_names_in_tray():
    assert presentation_state.mode_display({'wake_word_enabled': True}, 'ready') == 'Hold + Wake'


def test_help_defines_the_three_features_in_real_settings_host(qapp, qt_widget_scope):
    from PySide6.QtWidgets import QLabel
    from tests._theme_stub_app import build_settings_window
    window = build_settings_window({'ui': {'live_surface': {'enabled': True}}})
    with qt_widget_scope(window):
        window.show_tab('Help')
        qapp.processEvents()
        words = window.findChild(QLabel, 'featureWords')
        assert words is not None and words.isVisible()
        assert all(term in words.text() for term in ('Words Samsara uses', 'Wake word', 'Hands-free', 'Hold to talk'))
        assert words.wordWrap() and not words.font().underline()
