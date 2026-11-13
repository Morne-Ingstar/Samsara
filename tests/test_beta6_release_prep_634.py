"""Beta.6 release identity and Home release-note contract; no Git or build."""
from pathlib import Path
import re

# 875b: current identity changes; historical release assertions remain intact.
ROOT = Path(__file__).resolve().parents[1]


def test_release_stamps_and_derived_consumers_agree():
    from tools.check_release_version import check_versions
    # 1214b: pin the current release; retain historical release checks.
    version = check_versions(ROOT, 'v0.42.2')
    batch = (ROOT / 'build_release.bat').read_text(encoding='utf-8')
    # 875b: the selected interpreter reads the canonical version before naming outputs.
    assert "_string_assignment(Path('samsara/__init__.py'), '__version__')" in batch
    assert 'do set "VERSION=%%V"' in batch
    assert 'if not defined VERSION (' in batch
    assert batch.index('if not defined PYTHON_EXE') < batch.index('do set "VERSION=%%V"')
    assert batch.index('do set "VERSION=%%V"') < batch.index('title Building Samsara')
    assert batch.index('title Building Samsara') < batch.index('set "ARCHIVE=')
    assert not re.search(r'^set VERSION=', batch, re.M)
    assert 'title Building Samsara v%VERSION%\n' in batch
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    # 940: 914 moved public downloads to the now-published stamped release.
    assert set(re.findall(r'releases/tag/v([^/)]+)', readme)) == {version}
    installer = (ROOT / 'installer/samsara.iss').read_text(encoding='utf-8')
    builder = (ROOT / 'tools/build_installer.cmd').read_text(encoding='utf-8')
    assert 'AppVersion={#AppVersion}' in installer
    assert "_string_assignment(Path('samsara/__init__.py'), '__version__')" in builder
    spec = (ROOT / 'scripts/samsara.spec').read_text(encoding='utf-8')
    assert 'from samsara import __version__' in spec
    assert "f'Samsara-GestureControl-v{__version__}.zip'" in spec


def test_beta6_release_note_is_first_and_home_uses_its_lead(qapp, monkeypatch, qt_widget_scope):
    from samsara.ui.home_qt import changelog_summary

    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    headings = re.findall(r'^## \[([^]]+)\] - (.+)$', changelog, re.M)
    assert ('0.30.0-beta.6', '2026-10-17') in headings
    assert '0.30.0-beta.5' in [version for version, date in headings]
    section = changelog.split('## [0.30.0-beta.6]', 1)[1].split('## [0.30.0-beta.5]', 1)[0]
    lead = section.splitlines()[2]
    assert lead and not lead.startswith(('#', '-', '<'))
    assert lead.endswith('.') and 'Stand-in' not in lead
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
    for title in ('Ava', 'Dictation and hold-to-talk', 'Settings and setup',
                  'Commands', 'Daily-use polish'):
        assert f'### {title}\n\n-' in section
