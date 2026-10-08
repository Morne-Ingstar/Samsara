"""Schema-owned Settings help and the 358 docs anchors (prompt 359)."""

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.ui.settings_qt import _SettingsWindow
from tests.test_settings_pages import _StubApp, _schema_defaults


REPORTS = Path.home() / "Documents" / "Claude" / "reports"
DOCS_AUDIT = REPORTS / "358" / "artifacts" / "docs_audit.md"
DECISIONS = REPORTS / "359" / "artifacts" / "help_decisions.md"


def test_every_schema_key_has_a_decision_and_help_docs_home():
    audit = DOCS_AUDIT.read_text(encoding="utf-8")
    anchors = set(re.findall(r"settings/#([^\s|]+)", audit))
    decisions = re.findall(
        r"^\| `([^`]+)` \| (KEEP|DROP) \| ([^|]+) \|$",
        DECISIONS.read_text(encoding="utf-8"), re.MULTILINE,
    )
    # The saved 359 audit predates 372's rename and later config-only keys.
    decisions = [item for item in decisions if item[0] != "feedback.spoken_notices"]
    decisions.append(("tts.feedback_level", "DROP",
                      "The Voice feedback row and its choices explain the speech levels."))
    for key, reason in {
        "tasks.capture.ava_parse": "Config-only optional Ava parsing of captured tasks.",
        "commands.grammar_tier": "Config-only developer switch for grammar matching.",
    }.items():
        if key in SETTINGS_SCHEMA:
            decisions.append((key, "DROP", reason))
    # 397 restores the distinct debugging guidance that 359 dropped.
    decisions = [
        (key, "KEEP", "Timing logs are for debugging and contain no dictated text.")
        if key == "diagnostics.stage_timing" else (key, decision, reason)
        for key, decision, reason in decisions
    ]
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
            assert 2 <= len(re.findall(r"[.!?](?:\s|$)", help_data["why"])) <= 4, key
            assert help_data["docs"] == f"https://morneis.com/samsara/docs/settings/#{key}"
            assert key in anchors, key

    # Section D calls these out as having no ordinary Settings page row.
    no_home = set(re.findall(r"^\| `([^`]+)` \| config\.json only \|", audit, re.MULTILINE))
    no_home |= {
        "tasks.todoist.enabled", "tasks.todoist.api_token", "alarms.enabled",
        "alarms.nag_interval_seconds", "music_library", "wake_profiles",
        "diagnostics.watch_code_changes",
    }
    assert no_home <= set(SETTINGS_SCHEMA)
    assert all(SETTINGS_SCHEMA[key].get("help") or decision_by_key[key][1] for key in no_home)


def test_rows_only_show_schema_help_and_keyboard_link(qapp):
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
        window.hide()
        window.deleteLater()
