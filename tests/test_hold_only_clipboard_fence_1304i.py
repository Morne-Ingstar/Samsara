"""Compare the batch hold, prepared draft and direct streaming delivery seams."""

import ast
from io import BytesIO
from pathlib import Path
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

from PIL import Image
import pytest

from samsara import clipboard as cb, clipboard_recovery, dictation_history, paste, typed_delivery
from samsara import teach_patterns, tutorial_capture
from samsara.ava_edit import selection
from samsara.ava_edit.proposal import Proposal
from samsara.delivery_text import SessionTextDelivery
from samsara.history import HistoryManager
from tests.test_clipboard_audit6 import fake, target  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
WORDS = 'Synthetic screenshot delivery.'


def _source(path):
    return ast.parse((ROOT / path).read_text(encoding='utf-8'))


def _method(tree, name, namespace):
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), f'<{name}>', 'exec'), namespace)
    return namespace[name]


@pytest.fixture
def delivery(fake, target, tmp_path, monkeypatch):
    original = BytesIO()
    Image.new('RGB', (3, 2), (35, 117, 203)).save(original, format='BMP')
    dib = original.getvalue()[14:]
    fake.formats = {cb.CF_DIB: dib}
    tree = _source('dictation.py')
    typed = Mock(return_value=True)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda *_: NS(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste, '_editor_snapshot', lambda: None)
    monkeypatch.setattr(paste.delivery_verify, 'snapshot', lambda: None)
    monkeypatch.setattr(paste.delivery_verify, 'verified_snapshot', lambda *_: None)
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'editor.exe')
    monkeypatch.setattr(cb, '_paste_target', lambda: (42, 42, None))
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', lambda: cb.DeliveryOutcome.SENT)

    history = HistoryManager(tmp_path / 'history.db')
    app = NS(config={'auto_paste': True}, _foreground_wants_typed_injection=lambda: False,
             _flight_foreground_process_name=lambda: 'editor.exe', _record_undoable_paste=Mock(),
             adaptive_learner=NS(record_transcription=Mock()), _session_mode_manager=NS(),
             add_to_history=Mock(), _notify_main_window=Mock(), _show_outcome_chip=Mock(),
             _live_surface_hold_parking_enabled=lambda: False)

    def log_history(**fields):
        entry_id = history.add(**fields)
        dictation_history.remember_entry(app, entry_id, fields['display_text'])

    app._log_history = Mock(side_effect=log_history)
    wrapper = _method(tree, '_paste_preserving_clipboard', {
        'typed_delivery': typed_delivery, '_paste': paste, 'type_text_unicode': typed,
        'paste_with_preservation': cb.paste_with_preservation, '_get_foreground_hwnd': lambda: 42,
    })
    app._paste_preserving_clipboard = Mock(wraps=wrapper.__get__(app))
    yield NS(app=app, tree=tree, typed=typed, dib=dib, history=history,
             fake=fake, target=target)
    history.close()


