"""Beta.6 release identity and Home release-note contract; no Git or build."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_release_stamps_and_derived_consumers_agree():
    from tools.check_release_version import check_versions
    version = check_versions(ROOT, 'v0.30.0-beta.9')
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


def test_beta6_release_note_is_first_and_home_uses_its_lead():
    from samsara.ui.home_qt import changelog_summary

    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    headings = re.findall(r'^## \[([^]]+)\] - (.+)$', changelog, re.M)
    assert headings[3] == ('0.30.0-beta.6', '2026-10-17')
    assert headings[4][0] == '0.30.0-beta.5'
    section = changelog.split('## [0.30.0-beta.6]', 1)[1].split('## [0.30.0-beta.5]', 1)[0]
    lead = section.splitlines()[2]
    assert lead and not lead.startswith(('#', '-', '<'))
    assert lead.endswith('.') and 'Stand-in' not in lead
    current_lead = changelog.split('## [0.30.0-beta.9]', 1)[1].splitlines()[2]
    assert changelog_summary(ROOT / 'CHANGELOG.md') == ('0.30.0-beta.9', current_lead)
    for title in ('Ava', 'Dictation and hold-to-talk', 'Settings and setup',
                  'Commands', 'Daily-use polish'):
        assert f'### {title}\n\n-' in section
