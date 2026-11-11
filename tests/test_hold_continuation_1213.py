"""1213: post-decode continuation, without app bootstrap or document reads."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from samsara.cleanup import clean_text
from samsara import hold_continuation as hc


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(hc, '_target', lambda: (10, 20, 30))
    monkeypatch.setattr(hc.time, 'perf_counter', lambda: 105.)
    return NS(config={'mode': 'hold', 'cleanup_mode': 'clean', 'auto_capitalize': True},
              _hold_evidence_1121=NS(hold_seq_id='2-test'),
              _speech_presence_capture=NS(requested_at=104.))


def previous(app, text='We waited, ', *, target=(10, 20, 30), finished=100., sequence=1):
    app._hold_continuation_1213 = hc.Delivery(text, target, finished, sequence)


@pytest.mark.parametrize('ending', [', ', ' ', ' and ', ' but ', ' because ', '... ', '… ', '; ', ': '])
def test_unfinished_ending_continues(app, ending):
    previous(app, 'We waited' + ending)
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'because it mattered.'


@pytest.mark.parametrize('ending', ['. ', '! ', '? ', '." ', "?' ", '!\u201d '])
def test_sentence_ending_keeps_capital(app, ending):
    previous(app, 'We waited' + ending)
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'Because it mattered.'


@pytest.mark.parametrize('text', ["I agree.", "I'm ready.", "I've finished.", "I'll wait.",
                                  "I'd agree.", 'I’m ready.', 'NASA arrived.', 'SQL works.',
                                  'iPhone works.', 'eBay works.'])
def test_i_acronyms_and_mixed_case_survive(app, text):
    previous(app)
    assert hc.prepare(app, text, text) == text


@pytest.mark.parametrize('text', ['Alice met Alice today.', 'London is near London Bridge.',
                                  'Samsara helps with Samsara settings.'])
def test_evidenced_mid_sentence_names_survive(app, text):
    previous(app)
    assert hc.prepare(app, text, text) == text


def test_sentence_initial_repeat_is_not_proper_noun_evidence(app):
    previous(app)
    text = 'Because it mattered. Because we cared.'
    assert hc.prepare(app, text, text) == 'because it mattered. Because we cared.'


@pytest.mark.parametrize('text', ['"Because it mattered."', '(Because it mattered.)', '“Because it mattered.”'])
def test_opening_quotes_and_brackets(app, text):
    previous(app)
    assert hc.prepare(app, text, text) == text.replace('Because', 'because')


@pytest.mark.parametrize('started,expected', [(100., 'because'), (190., 'because'),
                                             (190.001, 'Because'), (99., 'Because')])
def test_expiry_is_from_delivery_to_next_hold_start(app, started, expected):
    previous(app)
    app._speech_presence_capture.requested_at = started
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.').startswith(expected)


@pytest.mark.parametrize('target', [(11, 20, 30), (10, 20, 31), (10, 21, 30), None])
def test_changed_or_unknown_target_resets(app, monkeypatch, target):
    previous(app)
    monkeypatch.setattr(hc, '_target', lambda: target)
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'Because it mattered.'


@pytest.mark.parametrize('mode', ['verbatim', 'raw', 'legacy'])
def test_non_clean_modes_are_byte_identical(app, mode):
    previous(app)
    app.config['cleanup_mode'] = mode
    assert hc.prepare(app, 'Because,  RAW\n', 'Because,  RAW\n') == 'Because,  RAW\n'


@pytest.mark.parametrize('attribute', ['_skip_cleanup', 'command_mode_recording', 'command_mode_active',
                                       'ava_mode_recording', '_memo_recording'])
def test_other_lanes_and_formatter_bypass(app, attribute):
    previous(app)
    setattr(app, attribute, True)
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'Because it mattered.'
    assert app._hold_continuation_pending_1213 is None


@pytest.mark.parametrize('kwargs', [{'verbatim': True}, {'tutorial': True}])
def test_verbatim_profile_and_tutorial_bypass(app, kwargs):
    previous(app)
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.', **kwargs) == 'Because it mattered.'


def test_auto_capitalize_setting_bypasses_policy(app):
    previous(app)
    app.config['auto_capitalize'] = False
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'Because it mattered.'


@pytest.mark.parametrize('mode', ['wake', 'continuous', 'command'])
def test_non_hold_mode_bypasses_policy(app, mode):
    previous(app)
    app.config['mode'] = mode
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'Because it mattered.'


@pytest.mark.parametrize('text', ['hello', 'hello,', 'hello...', '  lower\n', '', None])
def test_isolated_first_hold_matches_existing_pipeline(app, text):
    formatted = clean_text(text)
    assert hc.prepare(app, formatted, text) == formatted


@pytest.mark.parametrize('sent', [False, None, (True, False), 1])
def test_only_explicit_success_is_remembered(app, sent):
    hc.prepare(app, 'Waiting... ', 'Waiting...')
    hc.remember(app, 'Waiting... ', sent)
    assert app._hold_continuation_1213 is None


def test_changed_target_after_paste_does_not_remember(app, monkeypatch):
    hc.prepare(app, 'Waiting... ', 'Waiting...')
    monkeypatch.setattr(hc, '_target', lambda: (10, 20, 99))
    hc.remember(app, 'Waiting... ', True)
    assert app._hold_continuation_1213 is None


def test_real_two_hold_pipeline_from_ellipsis(app):
    first = hc.prepare(app, clean_text('waiting...') + ' ', 'waiting...')
    hc.remember(app, first, True)
    app._hold_evidence_1121.hold_seq_id = '3-test'
    app._speech_presence_capture.requested_at = 106.
    second = hc.prepare(app, clean_text('Because it mattered,') + ' ', 'Because it mattered,')
    assert first + second == 'Waiting... because it mattered, '


def test_no_reinterpretation_of_first_holds_comma_or_period(app):
    first = hc.prepare(app, clean_text('waiting,') + ' ', 'waiting,')
    hc.remember(app, first, True)
    app._hold_evidence_1121.hold_seq_id = '3-test'
    app._speech_presence_capture.requested_at = 106.
    assert first == 'Waiting. '
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'Because it mattered.'


def test_intervening_capture_invalidates_context(app):
    previous(app)
    app._hold_evidence_1121.hold_seq_id = '3-test'
    assert hc.prepare(app, 'Because it mattered.', 'Because it mattered.') == 'Because it mattered.'


def test_failed_or_abandoned_prepare_consumes_previous(app):
    previous(app)
    hc.prepare(app, 'Because it mattered.', 'Because it mattered.')
    assert hc.prepare(app, 'Next hold.', 'Next hold.') == 'Next hold.'


@pytest.mark.parametrize('before,text,expected', [
    ('We waited,', '. Because it mattered.', ' because it mattered.'),
    ('We waited, ', ', Because it mattered.', 'because it mattered.'),
    ('We waited.', ', Because it mattered.', ' Because it mattered.'),
    ('We waited. ', ', Because it mattered.', 'Because it mattered.'),
    ('We waited', 'Because it mattered.', ' because it mattered.'),
    ('We waited ', 'Because it mattered.', 'because it mattered.'),
])
def test_new_separator_and_spacing_only(app, before, text, expected):
    previous(app, before)
    assert hc.prepare(app, text, text) == expected


def test_reported_seams(app):
    # Owner-supplied cases remain in repository tests, never in report output.
    previous(app, 'chronic pain, ')
    assert hc.prepare(app, 'And my life.', 'And my life.') == 'and my life.'
    previous(app, 'my efforts became refocused onto Invention, ')
    assert hc.prepare(app, "There weren't sufficient tools.", "There weren't sufficient tools.") == "there weren't sufficient tools."
    previous(app, 'As a self-taught tech-wizard came to an abrupt halt, ')
    assert hc.prepare(app, 'Because the fingers I used...', 'Because the fingers I used...') == 'because the fingers I used...'


def test_hands_free_adapter_does_not_use_hold_context():
    from samsara.delivery_text import SessionTextDelivery
    app = NS(config={'cleanup_mode': 'clean', 'smart_corrections': {'modes': {'wake': False}}},
             _apply_verbatim_if_active=lambda text: None, process_transcription=lambda text: text,
             _apply_formatting_tokens=lambda text: text,
             _session_mode_manager=NS(dictate_pending_buffer='A longer accumulated sentence'),
             _hold_continuation_1213=hc.Delivery('Waiting, ', (10, 20, 30), 100., 1))
    assert SessionTextDelivery(app, single_line_probe=lambda: False).prepare('Because it mattered.') == 'Because it mattered.'


def test_authorized_two_post_decode_call_sites_only():
    source = Path(__file__).resolve().parents[1].joinpath('dictation.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_stop_recording_impl')
    calls = [n for n in ast.walk(method) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == 'hold_continuation']
    assert [c.func.attr for c in sorted(calls, key=lambda n: n.lineno)] == ['prepare', 'remember']
    prepare, remember = sorted(calls, key=lambda n: n.lineno)
    history = [n.lineno for n in ast.walk(method) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == 'add_to_history' and prepare.lineno < n.lineno < remember.lineno]
    assert history
