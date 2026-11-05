"""1146: synthetic content only; all clipboard APIs are in-memory fakes."""
import ast
import logging
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid
import wave

import pytest

from samsara import clipboard as cb, clipboard_recovery as recovery
from samsara import correction_queue as cq
from samsara.config_change_log import log_external_config_changes
from tests.test_clipboard_audit6 import fake, target


ROOT = Path(__file__).resolve().parents[1]
MARKERS = (
    'ExcludeClipboardContentFromMonitorProcessing',
    'CanIncludeInClipboardHistory',
    'CanUploadToCloudClipboard',
)


def markers(fake):
    return {fake.registered[name]: b'\0' * 4 for name in MARKERS}



def test_shared_guard_backend_publishes_protected_text(fake_clipboard):
    # 1146 r0440: exercise the 1148 session backend as well as the fault-injection fake.
    assert cb._user32.SetClipboardData.__self__ is fake_clipboard
    cb.copy_private_text('synthetic shared transport')
    assert fake_clipboard.text() == 'synthetic shared transport'
    assert fake_clipboard.formats == {
        **markers(fake_clipboard),
        cb.CF_UNICODETEXT: 'synthetic shared transport\0'.encode('utf-16-le'),
    }
    assert fake_clipboard.open_attempts == fake_clipboard.empties == 1
    assert not fake_clipboard.allocated and not fake_clipboard.is_open


def test_text_is_published_after_all_markers_in_one_open(fake, monkeypatch):
    writes = []
    original_set = cb._user32.SetClipboardData
    original_close = cb._user32.CloseClipboard
    closed = []

    def write(fmt, handle):
        assert fake.is_open
        if fmt == cb.CF_UNICODETEXT:
            assert fake.formats == markers(fake)
        writes.append(fmt)
        return original_set(fmt, handle)

    def close():
        closed.append(dict(fake.formats))
        return original_close()

    monkeypatch.setattr(cb._user32, 'SetClipboardData', write)
    monkeypatch.setattr(cb._user32, 'CloseClipboard', close)
    monkeypatch.setattr(cb.pyperclip, 'copy', Mock(side_effect=AssertionError('unsafe copy')))
    cb.copy_private_text('synthetic transport \U0001f642')
    assert writes == [fake.registered[name] for name in MARKERS] + [cb.CF_UNICODETEXT]
    assert fake.text() == 'synthetic transport \U0001f642'
    assert len(closed) == fake.open_attempts == fake.empties == 1
    assert closed[0] == {**markers(fake), cb.CF_UNICODETEXT: ('synthetic transport \U0001f642\0').encode('utf-16-le')}
    assert not fake.allocated and not fake.is_open


@pytest.mark.parametrize('name', MARKERS)
def test_registration_failure_keeps_original_and_never_uses_plain_copy(fake, monkeypatch, name):
    fake.user_copies_text('synthetic original')
    original = dict(fake.formats)
    register = cb._user32.RegisterClipboardFormatW
    monkeypatch.setattr(cb._user32, 'RegisterClipboardFormatW',
                        lambda item: 0 if item == name else register(item))
    monkeypatch.setattr(cb.pyperclip, 'copy', Mock(side_effect=AssertionError('unsafe copy')))
    with pytest.raises(RuntimeError, match='Protected clipboard write failed'):
        cb.copy_private_text('synthetic transport')
    assert fake.formats == original and not fake.empties and not fake.allocated


@pytest.mark.parametrize('allocation', range(1, 5))
def test_preparation_failure_frees_handles_without_replacing_original(fake, allocation):
    fake.user_copies_text('synthetic original')
    original = dict(fake.formats)
    fake.alloc_fail_on = allocation
    with pytest.raises(RuntimeError):
        cb.copy_private_text('synthetic transport')
    assert fake.formats == original and fake.empties == 0
    assert not fake.allocated and not fake.is_open


