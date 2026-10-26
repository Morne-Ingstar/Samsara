"""917: no GPU hardware, bootstrap, live profiles, downloads or app processes."""
import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.gpu_fallback import ResilientSpeechModel, load_resilient_model, report_delivered
from tools import gen_release_manifest as gen


CUDA_ERROR = 'Library cublas64_12.dll is not found or cannot be loaded'


def app_stub():
    return SimpleNamespace(config={'model_size': 'small', 'device': 'cuda', 'compute_type': 'float16'},
                           device_type='cuda', compute_type='float16')


@pytest.mark.parametrize('stage', ['call', 'lazy', 'after_segment'])
@pytest.mark.parametrize('message', [CUDA_ERROR, 'Could not load cudnn64_9.dll', 'CUDA failed with error 35'])
def test_decode_retries_exact_audio_once_on_cpu_and_notifies_once(stage, message, caplog):
    app = app_stub()
    saved = deepcopy(app.config)
    audio, info = object(), object()
    cpu = Mock()
    cpu.transcribe.side_effect = lambda *a, **k: (iter(['cpu result']), info)
    factory, notice = Mock(return_value=cpu), Mock()

    def fail(*a, **k):
        if stage == 'call':
            raise RuntimeError(message)
        def segments():
            if stage == 'after_segment':
                yield 'discard this partial'
            raise RuntimeError(message)
        return segments(), object()

    gpu = Mock(transcribe=Mock(side_effect=fail))
    model = ResilientSpeechModel(app, gpu, factory, device='cuda', notify=notice)
    app.model = model
    segments, returned_info = model.transcribe(audio, beam_size=5, language='en')
    assert list(segments) == ['cpu result'] and returned_info is info
    # 917b: the same audio/language is retried with bounded CPU parameters.
    assert gpu.transcribe.call_args.args == cpu.transcribe.call_args.args
    assert cpu.transcribe.call_args.kwargs == dict(beam_size=3, language='en', best_of=1,
                                                  temperature=0.0, max_new_tokens=192)
    assert cpu.transcribe.call_args.args[0] is audio
    factory.assert_called_once_with()
    # 917b: decoding alone must not announce a result the caller can reject.
    notice.assert_not_called()
    report_delivered(app)
    notice.assert_called_once_with()
    assert app.device_type == 'cpu' and app.compute_type == 'int8'
    assert app.config == saved
    assert list(model.transcribe(audio, beam_size=1)[0]) == ['cpu result']
    factory.assert_called_once()
    report_delivered(app)
    notice.assert_called_once()
    assert sum('[GPU-FALLBACK]' in r.message for r in caplog.records) == 1


def test_encode_failure_also_retries_same_features():
    app, features = app_stub(), object()
    cpu = Mock(encode=Mock(return_value='cpu encoded'))
    factory, notice = Mock(return_value=cpu), Mock()
    gpu = Mock(encode=Mock(side_effect=RuntimeError(CUDA_ERROR)))
    model = ResilientSpeechModel(app, gpu, factory, device='cuda', notify=notice)
    assert model.encode(features, to_cpu=True) == 'cpu encoded'
    assert gpu.encode.call_args == cpu.encode.call_args
    assert cpu.encode.call_args.args[0] is features
    factory.assert_called_once()
    # 917b: encode is preparation, not final delivery.
    notice.assert_not_called()


@pytest.mark.parametrize('error', [ValueError('invalid audio'), RuntimeError('invalid model shape'),
                                 OSError('disk read failed')])
def test_non_gpu_errors_propagate_without_changing_device(error):
    factory, notice = Mock(), Mock()
    model = ResilientSpeechModel(app_stub(), Mock(transcribe=Mock(side_effect=error)),
                                factory, device='cuda', notify=notice)
    with pytest.raises(type(error), match=str(error)):
        model.transcribe(object())
    factory.assert_not_called()
    notice.assert_not_called()


def test_cpu_retry_failure_propagates_without_a_reload_loop():
    cpu = Mock(transcribe=Mock(side_effect=RuntimeError(CUDA_ERROR)))
    factory, notice = Mock(return_value=cpu), Mock()
    model = ResilientSpeechModel(app_stub(), Mock(transcribe=Mock(side_effect=RuntimeError(CUDA_ERROR))),
                                factory, device='cuda', notify=notice)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='cublas64_12'):
            model.transcribe(object())
    factory.assert_called_once()
    assert cpu.transcribe.call_count == 2
    notice.assert_not_called()


