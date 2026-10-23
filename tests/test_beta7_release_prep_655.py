"""Beta.7 release identity and Home release-note contract; no Git or build."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_release_stamps_and_derived_consumers_agree():
    from tools.check_release_version import check_versions
    version = check_versions(ROOT, 'v0.30.0-beta.10')
    batch = (ROOT / 'build_release.bat').read_text(encoding='utf-8')
    assert re.search(r'^set VERSION=(.*)$', batch, re.M).group(1) == version
    assert f'title Building Samsara v{version}\n' in batch
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert set(re.findall(r'releases/tag/v(0\.30\.0-beta\.\d+)', readme)) == {version}
    installer = (ROOT / 'installer/samsara.iss').read_text(encoding='utf-8')
    builder = (ROOT / 'tools/build_installer.cmd').read_text(encoding='utf-8')
    assert 'AppVersion={#AppVersion}' in installer
    assert "_string_assignment(Path('samsara/__init__.py'), '__version__')" in builder
    spec = (ROOT / 'scripts/samsara.spec').read_text(encoding='utf-8')
    assert 'from samsara import __version__' in spec
    assert "f'Samsara-GestureControl-v{__version__}.zip'" in spec


def test_beta7_release_note_is_first_and_home_uses_its_lead():
    from samsara.ui.home_qt import changelog_summary

    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    headings = re.findall(r'^## \[([^]]+)\] - (.+)$', changelog, re.M)
    assert headings[3] == ('0.30.0-beta.7', '2026-10-17')
    assert headings[4][0] == '0.30.0-beta.6'
    section = changelog.split('## [0.30.0-beta.7]', 1)[1].split('## [0.30.0-beta.6]', 1)[0]
    lead = section.splitlines()[2]
    assert lead and not lead.startswith(('#', '-', '<'))
    assert lead.endswith('.') and 'Stand-in' not in lead
    current_lead = changelog.split('## [0.30.0-beta.10]', 1)[1].splitlines()[2]
    assert changelog_summary(ROOT / 'CHANGELOG.md') == ('0.30.0-beta.10', current_lead)
    for title in ('Updates', 'Setup and help', 'Ava and selected text', 'Listening display'):
        assert f'### {title}\n\n-' in section


def test_beta6_manual_upgrade_notice_is_in_current_release_notes():
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    section = changelog.split('## [0.30.0-beta.7]', 1)[1].split('## [0.30.0-beta.6]', 1)[0]
    assert 'Anyone on beta.6 must install beta.7 once by hand' in section
    assert '[download page](https://github.com/Morne-Ingstar/Samsara/releases/latest)' in section
    assert "because beta.6's updater has this bug" in section
