"""Beta.9 release identity and Home release-note contract; no Git or build."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_beta9_version_everywhere():
    from tools.check_release_version import check_versions
    assert check_versions(ROOT, 'v0.30.0-beta.9') == '0.30.0-beta.9'


def test_beta9_release_note_is_first_and_home_uses_its_lead():
    from samsara.ui.home_qt import changelog_summary

    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    headings = re.findall(r'^## \[([^]]+)\] - (.+)$', changelog, re.M)
    assert headings[0] == ('0.30.0-beta.9', '2026-10-20')
    assert headings[1][0] == '0.30.0-beta.8'
    section = changelog.split('## [0.30.0-beta.9]', 1)[1].split('## [0.30.0-beta.8]', 1)[0]
    lead = section.splitlines()[2]
    assert lead and not lead.startswith(('#', '-', '<')) and lead.endswith('.')
    assert changelog_summary(ROOT / 'CHANGELOG.md') == ('0.30.0-beta.9', lead)
    for title in ('Hands-free', 'Fixes', 'Settings'):
        assert f'### {title}\n\n-' in section