@pytest.mark.parametrize('name', MARKERS)
def test_marker_failure_never_publishes_text_and_paste_restores_original(fake, target, monkeypatch, name):
    fake.formats = {cb.CF_DIB: b'synthetic image'}
    original = dict(fake.formats)
    fmt = fake.RegisterClipboardFormatW(name)
    fake.set_fail_fmts.add(fmt)
    published = []
    write = cb._user32.SetClipboardData
    def record(fmt, handle):
        published.append(fmt)
        return write(fmt, handle)
    monkeypatch.setattr(cb._user32, 'SetClipboardData', record)
    assert not cb.paste_with_preservation('synthetic transport', paste_delay=0, restore_delay=0)
    assert cb.CF_UNICODETEXT not in published and not target.pasted
    assert fake.formats == original and not fake.allocated and not fake.is_open


@pytest.mark.parametrize('consent', [False, True])
def test_successful_paste_has_markers_and_preserves_original_formats(fake, target, consent):
    fake.formats = {cb.CF_DIB: b'synthetic pixels'}
    original = dict(fake.formats)
    receipt = recovery.Attempt(mode='paste' if consent else 'normal', sequence=fake.seq)
    seen = []
    target.after_paste.append(lambda: seen.append(dict(fake.formats)))
    token = recovery._attempt.set(receipt)
    try:
        assert cb.paste_with_preservation('synthetic transport', paste_delay=0, restore_delay=0)
    finally:
        recovery._attempt.reset(token)
    assert target.pasted == ['synthetic transport']
    assert seen[0] == {**markers(fake), cb.CF_UNICODETEXT: 'synthetic transport\0'.encode('utf-16-le')}
    assert fake.formats == (seen[0] if consent else original)
    assert receipt.replaced is consent


def test_restore_preserves_existing_privacy_formats(fake, target):
    cb.copy_private_text('synthetic earlier protected copy')
    original = dict(fake.formats)
    assert cb.paste_with_preservation('synthetic later transport', paste_delay=0, restore_delay=0)
    assert fake.formats == original


def test_config_log_never_formats_values_or_untrusted_names(caplog):
    class NoFormat:
        def __str__(self):
            raise AssertionError('config value formatted')
        __repr__ = __str__
    value = NoFormat()
    changed = {
        'cloud_llm': (value, {'api_key': value}),
        'tasks': (value, {'token': value}),
        'smart_actions': (value, {'auth_header': value}),
        'wake_word_config': (value, value),
        'api_key=synthetic-value': (value, value),
        'token_synthetic_embedded': (value, value),
        'untrusted\nforged log': (value, value),
        'synthetic_arbitrary_name': (value, value),
    }
    logger = logging.getLogger('privacy_test')
    with caplog.at_level(logging.INFO, logger='privacy_test'):
        log_external_config_changes(logger, changed)
    messages = [record.getMessage() for record in caplog.records if record.name == 'privacy_test']
    assert messages == [f'[CONFIG] External edit detected: {name} changed'
                        for name in ['cloud_llm', 'tasks', 'smart_actions', 'wake_word_config'] + ['[redacted key]'] * 4]


def test_reload_seam_calls_value_free_logger_without_bootstrapping_dictation():
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_apply_disk_config')
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'log_external_config_changes']
    assert len(calls) == 1
    assert [ast.unparse(arg) for arg in calls[0].args] == ['logger', 'changed']
    assert not any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and 'External edit detected' in node.value for node in ast.walk(method))


def source_function(path, name, namespace):
    """Run just a clipboard call site, with no UI/model/application bootstrap."""
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
    method.decorator_list = []
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<privacy transport seam>', 'exec'), namespace)
    return namespace[name]


