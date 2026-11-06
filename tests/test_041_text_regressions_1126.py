"""1126: synthetic decoder output, actual final processing, no live app/audio."""
import logging
from types import SimpleNamespace as NS

import pytest

from samsara import cleanup, decode_recovery as decoder, delivery_verify, dictation_retry, streaming
from samsara.hold_final_policy import FinalAudit, current_audit
from samsara.hold_text import append_chunk, guard_segments
from samsara.session_text import seam_join
from tests.test_hold_decoder_1052d import configure, evidence, segment
from tests.test_never_withhold_dictation_1125 import install_retry, overflow, wire_delivery
from tests.test_scratch_after_unacknowledged_paste_783 import rig, deliver
from tests.test_silent_hold_387 import make_worker


# Exact owner-supplied regression fixtures are permitted here, never in logs.
CASES = [
    (". So immediately, I'm seeing the same thing...", "So immediately, I'm seeing the same thing..."),
    (", and the message just stays there . . . .", 'And the message just stays there.'),
    (", I mean until you like do some other action . . . .", 'I mean until you like do some other action.'),
    ('Nd I mentioned my concern about this issue...', 'Nd I mentioned my concern about this issue...'),
    ('And I mentioned my concern about this issue...', 'And I mentioned my concern about this issue...'),
    (", like the moment, this showed up ... I don't know know why all my objections ...",
     "Like the moment, this showed up... I don't know know why all my objections..."),
    (", you're connected and ufdc, you can check the logs now if you want.",
     "You're connected and ufdc, you can check the logs now if you want."),
]


def real_processing(app, monkeypatch):
    # The gate adapter supplies the unchanged class without executing bootstrap.
    from dictation import DictationApp
    app.process_transcription = DictationApp.process_transcription.__get__(app)
    app._apply_formatting_tokens = DictationApp._apply_formatting_tokens.__get__(app)
    monkeypatch.setattr(streaming, 'clean_text', cleanup.clean_text)


@pytest.mark.parametrize('case', range(len(CASES)))
@pytest.mark.parametrize('lane', ['ordinary', 'overflow', 'relaxed', 'voice_retry'])
def test_exact_fixtures_through_standalone_final(make_worker, monkeypatch, case, lane):
    raw, expected = CASES[case]
    worker, fake, audio = configure(make_worker, monkeypatch, 4)
    session = wire_delivery(worker, fake, monkeypatch)
    real_processing(session.app, monkeypatch)
    seen = []
    if lane == 'overflow':
        overflow(session.app._dictation_consumer, monkeypatch, 'end')
    if lane == 'voice_retry':
        decoder.retain(session.app, audio, {}, 'synthetic', [dictation_retry.MissedRange(0, 4)])
    def transcribe(pcm, **params):
        seen.append(params)
        text = 'Try again.' if lane == 'voice_retry' and len(seen) == 1 else raw
        values = [] if lane == 'relaxed' and len(seen) == 1 else [segment(text)]
        return iter(values), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    worker._final_pass()
    session._paste_with_retry.assert_called_once_with(expected)
    assert all(p['condition_on_previous_text'] is False for p in seen)
    # Fresh standalone delivery bypasses continuation joins. The pure join
    # also preserves the first letter when its previous sentence was terminal.
    assert seam_join(True, expected) == expected
    assert len(seen) == (2 if lane in ('relaxed', 'voice_retry') else 1)


def test_unguarded_path_reproduces_punctuation_but_not_letter_loss(make_worker, monkeypatch):
    worker, fake, _ = configure(make_worker, monkeypatch, 4)
    session = wire_delivery(worker, fake, monkeypatch)
    real_processing(session.app, monkeypatch)
    monkeypatch.setattr('samsara.hold_text.guard_segments', lambda app, segments: segments)
    for index in (0, 1, 4):
        raw, _ = CASES[index]
        session.app.model.transcribe.side_effect = lambda *a, **k: (iter([segment(raw)]), NS(language='en'))
        worker._final_pass()
        actual = session._paste_with_retry.call_args.args[0]
        if index == 0:
            assert actual.startswith('. ')
        elif index == 1:
            assert actual.endswith('....')  # clean_text compacts, never creates spaced dots.
        else:
            assert actual.startswith('And ')


