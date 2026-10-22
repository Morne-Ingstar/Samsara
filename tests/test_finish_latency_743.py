"""743: internal endpointing and optional stitching, with no live app or audio I/O."""
from __future__ import annotations

import ast
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from samsara import session_modes as sm
from samsara.audio_engine import wake_consumer as wake
from samsara.streaming import DictatePreviewSession
from samsara.transcript_gates import _drop_trailing_garbage_segments, _trim_trailing_garbage_run
from tests.test_wake_consumer_hotkey_deafness import _make_wc, _loud_frame


SIGNALS = sm.UtteranceSignals(True, (1.0,), transcript_confident=True)


class Model:
    pass


class Decoder:
    model_rate = 16000

    def __init__(self):
        self.model = Model()
        self.calls = []

    def redecode(self, text, refs):
        self.calls.append(refs)


def make_manager(decoder, pasted):
    manager = sm.SessionModeManager(
        abort_phrases=[], foreground_exe_resolver=lambda: 'fixture.exe',
        foreground_hwnd_resolver=lambda: 743, inject_fn=lambda text, *args: pasted.append(text) or True,
        remove_chars_fn=lambda _: None, command_dispatch_fn=lambda _: None,
        agent_dispatch_fn=lambda *_: None, buffer_dictate_until_commit=True,
        commit_redecode_fn=decoder.redecode,
    )
    manager.reset(initial_mode=sm.SessionMode.DICTATE)
    return manager


def stage(manager, duration=3.0):
    audio = np.ones(int(duration * 16000), dtype=np.float32) * 0.05
    outcome = manager.dispatch_utterance('Synthetic fixture sentence.',
                                        sm.UtteranceSignals(True, (1.0,), True, audio))
    assert outcome.kind == 'dictate_staged'
    return audio


@pytest.fixture(autouse=True)
def commit_word():
    previous = sm.DICTATE_COMMIT_PHRASE
    sm.set_commit_phrase('finish')
    yield
    sm.set_commit_phrase(previous)


def test_sixty_second_staged_thought_skips_stitching_once(caplog):
    decoder, pasted = Decoder(), []
    manager = make_manager(decoder, pasted)
    for _ in range(20):
        stage(manager)
    expected = manager.dictate_pending_buffer
    with caplog.at_level(logging.INFO, logger='Samsara'):
        outcome = manager.dispatch_utterance('finish', SIGNALS)
    assert outcome.kind == 'dictate_committed'
    assert pasted == [expected]
    assert decoder.calls == []
    assert sum('same_model_no_short_fragments' in r.message for r in caplog.records) == 1


@pytest.mark.parametrize('duration', (0.4, 1.49, 1.5, 3.0))
def test_fragment_duration_boundary(duration):
    decoder, pasted = Decoder(), []
    manager = make_manager(decoder, pasted)
    stage(manager, duration)
    manager.dispatch_utterance('finish', SIGNALS)
    assert bool(decoder.calls) is (duration < 1.5)
    assert len(pasted) == 1


def test_model_change_between_chunks_keeps_redecode():
    decoder, pasted = Decoder(), []
    manager = make_manager(decoder, pasted)
    stage(manager)
    decoder.model = Model()
    stage(manager)
    manager.dispatch_utterance('finish', SIGNALS)
    assert len(decoder.calls) == 1


def test_missing_audio_provenance_keeps_redecode():
    decoder, pasted = Decoder(), []
    manager = make_manager(decoder, pasted)
    manager.dispatch_utterance('Synthetic fixture sentence.', SIGNALS)
    manager.dispatch_utterance('finish', SIGNALS)
    assert len(decoder.calls) == 1


def test_short_chunk_scratched_out_no_longer_requires_stitching():
    decoder, pasted = Decoder(), []
    manager = make_manager(decoder, pasted)
    stage(manager)
    stage(manager, 0.4)
    assert manager.dispatch_utterance('scratch that', SIGNALS).kind == 'scratch_success'
    manager.dispatch_utterance('finish', SIGNALS)
    assert decoder.calls == []


def preview_fixture(pace='standard', word='finish', *, buffered=True, mode=sm.SessionMode.DICTATE):
    consumer, _, app = _make_wc(command_mode_active=True, cm_mode='toggle')
    app.config['accessibility'] = {'speech_pace': pace}
    app._streaming_session = None
    app._session_mode_manager = SimpleNamespace(mode=mode, buffer_dictate_until_commit=buffered)
    app._vad_is_speech.return_value = False
    app.is_speaking = True
    app.silence_start = 100.0
    consumer._utterance_frames = [np.ones(16000, dtype=np.float32) * 0.05]
    consumer._last_voice_ns = 100_000_000_000
    preview = DictatePreviewSession.__new__(DictatePreviewSession)
    preview.app, preview._closed = app, False
    preview._commit_partial = (consumer._utterance_frames, consumer._last_voice_ns,
                               sm.is_dictate_commit(word.split()[-1]))
    app._dictate_preview = preview
    return consumer, app, preview