@pytest.mark.parametrize('name', ['_paste_retry_then_clipboard', '_direct_paste_final'])
@pytest.mark.parametrize('write_fails', [False, True])
def test_streaming_manual_fallback_uses_protected_writer(fake, monkeypatch, name, write_fails):
    from samsara import tutorial_capture
    monkeypatch.setattr(tutorial_capture, 'final_current', lambda *_a, **_k: True)
    monkeypatch.setitem(sys.modules, 'samsara.gpu_fallback', SimpleNamespace(
        paste_final=Mock(side_effect=RuntimeError('synthetic delivery failure'))))
    if write_fails:
        fake.set_fail_fmts.add(fake.RegisterClipboardFormatW(MARKERS[0]))
    logger = Mock()
    fn = source_function('samsara/streaming.py', name, {
        'logger': logger, 'pyperclip': object(), '_release_held_modifiers': lambda: None,
        'PASTE_SETTLE_S': 0})
    fn(SimpleNamespace(_paste_lock=threading.Lock(), _last_pasted=False), 'synthetic transport')
    if write_fails:
        assert cb.CF_UNICODETEXT not in fake.formats
        logger.info.assert_not_called()
    else:
        assert fake.text() == 'synthetic transport'
        assert all(fake.formats[fmt] == raw for fmt, raw in markers(fake).items())
        logger.info.assert_called_once()


@pytest.mark.parametrize('write_fails', [False, True])
def test_tool_paste_uses_protected_writer_and_does_not_paste_on_failure(fake, target, monkeypatch, write_fails):
    from samsara import composition_policy
    monkeypatch.setattr(composition_policy, 'tool_effect_allowed', lambda *_a: True)
    if write_fails:
        fake.set_fail_fmts.add(fake.RegisterClipboardFormatW(MARKERS[1]))
    fn = source_function('samsara/smart_actions_tools.py', '_tool_paste_text', {'Dict': dict, 'Any': object})
    result = fn(object(), {'text': 'synthetic tool text'})
    assert result['success'] is not write_fails
    assert target.pasted == ([] if write_fails else ['synthetic tool text'])
    if not write_fails:
        assert all(fake.formats[fmt] == raw for fmt, raw in markers(fake).items())


@pytest.mark.parametrize('write_fails', [False, True])
def test_clipboard_only_dictation_is_also_protected(fake, write_fails):
    if write_fails:
        fake.set_fail_fmts.add(fake.RegisterClipboardFormatW(MARKERS[2]))
    app = SimpleNamespace(_session_mode_manager=SimpleNamespace(_sentence_clipboard=True),
                          add_to_history=Mock(), _log_history=Mock(), _notify_main_window=Mock())
    fn = source_function('samsara/delivery_text.py', '_deliver', {})
    if write_fails:
        with pytest.raises(RuntimeError, match='Protected clipboard write failed'):
            fn(SimpleNamespace(app=app), 'synthetic sentence', 'synthetic sentence', None)
        app.add_to_history.assert_not_called()
    else:
        result = fn(SimpleNamespace(app=app), 'synthetic sentence', 'synthetic sentence', None)
        assert result.text == 'synthetic sentence' and not result.confirmed
        assert fake.text() == result.text
        assert all(fake.formats[fmt] == raw for fmt, raw in markers(fake).items())


@pytest.mark.parametrize('write_fails', [False, True])
def test_emergency_fallback_uses_protected_copy_or_advances_to_visible_recovery(fake, monkeypatch, write_fails):
    if write_fails:
        fake.set_fail_fmts.add(fake.RegisterClipboardFormatW(MARKERS[0]))
    monkeypatch.setitem(sys.modules, 'plugins.commands.smart_actions', SimpleNamespace(
        EARCON_FALLBACK='synthetic fallback', EARCON_ERROR='synthetic error',
        _play_earcon=lambda *_: None, get_config=lambda *_: {},
        resolve_brain_dump_path=lambda *_: None, append_entry=lambda *_: False))
    dispatcher = SimpleNamespace(app=object(),
        _emergency_db_write=Mock(side_effect=RuntimeError('synthetic unavailable storage')),
        _show_recovery_modal=Mock())
    fn = source_function('samsara/smart_actions_tools.py', '_fallback_save', {'logger': Mock()})
    result = fn(dispatcher, 'synthetic recovery text', 'synthetic failure')
    assert result == ('shown_in_modal' if write_fails else 'saved_to_clipboard')
    if write_fails:
        dispatcher._show_recovery_modal.assert_called_once_with('synthetic recovery text')
        assert cb.CF_UNICODETEXT not in fake.formats
    else:
        dispatcher._show_recovery_modal.assert_not_called()
        assert all(fake.formats[fmt] == raw for fmt, raw in markers(fake).items())


