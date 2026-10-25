"""Beta.9 release identity and Home release-note contract; no Git or build."""
from pathlib import Path
import re

# 875b: current identity changes; historical release assertions remain intact.
ROOT = Path(__file__).resolve().parents[1]


def test_beta9_version_everywhere():
    from tools.check_release_version import check_versions
    assert check_versions(ROOT, 'v0.40.0') == '0.40.0'


def test_beta9_release_note_is_first_and_home_uses_its_lead():
    from samsara.ui.home_qt import changelog_summary

    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    headings = re.findall(r'^## \[([^]]+)\] - (.+)$', changelog, re.M)
    assert ('0.30.0-beta.9', '2026-10-20') in headings
    assert '0.30.0-beta.8' in [version for version, date in headings]
    section = changelog.split('## [0.30.0-beta.9]', 1)[1].split('## [0.30.0-beta.8]', 1)[0]
    lead = section.splitlines()[2]
    assert lead and not lead.startswith(('#', '-', '<')) and lead.endswith('.')
    current_lead = changelog.split('## [0.40.0]', 1)[1].splitlines()[2]
    assert changelog_summary(ROOT / 'CHANGELOG.md') == ('0.40.0', current_lead)
    for title in ('Hands-free', 'Fixes', 'Settings'):
        assert f'### {title}\n\n-' in section