def test_failed_cpu_construction_is_attempted_once():
    factory = Mock(side_effect=RuntimeError('CPU unavailable'))
    model = ResilientSpeechModel(app_stub(), Mock(transcribe=Mock(side_effect=RuntimeError(CUDA_ERROR))),
                                factory, device='cuda', notify=Mock())
    with pytest.raises(RuntimeError, match='CPU unavailable'):
        model.transcribe(object())
    with pytest.raises(RuntimeError, match='CPU speech model could not load'):
        model.transcribe(object())
    factory.assert_called_once()


def test_concurrent_decodes_share_one_cpu_reload():
    cpu = Mock(transcribe=Mock(side_effect=lambda *a, **k: (iter(['result']), None)))
    factory, notice = Mock(return_value=cpu), Mock()
    app = app_stub()
    model = ResilientSpeechModel(app, Mock(transcribe=Mock(side_effect=RuntimeError(CUDA_ERROR))),
                                factory, device='cuda', notify=notice)
    app.model = model
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: list(model.transcribe(object())[0]), range(2)))
    assert results == [['result'], ['result']]
    factory.assert_called_once()
    # 917b: concurrent final delivery still announces the switch only once.
    notice.assert_not_called()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: report_delivered(app), range(2)))
    notice.assert_called_once()


@pytest.mark.parametrize('startup_failure', [False, True])
def test_startup_installs_shared_wrapper_and_cpu_uses_int8(monkeypatch, startup_failure):
    from samsara import cuda_detect
    from samsara.ui import qt_runtime
    ready = []
    monkeypatch.setattr(qt_runtime, 'post_when_ready', lambda cb: ready.append(cb))
    monkeypatch.setattr(cuda_detect, 'prepare_cuda_dll_search', Mock())
    app, calls = app_stub(), []
    cpu = Mock(transcribe=Mock(side_effect=lambda *a, **k: (iter(['cpu']), None)))
    gpu = Mock(transcribe=Mock(side_effect=RuntimeError(CUDA_ERROR)))
    def factory(name, **kwargs):
        calls.append((name, kwargs))
        if kwargs['device'] == 'cuda':
            if startup_failure:
                raise RuntimeError(CUDA_ERROR)
            return gpu
        return cpu
    model, elapsed, device, compute = load_resilient_model(
        app, Mock(), factory, 'small', 'cuda', 'float16')
    notice = model._notify = Mock()
    app.model = model
    assert isinstance(model, ResilientSpeechModel) and elapsed >= 0
    assert (device, compute) == (('cpu', 'int8') if startup_failure else ('cuda', 'float16'))
    # 917b: startup recovery is announced when UI is ready, before dictation.
    assert len(ready) == int(startup_failure)
    for callback in ready:
        callback()
    assert notice.call_count == int(startup_failure)
    app.config['model_size'] = 'large-v3'  # Retry the loaded model, not a later saved choice.
    assert list(model.transcribe(object())[0]) == ['cpu']
    assert len(calls) == 2 and calls[-1][0] == 'small'
    assert calls[-1][1]['device'] == 'cpu' and calls[-1][1]['compute_type'] == 'int8'
    report_delivered(app)
    notice.assert_called_once()


def test_startup_non_cuda_error_is_not_hidden(monkeypatch):
    from samsara import cuda_detect
    monkeypatch.setattr(cuda_detect, 'prepare_cuda_dll_search', Mock())
    factory = Mock(side_effect=RuntimeError('model corrupt'))
    with pytest.raises(RuntimeError, match='model corrupt'):
        load_resilient_model(app_stub(), Mock(), factory, 'small', 'cuda', 'float16')
    factory.assert_called_once()


def test_dll_directory_cookie_retained_and_path_added_once(tmp_path, monkeypatch):
    from samsara import cuda_detect
    package = tmp_path / '_internal' / 'ctranslate2'
    package.mkdir(parents=True)
    (package / 'ctranslate2.dll').touch()
    register, handles = Mock(return_value=object()), {}
    fake_os = SimpleNamespace(name='nt', pathsep=os.pathsep, environ={'PATH': str(tmp_path / 'other')},
                              add_dll_directory=register)
    monkeypatch.setattr(cuda_detect, 'os', fake_os)
    monkeypatch.setattr(cuda_detect, '_package_search_paths', lambda: [package])
    monkeypatch.setattr(cuda_detect, '_dll_directory_handles', handles)
    cuda_detect.prepare_cuda_dll_search()
    cuda_detect.prepare_cuda_dll_search()
    register.assert_called_once_with(str(package.resolve()))
    assert list(handles.values()) == [register.return_value]
    assert fake_os.environ['PATH'].split(os.pathsep).count(str(package.resolve())) == 1


