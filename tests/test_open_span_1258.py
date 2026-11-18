"""1258: deterministic quiet clock, synthetic PCM, real draft routing."""
from types import SimpleNamespace
from unittest.mock import Mock
import weakref

import numpy as np
import pytest

from samsara import draft_reread, open_span, session_modes
from samsara.ava_edit import session
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals
from samsara.session_text import span_needs_redecode, stitch_span_audio


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
ACTIVITY_PROBE = open_span._activity


class Model:
    pass


class App:
    model_rate = 16000
    device_type = 'cuda'
    is_speaking = False
    config = {}

    def __init__(self):
        self.model = Model()
        self.calls = []
        self.result = 'When the panel moves to the left, it stays there.'
        self.live_surface = SimpleNamespace(sync_draft=Mock())
        self._dictate_preview = SimpleNamespace(_closed=False, _resync_from_draft=Mock(), _render=Mock())

    def agent(self, *_):
        pass

    def decode(self, text, refs):
        self.calls.append((text, [ref.copy() for ref in refs]))
        return self.result() if callable(self.result) else self.result


class Clock:
    def __init__(self):
        self.now = 0.0
        self.jobs = []

    def timer(self, span, callback, delay):
        timer = SimpleNamespace(cancelled=False)
        timer.cancel = lambda: setattr(timer, 'cancelled', True)
        span.timer = timer
        self.jobs.append((self.now + delay, timer, callback))

    def advance(self, seconds):
        end = self.now + seconds
        while self.jobs:
            self.jobs.sort(key=lambda job: job[0])
            at, timer, callback = self.jobs[0]
            if at > end:
                break
            self.jobs.pop(0)
            self.now = at
            if not timer.cancelled:
                callback()
        self.now = end


@pytest.fixture
def rig(monkeypatch):
    session.clear()
    app, clock = App(), Clock()
    # Real retention/cap bookkeeping; VAD itself is covered by 1251.
    monkeypatch.setattr(draft_reread, 'speech_pcm', lambda audio, _: audio.copy())
    monkeypatch.setattr(open_span, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(open_span, '_timer', clock.timer)
    monkeypatch.setattr(open_span, '_activity', lambda _: (app.onset, app.is_speaking))
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'send')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'send'}))
    app.onset = 0
    prepare = Mock(side_effect=lambda text: text.replace('panel', 'diagram'))
    inject = Mock(side_effect=lambda text, *_: text)
    manager = SessionModeManager(abort_phrases=['cancel'], inject_fn=inject,
        inject_prepared_fn=inject, prepare_dictate_fn=prepare,
        foreground_exe_resolver=lambda: 'synthetic.exe', foreground_hwnd_resolver=lambda: 1258,
        remove_chars_fn=lambda *_: True, command_dispatch_fn=lambda *_: None,
        agent_dispatch_fn=app.agent, buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=lambda text: session_modes.HandsFreeCommandMatch(text, text)
            if text in {'new line', 'new paragraph', 'word left', 'select all'} else None,
        commit_redecode_fn=app.decode, live_surface_enabled=True)
    manager.reset(SessionMode.DICTATE)
    app._session_mode_manager = manager

    def stage(text='When the panel moves...', seconds=2):
        pcm = np.full(round(seconds * 16000), .2, np.float32)
        result = manager._stage_dictate_chunk(text, pcm)
        assert result.kind == 'dictate_staged'
        return manager.dictate_pending_buffer

    value = SimpleNamespace(app=app, clock=clock, manager=manager, stage=stage,
                            prepare=prepare, inject=inject)
    yield value
    session.clear()


