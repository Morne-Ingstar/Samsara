"""Optional packs stay out of Setup; bundled wake and old installs stay usable."""
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from samsara.runtime_manifest import OWW_MODEL_FILENAMES
from samsara.ui import first_run_qt as fq
from tests.test_first_run_components import world


@pytest.mark.parametrize('gpu,visible', [(False, False), (True, True), (None, True)])
def test_settings_filters_hardware_but_keeps_unknown_selectable(qapp, world, gpu, visible):
    from PySide6.QtWidgets import QWidget, QPushButton
    host = QWidget()
    page = fq.ComponentsPage(host, on_later=lambda: None,
        manifest_loader=lambda: world['manifest'], app_root=world['app_root'],
        downloads_dir=world['downloads'], gpu_probe=lambda: gpu,
        include_installed=True, run_in_thread=False)
    try:
        page.refresh()
        assert ('cuda-pack' in page._rows) is visible
        assert 'command-model' not in page._rows
        if visible:
            action = page._rows['cuda-pack']['get']
            assert isinstance(action, QPushButton) and action.isEnabled() and action.text()
    finally:
        host.close()


@pytest.mark.parametrize('marker_kind', ['legacy', 'local', 'files'])
def test_old_installs_remain_readable_without_gpu(qapp, world, marker_kind):
    from PySide6.QtWidgets import QWidget, QLabel
    from samsara import components
    from samsara.cuda_detect import _REQUIRED_CUDA_DLLS
    component = world['manifest']['components'][2]
    if marker_kind == 'files':
        target = world['app_root']/component['install_dir']
        target.mkdir(parents=True)
        for name in _REQUIRED_CUDA_DLLS:
            (target/name).write_bytes(b'legacy DLL fixture')
    else:
        marker = (world['downloads']/'cuda-pack.installed' if marker_kind == 'legacy'
                  else components.local_component_marker(world['app_root'], component))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(component['sha256'], encoding='ascii')
    host = QWidget()
    page = fq.ComponentsPage(host, on_later=lambda: None,
        manifest_loader=lambda: world['manifest'], app_root=world['app_root'],
        downloads_dir=world['downloads'], gpu_probe=lambda: False,
        include_installed=True, run_in_thread=False)
    try:
        page.refresh()
        label = page._rows['cuda-pack']['get']
        assert isinstance(label, QLabel) and label.text() == 'Installed'
        assert label.isEnabled()
        page.start_fetch('cuda-pack')
        assert not world['requests']
        component['available'] = False
        page.refresh()
        assert page._rows['cuda-pack']['get'].text() == 'Installed'
    finally:
        host.close()


def test_bundled_models_satisfy_components_without_marker(world):
    component = world['manifest']['components'][1]
    target = world['app_root']/component['install_dir']
    target.mkdir(parents=True)
    for name in OWW_MODEL_FILENAMES:
        (target/name).write_bytes(b'bundled model fixture')
    assert fq.component_installed(component, world['app_root'], world['downloads'])
    (target/'hey_jarvis_v0.1.onnx').unlink()
    assert not fq.component_installed(component, world['app_root'], world['downloads'])


@pytest.mark.parametrize('include_installed', [False, True])
@pytest.mark.parametrize('installed', [False, True])
def test_bundled_wake_models_never_have_a_components_row(qapp, world, include_installed, installed):
    from PySide6.QtWidgets import QLabel, QWidget
    if installed:
        component = world['manifest']['components'][1]
        target = world['app_root']/component['install_dir']
        target.mkdir(parents=True)
        for name in OWW_MODEL_FILENAMES:
            (target/name).write_bytes(b'bundled model fixture')
    host = QWidget()
    page = fq.ComponentsPage(host, on_later=lambda: None,
        manifest_loader=lambda: world['manifest'], app_root=world['app_root'],
        downloads_dir=world['downloads'], gpu_probe=lambda: True,
        include_installed=include_installed, run_in_thread=False)
    try:
        assert [c['id'] for c in page.refresh()] == ['cuda-pack']
        assert set(page._rows) == {'cuda-pack'}
        assert not any('Wake-word models' in label.text() for label in page.widget.findChildren(QLabel))
        page.start_fetch('wake-word-models')
        assert not world['requests']
    finally:
        host.close()


def test_jarvis_initializes_locally_without_download(monkeypatch):
    from samsara.wake_detector import WakeWordDetector
    model = ModuleType('openwakeword.model')
    model.Model = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, 'openwakeword.model', model)
    detector = WakeWordDetector('jarvis')
    assert detector.is_available
    model.Model.assert_called_once_with(wakeword_models=['hey_jarvis'], inference_framework='onnx')


def test_build_requires_bundled_models_and_installer_cannot_download():
    root = Path(__file__).resolve().parents[1]
    spec = (root/'scripts/samsara.spec').read_text(encoding='utf-8')
    assert 'oww_model_filenames = list(OWW_MODEL_FILENAMES)' in spec
    assert "collect_data_files('openwakeword', subdir='resources/models', includes=oww_model_filenames)" in spec
    assert 'if _oww_missing:' in spec and 'raise SystemExit(' in spec
    installer = (root/'installer/samsara.iss').read_text(encoding='utf-8')
    assert '[Components]' not in installer and '[Types]' not in installer
    assert '--fetch-components' not in installer and 'ExtraDiskSpaceRequired' not in installer
    assert '[InstallDelete]' not in installer
