"""Schema-owned Settings help and the 358 docs anchors (prompt 359)."""

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings_pages import _StubApp, _schema_defaults


FIXTURES = Path(__file__).parent / "fixtures" / "359"
DOCS_AUDIT = FIXTURES / "docs_audit.md"
DECISIONS = FIXTURES / "help_decisions.md"

# Explicit changes since the historical synthetic 359 inventory. New schema
# keys still fail unless a decision is recorded here or in that inventory.
DECISION_UPDATES = {
    # 826b/834r/862/877 / 888a: explicitly account for landed preference additions.
    'home.show_recent_words': ('DROP', 'The Home privacy row explains recent words.'),
    'ava.reply_window_seconds': ('DROP', 'The reply-window timing is configured outside Settings.'),
    'builtin_vocabulary_enabled': ('DROP', 'Dictionary owns the built-in vocabulary switch.'),
    'scroll.target_foreground': ('KEEP', 'Explain which window receives voice scrolling.'),
    'mouth_sounds.experiment_shadow': ('DROP', 'Offline shadow experiment, configured outside Settings.'),
    'mouth_sounds.pair_window_ms': ('DROP', 'Developer calibration bounds for the shadow detector.'),
    'mouth_sounds.refractory_ms': ('DROP', 'Developer shadow-detector refractory interval.'),
    'mouth_sounds.onset_rms': ('DROP', 'Developer transient threshold for the shadow detector.'),
    'agent_server.command_key_only': ('DROP', 'The AI-app setup control explains the command-key restriction.'),
    'agent_server.allow_typing': ('DROP', 'The AI-app setup control explains the typing opt-in.'),
    'agent_server.port': ('DROP', 'Advanced loopback port configured outside Settings.'),
    'hermes.profile': ('DROP', 'Optional Hermes setup owns its profile choice.'),
    'hermes.model': ('DROP', 'Optional Hermes setup owns its model choice.'),
    'hermes.effort': ('DROP', 'Optional Hermes setup owns its effort choice.'),
    'hermes.notice_accepted': ('DROP', 'Recorded by the explicit Hermes notice.'),
    'commands.grammar_tier': ('DROP', 'Config-only developer switch for grammar matching.'),
    'tasks.capture.ava_parse': ('DROP', 'Config-only optional Ava parsing of captured tasks.'),
    'dictation.speech_presence_gate': ('DROP', 'Developer speech-presence escape hatch, configured outside Settings.'),
    'task_hotkey': ('DROP', 'The Task held key row explains this shortcut.'),
    'streaming_mode': ('DROP', 'Retired capture flag retained only for migration.'),
    'command_mode.dictation_literal_commands_only': ('KEEP', 'Explain why argument-taking commands are typed while dictating.'),
    'command_mode.utterance_silence_s': ('KEEP', 'Explain the separate command and dictation pause thresholds.'),
    'command_mode.short_decode_enabled': ('DROP', 'Developer command-decode experiment, configured outside Settings.'),
    'hands_free.exact_commands': ('DROP', 'Config-only override; reserved session controls remain available.'),
    'hands_free.send_mode': ('DROP', 'The hands-free delivery choices explain staged and immediate output.'),
    'tts.feedback_level': ('DROP', 'The Voice feedback choices explain the speech levels.'),
    'ui.listening_display': ('DROP', 'The listening-display chooser includes its own explanation.'),
    'ui.live_surface.enabled': ('DROP', 'Legacy flag superseded by the listening-display chooser.'),
    'ava.conversation_log_enabled': ('DROP', 'The conversation log row describes its opt-in.'),
    'tts.network_voice_accepted': ('DROP', 'Acknowledgement recorded by the network voice notice.'),
    'diagnostics.stage_timing': ('KEEP', 'Explain that timing logs are local and contain no dictated text.'),
    'echo_cancellation.latency_ms': ('KEEP', 'Explain that this legacy option is retired.'),
}


