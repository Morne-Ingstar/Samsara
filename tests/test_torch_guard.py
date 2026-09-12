"""Exercise guard decisions and real downstream imports in fresh processes."""

import importlib
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import ModuleType

import pytest


@pytest.fixture
def load_guard(monkeypatch, caplog):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delenv("SAMSARA_ALLOW_TORCH", raising=False)
    caplog.set_level(logging.INFO, logger="Samsara.samsara.torch_guard")
    guards = []

    # Full-suite hygiene: if an earlier, unrelated test in this same process
    # imported the real `dictation` module, its module-level `samsara.
    # torch_guard.install()` call (dictation.py's own startup line) already
    # put the REAL shared module's `_finder` singleton in sys.meta_path --
    # a different object from every fixture-local copy this file loads via
    # importlib.util below, so nothing here would otherwise ever remove it.
    # Start every test on a clean slate regardless of what ran before it.
    _real_guard_before = sys.modules.get("samsara.torch_guard")
    if _real_guard_before is not None:
        _real_guard_before.uninstall()

    def load():
        path = Path(__file__).resolve().parents[1] / "samsara" / "torch_guard.py"
        spec = importlib.util.spec_from_file_location("samsara.torch_guard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        guards.append(module)
        return module

    yield load
    for guard in guards:
        guard.uninstall()
    # Full-suite hygiene: if some other test in this process imported the
    # real `dictation` module, its module-level `samsara.torch_guard.
    # install()` (dictation.py's own startup line) put the REAL shared
    # module's `_finder` singleton in sys.meta_path -- a different object
    # from any of this fixture's own importlib.util-loaded copies above, so
    # the loop right above never touches it. Uninstall it too, so a test
    # here that proves "torch imports normally again" isn't defeated by an
    # unrelated file's earlier import of the real module.
    real_guard = sys.modules.get("samsara.torch_guard")
    if real_guard is not None:
        real_guard.uninstall()


def test_import_has_no_guard_side_effects(load_guard, caplog):
    before = list(sys.meta_path)
    load_guard()

    assert sys.meta_path == before
    assert "torch" not in sys.modules
    assert not any("[TORCH-GUARD]" in r.message for r in caplog.records)


def test_installs_when_torch_absent(load_guard, caplog):
    guard = load_guard()
    guard.install()

    assert "torch" not in sys.modules
    assert sys.meta_path[0] is guard._finder
    messages = [r for r in caplog.records if "[TORCH-GUARD]" in r.message]
    assert len(messages) == 1
    assert messages[0].levelno == logging.INFO
    assert "enabled" in messages[0].message


@pytest.mark.parametrize("existing", [ModuleType("torch"), None])
def test_noop_when_torch_present(load_guard, monkeypatch, caplog, existing):
    monkeypatch.setitem(sys.modules, "torch", existing)
    before = list(sys.meta_path)

    guard = load_guard()
    guard.install()

    assert sys.modules["torch"] is existing
    assert sys.meta_path == before
    messages = [r for r in caplog.records if "[TORCH-GUARD]" in r.message]
    assert len(messages) == 1
    assert messages[0].levelno == logging.INFO
    assert "disabled (torch already in sys.modules)" in messages[0].message


def test_env_override_disables(load_guard, monkeypatch, caplog):
    monkeypatch.setenv("SAMSARA_ALLOW_TORCH", "1")
    before = list(sys.meta_path)

    guard = load_guard()
    guard.install()

    assert "torch" not in sys.modules
    assert sys.meta_path == before
    messages = [r for r in caplog.records if "[TORCH-GUARD]" in r.message]
    assert len(messages) == 1
    assert messages[0].levelno == logging.INFO
    assert "disabled (SAMSARA_ALLOW_TORCH=1)" in messages[0].message


def test_import_torch_raises_importerror(load_guard):
    guard = load_guard()
    guard.install()
    try:
        with pytest.raises(ImportError, match="SAMSARA_ALLOW_TORCH"):
            importlib.import_module("torch")
        assert "torch" not in sys.modules
    finally:
        guard.uninstall()


def test_finder_blocks_only_torch_and_its_submodules(load_guard):
    guard = load_guard()
    for name in ("torch", "torch.nn", "torch.nn.functional"):
        with pytest.raises(ImportError, match="SAMSARA_ALLOW_TORCH"):
            guard._finder.find_spec(name)
    for name in ("numpy", "scipy.stats", "torchvision", "pytorch", "torch_extra"):
        assert guard._finder.find_spec(name) is None


@pytest.mark.parametrize("allow_torch", [False, True])
def test_install_is_idempotent(load_guard, monkeypatch, caplog, allow_torch):
    if allow_torch:
        monkeypatch.setenv("SAMSARA_ALLOW_TORCH", "1")
    guard = load_guard()
    guard.install()
    before = sys.modules.copy()
    finders_before = list(sys.meta_path)
    caplog.clear()

    guard.install()
    guard.install()

    assert sys.modules == before
    assert sys.meta_path == finders_before
    assert not any("[TORCH-GUARD]" in r.message for r in caplog.records)


def test_uninstall_restores_normal_importing(load_guard, monkeypatch, tmp_path):
    # Use a real import from disk without loading the heavyweight torch package.
    (tmp_path / "torch.py").write_text("guard_test_marker = True\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    before = list(sys.meta_path)
    guard = load_guard()
    guard.install()
    try:
        with pytest.raises(ImportError, match="SAMSARA_ALLOW_TORCH"):
            importlib.import_module("torch")
        guard.uninstall()
        guard.uninstall()
        assert sys.meta_path == before
        assert importlib.import_module("torch").guard_test_marker is True
    finally:
        guard.uninstall()
        sys.modules.pop("torch", None)


def test_install_after_uninstall(load_guard):
    guard = load_guard()
    guard.install()
    guard.uninstall()
    guard.install()
    assert "torch" not in sys.modules
    assert sys.meta_path[0] is guard._finder
    assert sys.meta_path.count(guard._finder) == 1


def _run_guarded_probe(tmp_path, probe):
    env = os.environ.copy()
    env.pop("SAMSARA_ALLOW_TORCH", None)
    env["SAMSARA_HOME_DIR"] = str(tmp_path)
    script = (
        "import sys\n"
        "import samsara.torch_guard as guard\n"
        "guard.install()\n"
        "try:\n"
        "    assert 'torch' not in sys.modules\n"
        "    assert sys.meta_path[0] is guard._finder\n"
        + textwrap.indent(textwrap.dedent(probe).strip() + "\n", "    ")
        + "    assert 'torch' not in sys.modules\n"
        "finally:\n"
        "    guard.uninstall()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_scipy_array_api_probe_with_guard(tmp_path):
    _run_guarded_probe(tmp_path, """
        import scipy.stats
        import numpy as np
        from scipy._lib.array_api_compat.common import _helpers

        assert _helpers.is_torch_array(np.zeros(3)) is False
    """)


def test_openwakeword_hey_jarvis_model_with_guard(tmp_path):
    _run_guarded_probe(tmp_path, """
        import openwakeword
        from openwakeword.model import Model

        model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        assert "hey_jarvis" in model.models
    """)