@pytest.mark.parametrize('pace', ('standard', 'relaxed', 'unhurried'))
def test_commit_partial_closes_at_400ms_and_regular_chunk_at_650ms(monkeypatch, pace):
    for word, threshold in [('finish', 0.4), ('finish the report', 0.65)]:
        consumer, app, _ = preview_fixture(pace, word)
        now, flushed = [100 + threshold - 0.01], []
        monkeypatch.setattr(wake, 'time', SimpleNamespace(time=lambda: now[0],
                            perf_counter_ns=lambda: int(now[0] * 1e9), monotonic=lambda: now[0]))
        consumer._flush = lambda *args, **kwargs: flushed.append(kwargs)
        frame = _loud_frame()
        frame.pcm[:] = 0
        consumer._process_frame(frame)
        assert flushed == []
        now[0] += 0.02
        consumer._process_frame(frame)
        assert len(flushed) == 1
        assert flushed[0]['last_voice_ns'] == 100_000_000_000
        assert not app.is_speaking


@pytest.mark.parametrize('changed', ('new_chunk', 'new_speech', 'closed', 'hold'))
def test_stale_partial_cannot_end_a_new_capture(changed):
    consumer, app, preview = preview_fixture()
    assert preview.commit_partial_ready(consumer)
    if changed == 'new_chunk':
        consumer._utterance_frames = list(consumer._utterance_frames)
    elif changed == 'new_speech':
        consumer._last_voice_ns += 1
    elif changed == 'closed':
        preview._closed = True
    else:
        app._hotkey_recording = True
    assert not preview.commit_partial_ready(consumer)


@pytest.mark.parametrize('buffered,mode', [(False, sm.SessionMode.DICTATE), (True, sm.SessionMode.COMMAND)])
def test_fast_flush_is_manual_dictate_only(monkeypatch, buffered, mode):
    consumer, _, _ = preview_fixture(buffered=buffered, mode=mode)
    monkeypatch.setattr(wake, 'time', SimpleNamespace(time=lambda: 100.5, perf_counter_ns=lambda: 1))
    flushed = []
    consumer._flush = lambda *args, **kwargs: flushed.append(kwargs)
    frame = _loud_frame()
    frame.pcm[:] = 0
    consumer._process_frame(frame)
    assert not flushed


@pytest.mark.parametrize('partial,word,expected', [
    ('Finish.', 'finish', True), ('Draft sentence. FINISH!', 'finish', True),
    ('finish the report', 'finish', False), ('unfinished', 'finish', False),
    ('finish', 'end', False), ('End.', 'end', True), ('Done!', 'done', True),
])
def test_partial_decode_uses_configured_terminal_token(monkeypatch, partial, word, expected):
    from samsara import speech_presence
    sm.set_commit_phrase(word)
    consumer, app, preview = preview_fixture(word=word)
    app._wake_consumer = consumer
    app.model_lock = threading.Lock()
    app.get_transcription_params = lambda **_: {}
    app.model = SimpleNamespace(transcribe=lambda *args, **kwargs: ([SimpleNamespace(text=partial)], None))
    app._filter_dictation_language = lambda text, info, **_: text
    preview._snapshot_audio = lambda: np.ones(16000, dtype=np.float32)
    monkeypatch.setattr(speech_presence, 'skip_decode', lambda *args, **kwargs: False)
    assert preview._transcribe_partial() == partial
    assert preview.commit_partial_ready(consumer) is expected


@pytest.mark.parametrize('text,expected', [
    ('Keep the sentence. Yeah Yeah Yeah', 'Keep the sentence.'),
    ('Keep the sentence. YEAH, yeah! “Yeah.”', 'Keep the sentence.'),
    ('Keep this. you you you you', 'Keep this.'), ('Yeah Yeah Yeah', ''),
    ('Keep this. Yeah yeah', 'Keep this. Yeah yeah'),
    ('Yeah yeah yeah, then keep this.', 'Yeah yeah yeah, then keep this.'),
    ('Keep this. ______', 'Keep this.'), ('Keep this. yeah yeah yeah ______', 'Keep this.'),
])
def test_trailing_repetition_preserves_prefix(text, expected):
    assert _trim_trailing_garbage_run(text) == expected