@pytest.mark.parametrize('case', [0, 1, 2, 3, 4, 6])
def test_button_retry_also_guards_decoder_edges(make_worker, monkeypatch, case):
    raw, _ = CASES[case]
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    install_retry(session.app, monkeypatch)
    decoder.retain(session.app, audio, {}, 'synthetic')
    session.app.model.transcribe.side_effect = lambda *a, **k: (iter([segment(raw)]), NS(language='en'))
    assert dictation_retry.request(session.app)
    delivered = session.app._paste_preserving_clipboard.call_args.args[0]
    assert not delivered.startswith((', ', '. '))
    assert not delivered.endswith('. . . .')
    assert delivered.lstrip().split()[0] == raw.lstrip('., ').split()[0]


@pytest.mark.parametrize('text', ['comma hello period', 'Keep dot dot dot . . . .',
                                '.NET stays intact.', '.5 is a fraction.'])
def test_explicit_tokens_and_lexical_edges_survive(text):
    values = [segment(text)]
    assert guard_segments(NS(config={}), values) == values


def test_verbatim_and_separate_punctuation_segments():
    raw = [segment('. '), segment('And keep every letter'), segment(' . . . .')]
    assert guard_segments(NS(config={'cleanup_mode': 'verbatim'}), raw) is raw
    assert ''.join(s.text for s in guard_segments(NS(config={}), raw)).strip() == 'And keep every letter.'
    assert raw[0].text.strip() == '.'  # Original decoder objects are untouched.


def timed(text, spans, start=None, end=None):
    words = [NS(word=' ' + word, start=a, end=b) for word, a, b in spans]
    return segment(text, words[0].start if start is None else start,
                   words[-1].end if end is None else end, words=words)


def test_know_boundary_uses_words_despite_disjoint_segment_timestamps():
    left = timed("I don't know", [('I', 23, 23.2), ("don't", 23.3, 24), ('know', 24.3, 24.5)], end=24.45)
    right = timed('KNOW, why all my objections', [('KNOW,', 24.32, 24.52), ('why', 24.55, 24.7),
                  ('all', 24.8, 25), ('my', 25.1, 25.2), ('objections', 25.3, 25.7)], start=24.46)
    kept = [left]
    append_chunk(kept, [right], [left], (24.25, 24.75))
    assert ''.join(s.text for s in kept).strip() == "I don't know why all my objections"
    assert kept[-1].words == right.words[1:]
    assert (kept[-1].start, kept[-1].end) == (24.55, 25.7)
    assert right.text.startswith(' KNOW,') and len(right.words) == 5


@pytest.mark.parametrize('spans', [((23.9, 24.1), (24.8, 25.0)), ((24.3, 24.4), (24.5, 24.6))])
def test_real_very_very_repetition_has_distinct_source_times(spans):
    left = timed('very', [('very', *spans[0])])
    right = timed('very good', [('very', *spans[1]), ('good', 25.1, 25.4)])
    kept = [left]
    append_chunk(kept, [right], [left], (24.25, 24.75))
    assert ''.join(s.text for s in kept).strip() == 'very very good'


@pytest.mark.parametrize('shared,words', [(None, True), ((2, 3), True), ((24.25, 24.75), False)])
def test_no_shared_audio_or_no_word_timing_never_deletes(shared, words):
    left = timed('know', [('know', 24.3, 24.5)])
    right = timed('know why', [('know', 24.3, 24.5), ('why', 24.6, 24.9)])
    if not words:
        right.words = None
    kept = [left]
    append_chunk(kept, [right], [left], shared)
    assert kept == [left, right]