@pytest.mark.parametrize('path,name,content', [
    ('plugins/commands/tab_finder.py', 'find_tab', 'synthetic tab'),
    ('plugins/commands/example_greet.py', 'greet', 'synthetic greeting'),
])
def test_command_temporary_pastes_use_protected_writer(fake, monkeypatch, path, name, content):
    pasted = []
    def hotkey(*keys):
        if keys == ('ctrl', 'v'):
            assert all(fake.formats[fmt] == raw for fmt, raw in markers(fake).items())
            pasted.append(fake.text())
    gui = SimpleNamespace(hotkey=hotkey, press=lambda *_: None)
    monkeypatch.setitem(sys.modules, 'pyautogui', gui)
    fn = source_function(path, name, {
        '_WINDOWS_AVAILABLE': True, '_focus_browser': lambda: True, '_clean': lambda x: x,
        'copy_private_text': cb.copy_private_text, 'pyautogui': gui,
        'time': SimpleNamespace(sleep=lambda *_: None), 'GREETING_TEXT': content})
    assert fn(None, content)
    assert pasted == [content]


def test_audio_uses_unique_opaque_names_and_keeps_private_queue_link(tmp_path):
    directory = Path(cq.audio_dir(str(tmp_path)))
    directory.mkdir()
    legacy = directory / 'legacy-synthetic-word.wav'
    legacy.write_bytes(b'synthetic legacy clip')
    paths = [Path(cq.save_audio([0.0, 0.25, -0.25], 'synthetic-misheard', home_dir=str(tmp_path))) for _ in range(2)]
    assert paths[0] != paths[1]
    for path in paths:
        assert uuid.UUID(hex=path.stem).version == 4
        assert len(path.stem) == 32 and path.suffix == '.wav'
        with wave.open(str(path), 'rb') as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (1, 2, 16000, 3)
    assert legacy.read_bytes() == b'synthetic legacy clip'
    queue = cq.CorrectionQueue(home_dir=str(tmp_path))
    result = queue.capture('starlite', 'starlight', audio=str(paths[0]))
    assert result['stored']
    stored = cq.CorrectionQueue(home_dir=str(tmp_path)).pending[0]
    assert (stored['wrong'], stored['right'], stored['audio']) == ('starlite', 'starlight', [str(paths[0])])


@pytest.mark.parametrize('phase', ['save', 'prune'])
def test_audio_errors_never_log_legacy_filenames_or_exception_messages(tmp_path, monkeypatch, caplog, phase):
    error = OSError(13, 'synthetic private error', 'legacy-synthetic-word.wav')
    if phase == 'save':
        monkeypatch.setattr(wave, 'open', Mock(side_effect=error))
    else:
        directory = Path(cq.audio_dir(str(tmp_path)))
        directory.mkdir()
        (directory / 'legacy-synthetic-word.wav').write_bytes(b'synthetic clip')
        monkeypatch.setattr(cq, 'MAX_AUDIO_CLIPS', 1)
        monkeypatch.setattr(cq.os, 'remove', Mock(side_effect=error))
    with caplog.at_level(logging.DEBUG):
        result = cq.save_audio([0.0, 0.25], 'synthetic-misheard', home_dir=str(tmp_path))
    assert (result is None) is (phase == 'save')
    assert 'PermissionError' in caplog.text
    assert 'synthetic private error' not in caplog.text
    assert 'legacy-synthetic-word' not in caplog.text
    assert 'synthetic-misheard' not in caplog.text