def test_frozen_layout_registered_before_native_model_import(tmp_path, monkeypatch):
    from samsara import cuda_detect
    monkeypatch.setattr(cuda_detect.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(cuda_detect.sys, 'executable', str(tmp_path / 'Samsara.exe'))
    monkeypatch.setattr(cuda_detect, 'find_spec', lambda _name: None)
    monkeypatch.delitem(cuda_detect.sys.modules, 'ctranslate2', raising=False)
    assert tmp_path / '_internal' / 'ctranslate2' in cuda_detect._package_search_paths()


def test_cuda_probe_requires_cudnn_loader_too(tmp_path, monkeypatch):
    from samsara import cuda_detect
    for name in cuda_detect._REQUIRED_CUDA_DLLS:
        if name != 'cudnn64_9.dll':
            (tmp_path / name).touch()
    monkeypatch.setattr(cuda_detect, '_probable_search_paths', lambda: [tmp_path])
    assert cuda_detect._missing_cuda_dlls() == ('cudnn64_9.dll',)


def cuda_manifest(tmp_path, *, tag='v0.40.0', app_version='0.40.0', **changes):
    spec = dict(next(s for s in gen.component_specs(tag) if s['id'] == 'cuda-pack'),
                available=True)
    spec.update(changes)
    (tmp_path / spec['filename']).write_bytes(b'synthetic pack')
    return gen.build_manifest(artifacts_dir=tmp_path, tag=tag, app_version=app_version,
                              repo='fixture/project', git_sha='a' * 40, build_date='2026-10-26', specs=[spec])


def test_release_does_not_offer_the_old_pack(tmp_path):
    specs = [s for s in gen.component_specs('v0.40.0') if s['id'] == 'cuda-pack']
    doc = gen.build_manifest(artifacts_dir=tmp_path, tag='v0.40.0', app_version='0.40.0',
                             repo='fixture/project', git_sha='a' * 40, build_date='2026-10-26', specs=specs)
    cuda = doc['components'][0]
    assert cuda['available'] is False and cuda['url'] is None and cuda['sha256'] is None
    assert cuda['filename'] == 'Samsara-CUDA-Pack-v0.40.0.zip'


@pytest.mark.parametrize('changes', [{'filename': 'Samsara-CUDA-Pack-v0.20.0.zip'},
                                    {'tag': 'v0.20.0'}, {'app_version': '0.39.0'}])
def test_manifest_rejects_cuda_app_version_mismatch(tmp_path, changes):
    with pytest.raises(gen.ManifestGenerationError, match='cuda-pack version does not match'):
        cuda_manifest(tmp_path, **changes)


def test_same_release_pack_identity_can_pass_after_verification(tmp_path):
    doc = cuda_manifest(tmp_path)
    assert doc['components'][0]['available'] is True
    assert '/v0.40.0/Samsara-CUDA-Pack-v0.40.0.zip' in doc['components'][0]['url']


def test_pinned_legacy_pack_cannot_bypass_identity_check(tmp_path):
    spec = dict(next(s for s in gen.component_specs('v0.40.0') if s['id'] == 'cuda-pack'),
                source='pinned', available=True)
    pin = dict(gen.PINNED_ASSETS['cuda-pack'], filename=spec['filename'])
    with pytest.raises(gen.ManifestGenerationError, match='cuda-pack version does not match'):
        gen.build_manifest(artifacts_dir=tmp_path, tag='v0.40.0', app_version='0.40.0',
                           repo='fixture/project', git_sha='a' * 40, build_date='2026-10-26',
                           specs=[spec], pins={'cuda-pack': pin})


@pytest.mark.parametrize('display', ['compact', 'full'])
@pytest.mark.parametrize('change', [{'device': 'cpu'}, {'compute_type': 'int8'}])
def test_settings_apply_saves_before_restart_notice_and_button(qapp, monkeypatch, display, change):
    from samsara import autostart
    from samsara.ui import speech_hardware_qt as ui
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _schema_defaults, _StubApp
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    app = _StubApp(deepcopy(_schema_defaults()))
    app.config.update(device='cuda', compute_type='float16', ui={'listening_display': display})
    app.tray_icon = SimpleNamespace(_restart_samsara=Mock())
    app.save_config = Mock()
    window = _SettingsWindow(app)
    window._save_fns = [lambda _acc: change]
    try:
        window._apply_and_close()
        app.save_config.assert_called_once()
        assert all(app.config[k] == value for k, value in change.items())
        dialogs = list(ui._dialogs)
        assert len(dialogs) == 1
        dialog = dialogs[0]
        assert dialog.text() == ui.RESTART_NOTICE
        assert not window.isVisible()
        button = next(b for b in dialog.buttons() if b.text() == 'Restart Samsara')
        app.tray_icon._restart_samsara.assert_not_called()
        button.click()
        app.tray_icon._restart_samsara.assert_called_once_with()
    finally:
        for dialog in list(ui._dialogs):
            dialog.close()
        window.deleteLater()
        qapp.processEvents()


def test_unchanged_hardware_does_not_show_restart_notice(monkeypatch):
    from samsara.ui import speech_hardware_qt as ui
    show = Mock()
    monkeypatch.setattr(ui, '_notice', show)
    app = app_stub()
    assert ui.request_settings_restart(app, ui.hardware_signature(app.config)) is None
    show.assert_not_called()


def test_shared_startup_seam_uses_resilient_model_without_importing_bootstrap():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    loader = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'load_model_async')
    calls = [n for n in ast.walk(loader) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == 'load_resilient_model']
    assert len(calls) == 1 and ast.unparse(calls[0].args[0]) == 'self'