def test_match_can_span_segments_but_never_trims_within_one_chunk():
    left = [timed('I know', [('I', 24.27, 24.3), ('know', 24.32, 24.4)])]
    right = [timed('I', [('I', 24.27, 24.3)]),
             timed('know why', [('know', 24.32, 24.4), ('why', 24.5, 24.7)])]
    kept = list(left)
    append_chunk(kept, right, left, (24.25, 24.75))
    assert ''.join(s.text for s in kept).strip() == 'I know why'
    within = []
    append_chunk(within, right + right)
    assert within == right + right


def test_whole_duplicate_segment_removed_only_with_same_word_times():
    left = timed('know', [('know', 24.3, 24.5)])
    right = timed('know', [('know', 24.3, 24.5)])
    kept = [left]
    append_chunk(kept, [right], [left], (24.25, 24.75))
    assert kept == [left]


@pytest.mark.parametrize('path', ['stream', 'batch', 'retry'])
@pytest.mark.parametrize('repeated', [False, True])
def test_multichunk_decoder_offsets_words_and_requests_timing(make_worker, monkeypatch, path, repeated):
    worker, session, audio = configure(make_worker, monkeypatch, 26)
    calls = []
    def transcribe(pcm, **params):
        calls.append(params)
        # 1165: retry has 4.5s cores; keep these word times at its first seam.
        if path == 'retry' and len(calls) > 2:
            return iter([]), NS(language='en')
        shift = 20 if path == 'retry' else 0
        if len(calls) == 1:
            item = timed('very' if repeated else "I don't know", [('very' if repeated else 'I', 23-shift, 23.2-shift)] +
                         ([] if repeated else [("don't", 23.3-shift, 24-shift), ('know', 24.3-shift, 24.5-shift)]))
        else:
            item = timed('very good' if repeated else 'know why',
                         [('very' if repeated else 'know', .55 if repeated else .05, .75 if repeated else .25),
                          ('good' if repeated else 'why', .8, 1.0)])
        return iter([item]), NS(language='en')
    session.app.model.transcribe.side_effect = transcribe
    if path == 'stream':
        text = decoder.stream_final(worker, audio, {})[2]
    elif path == 'batch':
        text = decoder.decode_hold(session.app, audio, {}, 26).text
    else:
        slot = decoder.RetainedAudio(audio, {}, 'synthetic', (dictation_retry.MissedRange(0, 26),))
        decoded = dictation_retry.decode_slot(session.app, slot)
        text = ''.join(s.text for s in decoded.segments).strip()
    assert text == ('very very good' if repeated else "I don't know why")
    assert len(calls) == (6 if path == 'retry' else 2) and all(p['word_timestamps'] for p in calls)  # 1165


def test_conflict_resolution_preserves_1121_audit_and_1125_partial(make_worker, monkeypatch, caplog):
    worker, session, audio = configure(make_worker, monkeypatch, 4)
    audit = FinalAudit()
    audit.hold_seq_id = 'synthetic-1126'
    token = current_audit.set(audit)
    try:
        def broken():
            yield segment('Safe synthetic words.', 0, 1)
            raise RuntimeError('synthetic')
        session.app.model.transcribe.side_effect = lambda *a, **k: (broken(), NS(language='en'))
        with caplog.at_level(logging.WARNING):
            assert decoder.stream_final(worker, audio, {})[2] == 'Safe synthetic words.'
        assert session.app._hold_retry_audio.ranges == (dictation_retry.MissedRange(1, 4),)
        assert 'hold_seq_id=synthetic-1126' in caplog.text
        session.app.model.transcribe.side_effect = lambda *a, **k: (iter([]), NS(language='en'))
        decoder.stream_final(worker, audio, {})
        assert audit.empty_spoken_chunk
    finally:
        current_audit.reset(token)


def test_unconfirmed_can_mean_delivered_without_readback(rig, monkeypatch):
    editor, app, _ = rig
    monkeypatch.setattr(delivery_verify, 'snapshot', lambda: None)
    sent, confirmed = deliver(rig, 'Synthetic receipt.', confirmation=True)
    assert sent and not confirmed
    assert editor.document.decode('utf-16-le') == 'Synthetic receipt.'
    assert app._undo_unit is None
