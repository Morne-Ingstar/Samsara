"""Release identity and staged agent-document contracts; no build or Git."""
import ast
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

import pytest

# 875b: current identity changes; historical release assertions remain intact.
ROOT = Path(__file__).resolve().parents[1]
SKILLS = ('Docs/agents/samsara/SKILL.md', 'Docs/hermes/samsara-voice/SKILL.md')


def test_release_stamps_and_derived_consumers_agree():
    from tools.check_release_version import check_versions
    version = check_versions(ROOT, 'v0.40.0')
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
    # 914: the README identifies the stamped version without pinning a release literal.
    assert f'v{version}' in readme
    installer = (ROOT / 'installer/samsara.iss').read_text(encoding='utf-8')
    builder = (ROOT / 'tools/build_installer.cmd').read_text(encoding='utf-8')
    assert 'AppVersion={#AppVersion}' in installer
    assert "_string_assignment(Path('samsara/__init__.py'), '__version__')" in builder
    spec = (ROOT / 'scripts/samsara.spec').read_text(encoding='utf-8')
    assert 'from samsara import __version__' in spec
    assert "f'Samsara-GestureControl-v{__version__}.zip'" in spec


def _documentation_nodes():
    tree = ast.parse((ROOT / 'scripts/samsara.spec').read_text(encoding='utf-8'))
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'AGENT_SKILL_PATHS'
                              for t in n.targets))
    loops = [n for n in tree.body if isinstance(n, ast.For)
             and isinstance(n.iter, ast.Name) and n.iter.id == 'AGENT_SKILL_PATHS']
    assert len(loops) == 2
    return assignment, loops


def test_spec_stages_both_skills_and_copies_exact_bytes_beside_exe(tmp_path):
    assignment, loops = _documentation_nodes()
    scope = {'Path': Path, 'app_dir': ROOT, 'datas': [], 'shutil': shutil,
             'coll': SimpleNamespace(name=str(tmp_path))}
    exec(compile(ast.Module(body=[assignment, loops[0]], type_ignores=[]),
                 'skill_datas', 'exec'), scope)
    assert scope['AGENT_SKILL_PATHS'] == SKILLS
    for source, destination in scope['datas']:
        relative = Path(destination) / Path(source).name
        assert relative.as_posix() in SKILLS
        staged = tmp_path / '_internal' / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, staged)
    exec(compile(ast.Module(body=[loops[1]], type_ignores=[]), 'root_docs', 'exec'), scope)
    for relative in SKILLS:
        assert (tmp_path / relative).read_bytes() == (ROOT / relative).read_bytes()


def test_frozen_import_hook_has_no_duplicate_shutil_import():
    spec = (ROOT / 'scripts/samsara.spec').read_text(encoding='utf-8')
    hook_start = spec.index("_runtime_import_hook.write_text(r'''\n") + len(
        "_runtime_import_hook.write_text(r'''\n")
    hook_end = spec.index("\n'''", hook_start)
    hook = spec[hook_start:hook_end]
    assert hook.splitlines().count('import shutil') == 0
    assert spec.splitlines().count('import shutil') == 1


def test_upgrade_guide_separates_closed_file_copies_from_in_app_imports():
    guide = (ROOT / 'Docs/UPGRADING_TO_BETA5.md').read_text(encoding='utf-8')
    assert 'Before the first start' in guide
    assert 'with Samsara closed — file copies' in guide
    assert 'After first launch — in-app Imports' in guide
    assert 'Settings > Commands' in guide
    assert 'choose **Import…**' in guide


def test_missing_staged_skill_fails_packaging(tmp_path):
    assignment, loops = _documentation_nodes()
    scope = {'Path': Path, 'shutil': shutil, 'coll': SimpleNamespace(name=str(tmp_path))}
    with pytest.raises(FileNotFoundError):
        exec(compile(ast.Module(body=[assignment, loops[1]], type_ignores=[]),
                     'missing_doc', 'exec'), scope)


def test_release_checks_require_root_docs_before_archive():
    workflow = (ROOT / '.github/workflows/release-build.yml').read_text(encoding='utf-8')
    batch = (ROOT / 'build_release.bat').read_text(encoding='utf-8')
    for relative in SKILLS:
        windows = relative.replace('/', '\\')
        assert windows in batch and windows in workflow
        assert f'`{relative}`' in (ROOT / relative).read_text(encoding='utf-8')
    assert 'if not exist "dist\\Samsara\\%%~S"' in batch
    assert batch.index('for %%S') < batch.index('7z a ')
    assert 'Test-Path "dist\\Samsara\\$skill" -PathType Leaf' in workflow
    assert workflow.index('Agent instructions missing') < workflow.index('Write-Host "Build OK:')
    assert 'exit /b 1' in batch[batch.index('for %%S'):batch.index('echo Build successful:')]
    assert 'exit 1' in workflow[workflow.index('foreach ($skill'):workflow.index('Write-Host "Build OK:')]


def test_upgrade_guide_covers_layouts_and_manual_snapshot_gap():
    guide = (ROOT / 'Docs/UPGRADING_TO_BETA5.md').read_text(encoding='utf-8')
    for path in ('OLD\\_internal\\commands.json', 'OLD\\commands.json',
                 'OLD\\_internal\\plugins\\commands\\', 'OLD\\plugins\\commands\\',
                 'OLD\\_internal\\profiles\\', 'OLD\\profiles\\',
                 'OLD\\_internal\\samsara\\ui\\profiles\\',
                 '%USERPROFILE%\\.samsara', '%LOCALAPPDATA%\\Programs\\Samsara',
                 '%ProgramFiles%\\Samsara', 'SAMSARA_HOME_DIR'):
        assert path in guide
    assert 'A manual installer does not create the in-app snapshot.' in guide
    assert 'Do not replace it wholesale' in guide
    assert 'Leave the installer’s launch checkbox off.' in guide
    changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    section = changelog.split('## [0.30.0-beta.5]')[1].split('## [0.30.0-beta.4]')[0]
    assert 'Docs/UPGRADING_TO_BETA5.md' in section
    for group in ('Fixes from the beta.4 audit', 'Ava & OpenRouter', 'AI apps',
                  'Commands', 'Daily-use polish'):
        assert f'### {group}' in section