def test_fragments_stage_immediately_then_cuda_settle_repairs_one_take(rig):
    rig.stage()
    displayed = rig.stage('to the left.')
    assert rig.app.calls == [] and 'diagram' in displayed
    rig.clock.advance(.349)
    assert rig.app.calls == []
    rig.clock.advance(.002)
    assert len(rig.app.calls) == 1
    assert rig.app.calls[0][0] == displayed
    assert len(rig.app.calls[0][1]) == 1  # The unmodified decoder receives one stitched clip.
    assert len(rig.app.calls[0][1][0]) == 4 * 16000 + 2560
    assert rig.manager.dictate_pending_buffer == 'When the diagram moves to the left, it stays there.'
    assert rig.prepare.call_count == 3
    rig.app.live_surface.sync_draft.assert_called_once_with(rig.manager)
    rig.app._dictate_preview._resync_from_draft.assert_called_once_with(rig.manager)
    assert rig.manager.draft_document.delivery_prepared
    assert rig.manager._reread_source[2] == rig.manager.dictate_pending_buffer


@pytest.mark.parametrize('device,delay', [('cpu', 1.8), ('cuda', .35), ('unknown', 1.8)])
def test_device_trigger_and_send_before_quiet(rig, device, delay):
    rig.app.device_type = device
    rig.stage(); rig.stage('to the left.')
    rig.clock.advance(delay - .001)
    assert not rig.app.calls
    rig.clock.advance(.002)
    assert len(rig.app.calls) == 1
    # A settled span is not decoded again at send, and the sealed prefix never is.
    outcome = rig.manager.dispatch_utterance('send', GOOD)
    assert outcome.kind == 'dictate_committed' and len(rig.app.calls) == 1
    assert rig.inject.call_args.args[0] == outcome.detail['text']


def test_cpu_send_redecodes_without_waiting_and_excludes_command_audio(rig):
    rig.app.device_type = 'cpu'
    rig.stage(); rig.stage('to the left.')
    refs = list(rig.manager._dictate_pending_audio)
    rig.clock.advance(.4)
    assert not rig.app.calls
    outcome = rig.manager.dispatch_utterance('send', GOOD)
    assert outcome.kind == 'dictate_committed' and len(rig.app.calls) == 1
    assert len(rig.app.calls[0][1][0]) == sum(len(ref) for ref in refs) + 2560
    rig.clock.advance(2)
    assert len(rig.app.calls) == 1


@pytest.mark.parametrize('phase', ['settle', 'decode'])
def test_onset_even_if_speech_already_ended_discards_work(rig, phase):
    rig.stage(); before = rig.stage('to the left.')
    if phase == 'settle':
        rig.app.onset += 1
    else:
        def resumed():
            rig.app.onset += 1
            return 'Stale synthetic result.'
        rig.app.result = resumed
    rig.clock.advance(.36)
    assert rig.manager.dictate_pending_buffer == before
    assert len(rig.app.calls) == (phase == 'decode')


def test_next_fragment_rearms_after_speech_cancels_settle(rig):
    rig.stage(); rig.stage('to the left.')
    rig.app.onset += 1
    rig.clock.advance(.4)
    rig.stage('and it stays there.')
    rig.clock.advance(.36)
    assert len(rig.app.calls) == 1


def test_actual_activity_probe_uses_capture_onset_counter(rig, monkeypatch):
    from samsara import execution_policy as ep
    monkeypatch.setattr(ep._speech, 'last_onset', 11)
    monkeypatch.setattr(ep._speech, 'last_dispatch_start', 10)
    assert ACTIVITY_PROBE(rig.manager) == (11, True)
    monkeypatch.setattr(ep._speech, 'last_dispatch_start', 12)
    assert ACTIVITY_PROBE(rig.manager) == (11, False)


@pytest.mark.parametrize('change', ['manual', 'append', 'reset', 'model', 'callback', 'parked'])
def test_result_never_overwrites_a_newer_edit_or_session(rig, change):
    rig.stage(); rig.stage('to the left.')
    expected = []
    def changed():
        if change == 'manual':
            rig.manager.draft_document.replace_text('Synthetic manual edit.', edited=True)
        elif change == 'append':
            rig.stage('Another synthetic fragment.')
        elif change == 'reset':
            rig.manager.reset(SessionMode.DICTATE)
        elif change == 'model':
            rig.app.model = Model()
        elif change == 'callback':
            rig.manager._commit_redecode_fn = lambda *_: 'Other decoder.'
        else:
            rig.manager.stage_parked_hold('Synthetic held take.')
        expected.append(rig.manager.dictate_pending_buffer)
        return 'Stale synthetic result.'
    rig.app.result = changed
    rig.clock.advance(.36)
    assert rig.manager.dictate_pending_buffer == expected[-1]


