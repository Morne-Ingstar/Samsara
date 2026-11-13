"""Beta.10 hotfix release identity, edge-tts pin and Home release-note contract; no Git or build."""
from pathlib import Path
import re

# 875b: current identity changes; historical release assertions remain intact.
ROOT = Path(__file__).resolve().parents[1]


def test_beta10_version_everywhere():
    from tools.check_release_version import check_versions
    # 1214b: pin the current release; retain historical release checks.
    assert check_versions(ROOT, 'v0.42.2') == '0.42.2'


def test_beta10_pins_the_edge_tts_that_signs_in_the_new_way():
    reqs = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    assert 'edge-tts==7.3.1' in reqs.splitlines()


def test_beta10_release_note_is_first_and_home_uses_its_lead(qapp, monkeypatch, qt_widget_scope):
    from samsara.ui.home_qt import changelog_summary

    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    headings = re.findall(r'^## \[([^]]+)\] - (.+)$', changelog, re.M)
    assert ('0.30.0-beta.10', '2026-10-23') in headings
    assert '0.30.0-beta.9' in [version for version, date in headings]
    section = changelog.split('## [0.30.0-beta.10]', 1)[1].split('## [0.30.0-beta.9]', 1)[0]
    lead = section.splitlines()[2]
    assert lead and not lead.startswith(('#', '-', '<')) and lead.endswith('.')
    assert headings[0] == ('0.42.2', '2026-11-13')
    # 1014: release history stays above; the corrected lead is opened in What's new.
    from PySide6.QtWidgets import QLabel
    from samsara.ui import home_qt, home_signals
    from tests.test_home_qt import _app, _FakeSettings

    expected = '0.42.2 - cleaner dictation that keeps your words and joins your sentences, easier hands-free, and more voice commands.'
    assert changelog_summary(ROOT / 'CHANGELOG.md') == ('0.42.2', expected)
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(home_qt, '_user_word_count', lambda app: 0)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, 'diagnostic_hints', lambda app: [])
    with qt_widget_scope(home_qt.HomePage(_app())) as home:
        home._whats_new_button.click()
        dialog = home._whats_new_button.dialog
        assert dialog.isVisible()
        assert dialog.findChild(QLabel, 'releaseSummary').text() == expected
    assert '### Fixes\n\n-' in section