def test_garbage_segment_does_not_poison_good_prefix_telemetry():
    good = SimpleNamespace(text='Keep this.', compression_ratio=1.0)
    garbage = SimpleNamespace(text=' Yeah Yeah Yeah Yeah.', compression_ratio=10.0)
    assert _drop_trailing_garbage_segments([good, garbage]) == [good]
    assert _trim_trailing_garbage_run(good.text + garbage.text) == good.text


def commit_redecode_method():
    """Compile only the owned pure seam; never import or execute app bootstrap."""
    source = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == '_dictate_commit_redecode')
    namespace = {'np': np, 'logger': logging.getLogger('Samsara')}
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<commit-seam-743>', 'exec'), namespace)
    return namespace['_dictate_commit_redecode']


@pytest.mark.parametrize('configured', (None, 'bad-value', 20.0))
def test_default_or_invalid_cap_skips_sixty_seconds_before_model(configured, caplog):
    cm = {} if configured is None else {'dictate_commit_redecode_max_s': configured}
    app = SimpleNamespace(config={'command_mode': cm}, model_rate=16000)
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert commit_redecode_method()(app, 'Synthetic fixture.', [np.zeros(60 * 16000)]) is None
    assert 'configured cap 20.00s' in caplog.text


def test_explicit_cap_can_retain_optional_redecode(monkeypatch):
    from samsara import speech_presence
    calls = []
    monkeypatch.setattr(speech_presence, 'skip_decode',
                        lambda audio, app, **kw: calls.append(len(audio)) or True)
    app = SimpleNamespace(config={'command_mode': {'dictate_commit_redecode_max_s': 120}}, model_rate=16000)
    assert commit_redecode_method()(app, 'Synthetic fixture.', [np.zeros(60 * 16000)]) is None
    assert calls == [60 * 16000]


def test_replay_uses_correlated_marks_and_leaves_missing_values_unknown():
    import json
    from tools.finish_latency_743 import stage_rows, log_rows
    evidence = {'created_at': '2026-10-21T20:18:00', 'outcome': 'typed',
                'route': 'handsfree', 'mode': 'dictate',
                'timestamps_ns': {'last_voice': 1_000_000_000, 'speech_end': 1_400_000_000,
                                  'action_done': 1_700_000_000}}
    row = stage_rows([json.dumps(evidence)])[0]
    assert row == dict(commit=1, endpoint_s=0.4, post_close_s=0.3, total_s=0.7)
    assert stage_rows([json.dumps(evidence)], start='2026-10-21T20:25:00') == []
    row = log_rows(['2026-10-21 20:18:00,000 INFO dictate_committed'])[0]
    assert row['endpoint_s'] is None and row['total_s'] is None


def test_legacy_replay_correlates_gates_and_excludes_empty_commit():
    from tools.finish_latency_743 import log_rows, legacy_table
    lines = [
        '2026-10-21 20:17:52,905 [GATE] speech_presence path=handsfree duration_s=4.400',
        '2026-10-21 20:17:53,033 [GATE] pass: max contiguous speech 448ms at 1.41s (buffer 4.4s)',
        '2026-10-21 20:17:53,128 [GATE] speech_presence path=commit_redecode duration_s=69.000',
        '2026-10-21 20:17:55,005 [SESSION] outcome=dictate_committed',
        "2026-10-21 20:17:57,644 [SESSION] outcome=dictate_committed detail={'empty': True}",
    ]
    rows = log_rows(lines, start='2026-10-21T20:17:00', end='2026-10-21T20:25:59')
    assert len(rows) == 1
    row = rows[0]
    assert (row['endpoint_s'], row['post_close_s'], row['total_s']) == (None, None, None)
    assert row['gate_to_commit_s'] == pytest.approx(2.100)
    assert row['redecode_to_commit_s'] == pytest.approx(1.877)
    assert row['longest_run_tail_s'] == pytest.approx(2.542)
    assert 'Gate entry to commit' in legacy_table(rows)


def test_five_pcm_commits_per_pace_reach_paste_sink_within_budget(monkeypatch, tmp_path):
    from tools.finish_latency_743 import fixture_rows
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    rows = fixture_rows()
    assert len(rows) == 10
    for pace in ('standard', 'unhurried'):
        trials = [row for row in rows if row['pace'] == pace]
        assert [row['commit'] for row in trials] == [1, 2, 3, 4, 5]
        assert all(row['redecode_calls'] == 0 for row in trials)
        assert all(row['post_close_s'] < 1.0 and row['total_s'] <= 1.5 for row in trials)