@pytest.mark.parametrize('display', ['compact', 'full'])
def test_notices_render_in_real_themed_settings_host(qapp, monkeypatch, tmp_path, display):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from samsara import autostart, cuda_detect
    from samsara.ui import speech_hardware_qt as ui, theme
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _schema_defaults, _StubApp
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    monkeypatch.setattr(cuda_detect, 'request_cuda_status', lambda callback: callback(True, 'CUDA available (test fixture)'))
    app = _StubApp(deepcopy(_schema_defaults()))
    app.config.update(device='cuda', compute_type='float16', ui={'listening_display': display})
    app.tray_icon = SimpleNamespace(_restart_samsara=Mock())
    window = _SettingsWindow(app)
    theme.set_theme('dark', refresh=False)
    window.apply_theme()
    window.resize(1160, 900)
    window.show_tab('Dictation')
    for _ in range(5):
        qapp.processEvents()
    output = Path(os.environ.get('FU01_PROOF_DIR', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    try:
        for kind in ('fallback', 'restart'):
            if kind == 'fallback':
                dialog = ui.show_fallback_notice()
            else:
                previous = ui.hardware_signature(app.config)
                app.config['device'] = 'cpu'
                dialog = ui.request_settings_restart(app, previous)
            for _ in range(5):
                qapp.processEvents()
            assert dialog.isVisible() and dialog.width() <= window.width()
            assert all(button.height() >= theme.HIT_TARGET_MIN for button in dialog.buttons())
            assert all(button.width() < window.width() / 2 for button in dialog.buttons())
            # Actual rendered Qt widgets, composited into their themed host.
            # No desktop capture, live profile or real Samsara process.
            picture = QPixmap(window.width(), window.height() + 40)
            picture.fill(QColor(theme.BG0))
            painter = QPainter(picture)
            painter.setPen(QColor(theme.TEXT_PRIMARY))
            painter.drawText(16, 26, f'AFTER - {display.title()} - {kind.title()} notice (offscreen fixture)')
            painter.drawPixmap(0, 40, window.grab())
            position = QPoint((window.width() - dialog.width()) // 2,
                              40 + (window.height() - dialog.height()) // 2)
            painter.drawPixmap(position, dialog.grab())
            painter.end()
            assert picture.save(str(output / f'AFTER_{display}_{kind}_themed_host.png'))
            dialog.close()
            qapp.processEvents()
    finally:
        for dialog in list(ui._dialogs):
            dialog.close()
        window.deleteLater()
        qapp.processEvents()
