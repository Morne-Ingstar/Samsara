"""Beta.10 hotfix release identity, edge-tts pin and Home release-note contract; no Git or build."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_beta10_version_everywhere():
    from tools.check_release_version import check_versions
    assert check_versions(ROOT, 'v0.30.0-beta.10') == '0.30.0-beta.10'


def test_beta10_pins_the_edge_tts_that_signs_in_the_new_way():
    reqs = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    assert 'edge-tts==7.3.1' in reqs.splitlines()


def test_beta10_release_note_is_first_and_home_uses_its_lead():
    from samsara.ui.home_qt import changelog_summary

    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    headings = re.findall(r'^## \[([^]]+)\] - (.+)$', changelog, re.M)
    assert headings[0] == ('0.30.0-beta.10', '2026-10-23')
    assert headings[1][0] == '0.30.0-beta.9'
    section = changelog.split('## [0.30.0-beta.10]', 1)[1].split('## [0.30.0-beta.9]', 1)[0]
    lead = section.splitlines()[2]
    assert lead and not lead.startswith(('#', '-', '<')) and lead.endswith('.')
    assert changelog_summary(ROOT / 'CHANGELOG.md') == ('0.30.0-beta.10', lead)
    assert '### Fixes\n\n-' in section
