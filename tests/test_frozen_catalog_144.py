"""Queue 144: the frozen artifact must carry the policy risk catalog.

These tests deliberately load the smoke harness by path, never `dictation`.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_SMOKE_PATH = REPO / "tools" / "frozen_smoke.py"
_spec = importlib.util.spec_from_file_location("frozen_smoke_144", _SMOKE_PATH)
frozen_smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frozen_smoke)


def _stage_catalog(dist_path: Path) -> Path:
    data_root = frozen_smoke.bundled_data_root(dist_path)
    data_root.mkdir(parents=True)
    path = data_root / frozen_smoke.CATALOG_NAME
    shutil.copyfile(REPO / "commands_catalog.json", path)
    return path


def test_smoke_catalog_check_accepts_parseable_frozen_artifact(tmp_path):
    path = _stage_catalog(tmp_path)

    check = frozen_smoke.check_bundled_command_catalog(tmp_path)

    assert check.passed is True
    assert str(path) in check.detail


def test_smoke_catalog_check_fails_when_artifact_is_removed(tmp_path):
    path = _stage_catalog(tmp_path)
    path.unlink()

    check = frozen_smoke.check_bundled_command_catalog(tmp_path)

    assert check.passed is False
    assert "missing" in check.detail


def test_smoke_catalog_check_rejects_non_catalog_json(tmp_path):
    path = frozen_smoke.bundled_data_root(tmp_path) / frozen_smoke.CATALOG_NAME
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")

    check = frozen_smoke.check_bundled_command_catalog(tmp_path)

    assert check.passed is False
    assert "commands list" in check.detail


_COUNT_CHILD = """
import json
import sys
from pathlib import Path

sys.path.insert(0, {repo!r})
from tools.dump_command_metadata import build_executor
from samsara import command_catalog, execution_policy as ep, plugin_commands

build_executor()  # production registry loader; never imports dictation.py
phrases = {{entry['phrase'] for entry in plugin_commands._REGISTRY.values()}}
def confirmation_count(root):
    command_catalog.ROOT = Path(root)
    ep.clear_catalog_risk_cache()
    return sum(
        ep.classify(phrase)[0] in (ep.RISK_UNKNOWN, ep.RISK_DESTRUCTIVE)
        for phrase in phrases
    )

print(json.dumps({{
    'plugins': len(phrases),
    'source': confirmation_count({source!r}),
    'frozen': confirmation_count({frozen!r}),
    'absent': confirmation_count({absent!r}),
}}))
"""


def test_frozen_catalog_copy_preserves_all_confirmation_classifications(tmp_path):
    """The packaged runtime copy must classify exactly like the source catalog.

    The explicit absent-file measurement establishes why smoke must reject a
    missing artifact. Equality is source catalog versus staged frozen catalog:
    a release cannot validly have the absent-file result.
    """
    _stage_catalog(tmp_path)
    code = _COUNT_CHILD.format(
        repo=str(REPO),
        source=str(REPO),
        frozen=str(frozen_smoke.bundled_data_root(tmp_path)),
        absent=str(tmp_path / "missing_runtime_root"),
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO), check=True,
        capture_output=True, text=True,
    )
    counts = json.loads(result.stdout)

    assert counts == {
        "plugins": 208,
        "source": 20,
        "frozen": 20,
        "absent": 64,
    }