def test_scratch_restores_exact_prior_display_after_redecode_and_redo(rig):
    first = rig.stage()
    rig.stage('to the left.')
    rig.clock.advance(.36)
    repaired = rig.manager.dictate_pending_buffer
    assert rig.manager.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert rig.manager.dictate_pending_buffer == first
    assert len(rig.manager._dictate_pending_audio) == 1
    assert rig.manager.dispatch_utterance('redo that', GOOD).kind == 'scratch_success'
    assert rig.manager.dictate_pending_buffer == repaired
    assert len(rig.manager._dictate_pending_audio) == 2
    assert rig.manager.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert rig.manager.dictate_pending_buffer == first


def test_second_scratch_restores_display_before_first_fragment(rig):
    rig.stage(); rig.stage('to the left.')
    rig.clock.advance(.36)
    prior = rig.manager.dictate_pending_buffer
    rig.stage('and stays there.')
    rig.clock.advance(.36)
    assert rig.manager._do_scratch_that_n(1)['scratched'] == 1
    assert rig.manager.dictate_pending_buffer == prior
    assert rig.manager._do_scratch_that_n(1)['scratched'] == 1
    assert rig.manager.dictate_pending_buffer == 'When the diagram moves...'


@pytest.mark.parametrize('command', ['new line', 'new paragraph', 'word left', 'select all', 'uppercase that'])
def test_commands_seal_without_retaining_their_audio(rig, command):
    rig.stage(); rig.stage('to the left.')
    outcome = rig.manager.dispatch_utterance(command, GOOD)
    assert outcome.kind == 'hands_free_command_executed'
    after = rig.manager.dictate_pending_buffer
    assert not rig.manager._open_span.durations
    assert len(rig.manager._dictate_pending_audio) == 2
    rig.clock.advance(2)
    assert not rig.app.calls and rig.manager.dictate_pending_buffer == after


def test_cap_seals_and_only_the_new_span_is_replaced(rig):
    rig.stage('A sealed synthetic sentence.', seconds=10)
    prefix = rig.manager.dictate_pending_buffer
    assert not rig.manager._open_span.durations
    rig.stage('when the panel moves...', seconds=8)
    rig.stage('to the left.', seconds=8)
    rig.clock.advance(.36)
    assert rig.app.calls[0][0].startswith('when the diagram')
    assert rig.manager.dictate_pending_buffer.startswith(prefix + ' ')
    prior = rig.manager.dictate_pending_buffer
    rig.stage('Another synthetic start...', seconds=3)  # Would cross the cap: seals 16.16 s.
    assert rig.manager._open_span.prefix == prior
    assert rig.manager._open_span.start == 3
    rig.stage('with an ending.', seconds=2)
    rig.clock.advance(.36)
    assert rig.manager.dictate_pending_buffer.startswith(prior + ' ')
    assert rig.app.calls[-1][0] == 'Another synthetic start with an ending.'


def test_direct_cursor_command_also_invalidates_the_pending_span(rig):
    from samsara.draft_commands import dispatch_draft_command
    rig.stage(); before = rig.stage('to the left.')
    dispatch_draft_command(rig.manager, 'word left', 'word left')
    rig.clock.advance(.36)
    assert rig.manager.dictate_pending_buffer == before and not rig.app.calls


def test_clean_long_fragments_need_no_redecode(rig):
    rig.stage('First synthetic sentence.')
    before = rig.stage('Second synthetic sentence.')
    rig.clock.advance(2)
    assert not rig.app.calls and rig.manager.dictate_pending_buffer == before