def test_every_schema_key_has_a_decision_and_help_docs_home():
    audit = DOCS_AUDIT.read_text(encoding="utf-8")
    anchors = set(re.findall(r"settings/#([^\s|]+)", audit))
    decisions = re.findall(
        r"^\| `([^`]+)` \| (KEEP|DROP) \| ([^|]+) \|$",
        DECISIONS.read_text(encoding="utf-8"), re.MULTILINE,
    )
    # The fixed fixture retains the legacy speech key for migration coverage.
    decisions = [item for item in decisions if item[0] != "feedback.spoken_notices"
                 and item[0] not in DECISION_UPDATES]
    decisions.extend((key, decision, reason) for key, (decision, reason) in DECISION_UPDATES.items())
    anchors.update({'command_mode.dictation_literal_commands_only', 'command_mode.utterance_silence_s',
                    'scroll.target_foreground'})
    assert len(decisions) == len(SETTINGS_SCHEMA)
    assert {key for key, _decision, _reason in decisions} == set(SETTINGS_SCHEMA)
    decision_by_key = {key: (decision, reason) for key, decision, reason in decisions}
    assert all(reason.strip() for _decision, reason in decision_by_key.values())

    for key, entry in SETTINGS_SCHEMA.items():
        help_data = entry.get("help")
        decision, _reason = decision_by_key[key]
        assert decision == ("KEEP" if help_data else "DROP"), key
        if help_data:
            assert set(help_data) == {"why", "docs"}
            if key in {'echo_cancellation.enabled', 'echo_cancellation.latency_ms'}:
                assert entry['settings_ui'] is False
                assert help_data['why'] == 'This option is inactive because echo cancellation was retired.'
            elif key == 'ava_command_session.interpret_misses':
                from samsara.result_messages import AVA_INTERPRET_HELP_LONG
                assert help_data['why'] == AVA_INTERPRET_HELP_LONG
                assert len(re.findall(r"[.!?](?:\s|$)", help_data['why'])) == 5
            else:
                assert 2 <= len(re.findall(r"[.!?](?:\s|$)", help_data["why"])) <= 4, key
            assert help_data["docs"] == f"https://morneis.com/samsara/docs/settings/#{key}"
            assert key in anchors, key

    # Config-only entries still need a documented home or an explicit reason.
    no_home = set(re.findall(r"^\| `([^`]+)` \| config\.json only \|", audit, re.MULTILINE))
    no_home |= {
        "tasks.todoist.enabled", "tasks.todoist.api_token", "alarms.enabled",
        "alarms.nag_interval_seconds", "music_library", "wake_profiles",
        "diagnostics.watch_code_changes",
    }
    assert no_home <= set(SETTINGS_SCHEMA)
    assert all(SETTINGS_SCHEMA[key].get("help") or decision_by_key[key][1] for key in no_home)


def test_rows_only_show_schema_help_and_keyboard_link(qapp, qt_widget_scope):
    window = _SettingsWindow(_StubApp(_schema_defaults()))
    try:
        window.show()
        qapp.processEvents()
        for row in window._destination_rows:
            expected = next(
                (SETTINGS_SCHEMA[key]["help"] for key in row.config_keys
                 if key in SETTINGS_SCHEMA and "help" in SETTINGS_SCHEMA[key]),
                None,
            )
            assert row.help_data == expected, row.title
            assert (row.help is not None) == (expected is not None), row.title
            if expected:
                assert row.description not in expected["why"], row.title

        assert window._rows_by_widget["theme_combo"].help is None
        assert window._rows_by_widget["lang_combo"].help is None
        row = window._rows_by_widget["providerRoute"]
        window.show_tab("Ava")
        row.help.setFocus()
        QTest.keyClick(row.help, Qt.Key.Key_Return)
        assert row.help_panel.isVisibleTo(window)
        assert row.help_copy.text() == SETTINGS_SCHEMA["ava.provider_policy"]["help"]["why"]
        assert row.help_link.openExternalLinks()
        assert row.help_link.minimumHeight() >= 44
        assert row.help_link.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert SETTINGS_SCHEMA["ava.provider_policy"]["help"]["docs"] in row.help_link.text()
        assert "text-decoration: none;" in row.help_link.text() and all(
            "text-decoration: none;" in re.search(
                rf"QLabel{state}\s*\{{([^}}]+)\}}", row.help_link.styleSheet()
            ).group(1) for state in ("", ":hover", ":focus")
        )
        row.help_link.setFocus()
        QTest.keyClick(row.help_link, Qt.Key.Key_Escape)
        assert row.help_panel.isHidden()
        assert row.help.hasFocus()
    finally:
        with qt_widget_scope(window):
            window.hide()