def _batch_hold(delivery):
    """Run the real History and ordinary-hold dispatch statements without decoding audio."""
    worker = next(n for n in ast.walk(delivery.tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'transcribe')
    block = next(n.body for n in ast.walk(worker) if isinstance(n, ast.If)
                 and any(isinstance(stmt, ast.If) and ast.unparse(stmt.test) ==
                         "self.config['auto_paste'] or tutorial_waiting" for stmt in n.body))
    history_start = next(i for i, stmt in enumerate(block)
                         if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                         and ast.unparse(stmt.value.func) == 'self.add_to_history')
    send = next(stmt for stmt in block if isinstance(stmt, ast.If) and
                ast.unparse(stmt.test) == "self.config['auto_paste'] or tutorial_waiting")
    statements = [*block[history_start:history_start + 3], send]
    ns = dict(self=delivery.app, text=WORDS, raw=WORDS, audio_duration=0.5,
              tutorial_waiting=None, tutorial_capture=tutorial_capture, _paste=paste,
              hold_continuation=NS(remember=Mock()))
    exec(compile(ast.Module(body=statements, type_ignores=[]), '<batch hold delivery>', 'exec'), ns)
    return ns['delivered']


@pytest.mark.parametrize('backup_fails', [False, True], ids=['restores-screenshot', 'keeps-history'])
def test_ordinary_hold_screenshot_and_history_recovery(delivery, monkeypatch, backup_fails):
    d = delivery
    d.app._session_mode_manager = None
    if backup_fails:
        d.fake.no_data_formats.add(cb.CF_DIB)
    initial = d.fake.seq
    assert _batch_hold(d) is not backup_fails
    d.typed.assert_not_called()
    d.app.add_to_history.assert_called_once_with(WORDS, is_command=False)
    entry = dictation_history.entry_for(d.app)
    assert d.history.recent(1)[0]['display_text'] == WORDS
    if backup_fails:
        assert d.target.pasted == [] and d.fake.empties == 0 and d.fake.seq == initial
        assert d.fake.formats == {cb.CF_DIB: d.dib}
        label, kind = d.app._show_outcome_chip.call_args.args
        assert (label, kind) == (clipboard_recovery.HISTORY_NOTICE, 'error')
        link = dictation_history.link_for(d.app, label)
        open_entry = Mock()
        monkeypatch.setattr(dictation_history, 'open_entry', open_entry)
        link()
        open_entry.assert_called_once_with(d.app, entry)
    else:
        assert d.target.pasted == [WORDS]
        assert d.fake.formats[cb.CF_DIB] == d.dib
        assert d.fake.empties == 2


def _stream(app, phase):
    session = NS(app=app, _paste_lock=threading.Lock(), cancel_event=threading.Event(),
                 _last_pasted=False, _focus_unchanged=lambda: True)
    method = _method(_source('samsara/streaming.py'), '_direct_paste_' + phase, {
        'pyautogui': NS(hotkey=Mock()), 'pyperclip': None, 'logger': Mock(),
        'PASTE_SETTLE_S': 0, 'UNDO_SETTLE_S': 0, '_release_held_modifiers': Mock(),
    })
    method(session, WORDS)


@pytest.mark.parametrize('route', ['prepared-draft', 'streaming-partial', 'streaming-final'])
@pytest.mark.parametrize('backup_fails', [False, True], ids=['readable-image', 'backup-timeout'])
def test_draft_and_streaming_keep_master_dispatch(delivery, monkeypatch, route, backup_fails):
    d = delivery
    if backup_fails:
        monkeypatch.setattr(cb, 'save_clipboard', lambda: cb._failed_snapshot('clipboard backup timed out'))
    initial = d.fake.seq
    if route == 'prepared-draft':
        guard = Mock(return_value=True)
        assert SessionTextDelivery(d.app).inject_prepared(WORDS, guard)
        assert d.app._paste_preserving_clipboard.call_args.kwargs == {
            'before_paste': guard, 'return_delivery_confirmation': True,
        }
        d.app.add_to_history.assert_called_once_with(WORDS, is_command=False)
    else:
        phase = route.removeprefix('streaming-')
        _stream(d.app, phase)
        kwargs = d.app._paste_preserving_clipboard.call_args.kwargs
        if phase == 'partial':
            assert kwargs == {'feedback': 'silent'}
            d.app._show_outcome_chip.assert_not_called()
        else:
            assert list(kwargs) == ['before_paste'] and kwargs['before_paste']()
        d.app.add_to_history.assert_not_called()
    if backup_fails:
        d.typed.assert_called_once_with(WORDS)
        assert not d.target.pasted and d.fake.seq == initial and d.fake.empties == 0
    else:
        d.typed.assert_not_called()
        assert d.target.pasted == [WORDS] and d.fake.empties == 2
    assert d.fake.formats == {cb.CF_DIB: d.dib}


@pytest.mark.parametrize(
    "instruction, should_paste",
    [
        pytest.param(selection.DEFAULT_INSTRUCTION, True, id="clean-up"),
        pytest.param("Make it more formal", False, id="other-selection-edit"),
    ],
)
def test_only_clean_up_uses_native_fence_without_pyperclip(delivery, monkeypatch,
                                                           instruction, should_paste):
    original = "A synthetic   selection."
    proposal = Proposal.build(original, WORDS, instruction=instruction)
    staged = NS(selection_target=(42, 42, None), request=None,
                proposal=proposal, selection_mode="replace")
    monkeypatch.setattr(selection.session, "pending", lambda: staged)
    monkeypatch.setattr(selection, "_target_matches", lambda target: target == (42, 42, None))
    monkeypatch.setattr(teach_patterns, "grab_selection_block", lambda: original)
    monkeypatch.setattr(teach_patterns, "_wait_for_copy_modifiers", lambda: True)
    monkeypatch.setattr(teach_patterns, "_copy_modifiers_down", lambda: False)
    monkeypatch.setattr(cb, "HAS_PYPERCLIP", False)
    monkeypatch.setattr(cb, "pyperclip", None)
    preflight = Mock(wraps=cb.preflight_clipboard)
    monkeypatch.setattr(cb, "preflight_clipboard", preflight)

    assert selection._paste(staged) is should_paste
    assert delivery.target.pasted == ([WORDS] if should_paste else [])
    assert delivery.fake.formats[cb.CF_DIB] == delivery.dib
    if should_paste:
        preflight.assert_called_once_with()
        assert delivery.fake.empties == 2
    else:
        preflight.assert_not_called()
        assert delivery.fake.empties == 0