def test_preparation_result_keeps_its_exact_whitespace(rig):
    rig.stage(); rig.stage('to the left.')
    rig.prepare.side_effect = lambda _: 'Prepared  synthetic sentence. '
    rig.clock.advance(.36)
    assert rig.manager.dictate_pending_buffer == 'Prepared  synthetic sentence. '


def test_sentence_end_after_ten_seconds_seals_after_repair(rig):
    rig.stage(seconds=6); rig.stage('to the left.', seconds=5)
    assert rig.manager._open_span.closing
    rig.clock.advance(.36)
    assert len(rig.app.calls) == 1 and not rig.manager._open_span.durations
    sealed = rig.manager.dictate_pending_buffer
    rig.manager.dispatch_utterance('send', GOOD)
    assert len(rig.app.calls) == 1 and rig.inject.call_args.args[0] == sealed


def test_ellipsis_after_ten_seconds_does_not_seal_as_sentence(rig):
    rig.stage('When the panel...', seconds=6)
    rig.stage('moves to the...', seconds=5)
    assert not rig.manager._open_span.closing


@pytest.mark.parametrize('bad', ['empty', 'exception', 'poor', 'prepare'])
def test_bad_redecode_retains_staged_span(rig, bad):
    rig.stage('First synthetic sentence.')
    before = rig.stage('Second synthetic sentence.', seconds=1)
    if bad == 'empty':
        rig.app.result = ''
    elif bad == 'exception':
        rig.app.result = lambda: (_ for _ in ()).throw(RuntimeError('synthetic'))
    elif bad == 'poor':
        rig.app.result = ' '.join(['synthetic'] * 110)
    else:
        rig.prepare.side_effect = lambda _: ''
    rig.clock.advance(.36)
    assert rig.manager.dictate_pending_buffer == before


@pytest.mark.parametrize('lost', ['audio', 'pool', 'eviction'])
def test_incomplete_or_evicted_span_never_becomes_partial_decode(rig, monkeypatch, lost):
    if lost == 'eviction':
        monkeypatch.setattr(draft_reread, 'MAX_AUDIO_BYTES', 300000)
    rig.stage(); before = rig.stage('to the left.')
    if lost == 'audio':
        rig.manager._dictate_pending_audio[0] = None
    elif lost == 'pool':
        rig.manager._reread_pcm.clear()
    rig.clock.advance(.36)
    assert rig.manager.dictate_pending_buffer == before and not rig.app.calls
    assert rig.manager.dispatch_utterance('send', GOOD).kind == 'dictate_committed'
    assert not rig.app.calls  # No fallback that can rewrite the sealed prefix.


def test_audio_is_not_held_again_by_span_state(rig):
    rig.stage(); rig.stage('to the left.')
    refs = [weakref.ref(ref) for ref in rig.manager._dictate_pending_audio]
    rig.manager.reset(SessionMode.DICTATE)
    rig.clock.advance(2)
    assert all(ref() is None for ref in refs)


@pytest.mark.parametrize('durations,seam,expected', [
    ([1], True, False), ([2, 2], False, False), ([2, 2], True, True),
    ([1.49, 2], False, True), ([1.5, 2], False, False),
])
def test_span_eligibility(durations, seam, expected):
    assert span_needs_redecode(durations, seam) is expected


def test_stitch_has_bounded_quiet_seam_and_ten_ms_edge_fades():
    result = stitch_span_audio([np.ones(3200, np.float32), np.full(3200, .5, np.float32)])
    assert result.dtype == np.float32 and len(result) == 8960
    np.testing.assert_array_equal(result[3200:5760], np.zeros(2560))
    assert result[3039] == 1 and result[3199] == 0
    assert result[5760] == 0 and result[5919] == .5 and result[-1] == .5


@pytest.mark.parametrize('clips', [[], [np.zeros(0)], [np.zeros((2, 3))],
                                   [np.array([np.nan])], [np.zeros(320001)]])
def test_stitch_refuses_incomplete_or_oversized_evidence(clips):
    assert stitch_span_audio(clips) is None
