"""1120: synthetic PCM only; no real device, model, app bootstrap or profile."""
import importlib
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import mic_check as check


def tone(amplitude, seconds=1):
    return (amplitude * np.sin(2 * np.pi * 220 * np.arange(round(seconds * check.SAMPLE_RATE)) / check.SAMPLE_RATE)).astype(np.float32)


@pytest.mark.parametrize('amplitude,noise,verdict', [
    (1.5, .0001, 'too_loud'), (.002, .0001, 'too_quiet'),
    (.08, .02, 'noisy'), (.12, .0002, 'good'),
])
def test_verdicts(amplitude, noise, verdict):
    audio = np.clip(tone(amplitude), -1, 1)
    result = check.evaluate(audio, tone(noise))
    assert result['verdict'] == verdict
    assert np.isfinite(result['snr_db'])
    assert result['message'] == check.VERDICTS[verdict]


def test_isolated_spike_is_not_clipping_but_repeated_full_frames_are():
    audio = tone(.1)
    audio[0] = 1
    result = check.evaluate(audio, tone(.0001))
    assert result['verdict'] == 'good'
    assert result['clipped_fraction'] == pytest.approx(1 / 16000)
    audio[1600] = audio[3200] = 1
    assert check.evaluate(audio, tone(.0001))['verdict'] == 'too_loud'


def test_exact_clip_fraction_boundary():
    audio = tone(.1)
    audio[:16] = .98
    assert check.evaluate(audio, tone(.0001))['verdict'] == 'good'
    audio[16] = .98
    assert check.evaluate(audio, tone(.0001))['verdict'] == 'too_loud'


@pytest.mark.parametrize('audio,quiet', [(np.zeros(16000), np.zeros(16000)), (tone(.02), tone(.02))])
def test_silence_or_constant_room_noise_is_inconclusive(audio, quiet):
    assert check.evaluate(audio, quiet)['verdict'] == 'no_speech'


def test_short_phrase_is_not_diluted_by_pauses():
    audio = np.concatenate((tone(.0002, 3), tone(.12, 1), tone(.0002, 4)))
    result = check.evaluate(audio, tone(.0002))
    assert result['verdict'] == 'good'
    assert result['speech_dbfs'] > -25


def test_fragmented_live_frames_and_integer_pcm():
    meter = check.LiveLevels()
    audio = (tone(.1, .3) * 32767).astype(np.int16)
    assert not meter.feed(audio[:333])
    readings = meter.feed(audio[333:])
    assert len(readings) == 3
    assert readings[0]['rms_dbfs'] == pytest.approx(-23.01, abs=.02)
    assert readings[0]['peak'] == pytest.approx(.1, abs=.001)
    assert readings[0]['clipped_fraction'] == 0


@pytest.mark.parametrize('audio', [np.array([np.nan]), np.array([np.inf]), np.ones((20, 2)), np.array(['bad'])])
def test_invalid_pcm_cannot_produce_a_result(audio):
    with pytest.raises(ValueError):
        check.measure(audio)


def test_persistence_allowlist_no_pcm_or_transcript(tmp_path):
    result = check.evaluate(tone(.12), tone(.0002))
    result.update(audio=tone(.1), heard='synthetic test text', secret='excluded')
    path = tmp_path / 'diagnostics' / 'mic_check.json'
    check.save_result(result, 'Synthetic microphone', path)
    saved = json.loads(path.read_text())
    assert saved['verdict'] == 'good'
    assert saved['microphone_name'] == 'Synthetic microphone'
    assert 'time' in saved
    assert not {'audio', 'heard', 'secret', 'message'} & saved.keys()
    assert list(path.parent.iterdir()) == [path]
    check.save_result(check.evaluate(tone(.002), tone(.0001)), 'Other', path)
    assert json.loads(path.read_text())['verdict'] == 'too_quiet'


def test_default_save_uses_isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    check.save_result(check.evaluate(tone(.1), tone(.0001)), 'Synthetic')
    assert (tmp_path / 'diagnostics' / 'mic_check.json').is_file()


class Source:
    def __init__(self, audio):
        self.blocks = iter([np.zeros(0, np.float32)] + list(np.array_split(audio, max(1, len(audio) // 1103))))
        self.close = Mock()

    def pull(self):
        return next(self.blocks, np.zeros(0, np.float32))


def recording():
    return np.concatenate((tone(.0001, check.QUIET_S), tone(.12, check.PHRASE_S)))


def test_capture_separates_quiet_and_phrase_across_source_blocks():
    events = []
    audio, quiet = check.capture(Source(recording()), threading.Event(), lambda *e: events.append(e))
    assert len(quiet) == round(check.QUIET_S * check.SAMPLE_RATE)
    assert len(audio) == round(check.PHRASE_S * check.SAMPLE_RATE)
    assert check.evaluate(audio, quiet)['verdict'] == 'good'
    assert [k for k, _ in events if k != 'level'] == ['quiet', 'noise', 'speak']
    assert sum(k == 'level' for k, _ in events) == 92


def test_capture_stall_and_cancel():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        check.capture(Source(recording()), cancel, lambda *e: None)
    now = iter([0, 3])
    with pytest.raises(RuntimeError, match='No microphone audio'):
        check.capture(Source(np.empty(0)), threading.Event(), lambda *e: None, clock=lambda: next(now))


@pytest.fixture
def app():
    return NS(config={'microphone': 3, 'microphone_name': 'Synthetic microphone'}, recording=False)


def test_window_runs_updates_meter_and_releases_source(qapp, monkeypatch, app, qt_widget_scope):
    from samsara.ui.mic_check_qt import MicCheckWindow
    source, save, heard = Source(recording()), Mock(), Mock(return_value=(check.SENTENCE, -.2))
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, function, **kw: function())
    window = MicCheckWindow(app, source_factory=lambda d: source, transcriber=heard, save=save)
    with qt_widget_scope(window):
        window.show()
        window.start_button.click()
        qapp.processEvents()
        assert not window.busy and app._mic_score_active is False
        assert 'Good' in window.verdict.text()
        assert check.SENTENCE in window.heard.text()
        assert window.noise.text() == 'Background noise: low'
        assert window.bar.level_dbfs == pytest.approx(-21.43, abs=.1)
        assert window.start_button.text() == 'Check again'
        assert window.start_button.width() < window.width() / 2
        source.close.assert_called_once()
        heard.assert_called_once()
        save.assert_called_once()


@pytest.mark.parametrize('fault', ['open', 'pull', 'close', 'decode', 'save', 'changed', 'cancel'])
def test_window_failures_cleanup(qapp, monkeypatch, app, qt_widget_scope, fault):
    from samsara.ui.mic_check_qt import MicCheckWindow
    source = Source(recording())
    if fault == 'pull':
        source.pull = Mock(side_effect=RuntimeError)
    if fault == 'close':
        source.close.side_effect = RuntimeError
    def factory(device):
        if fault == 'open':
            raise RuntimeError
        if fault == 'changed':
            app.config['microphone'] = 44
        return source
    def decode(audio):
        if fault == 'decode':
            raise RuntimeError
        if fault == 'cancel':
            window.cancel.set()
        return check.SENTENCE, -.2
    save = Mock(side_effect=OSError if fault == 'save' else None)
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, function, **kw: function())
    window = MicCheckWindow(app, source_factory=factory, transcriber=decode, save=save)
    with qt_widget_scope(window):
        window.start_check()
        assert not window.busy and not app._mic_score_active
        if fault in ('decode', 'save'):
            assert 'Good' in window.verdict.text()
            assert ('unavailable' in window.heard.text()) if fault == 'decode' else ('could not be saved' in window.instruction.text())
        else:
            assert 'No result' in window.verdict.text()
            save.assert_not_called()
        if fault != 'open':
            source.close.assert_called_once()


def test_busy_guard_and_escape_cancel(qapp, app, qt_widget_scope):
    from samsara.ui.mic_check_qt import MicCheckWindow
    source = Mock()
    window = MicCheckWindow(app, source_factory=source)
    with qt_widget_scope(window):
        app._mic_score_active = True
        window.start_check()
        source.assert_not_called()
        assert 'Finish' in window.instruction.text()
        window.reject()
        assert window.cancel.is_set()


def test_comparison_cannot_overlap_quick_check(qapp, app, qt_widget_scope):
    from samsara.ui.mic_score_qt import MicScoreWindow
    app._mic_score_active = True
    window = MicScoreWindow(app, preview=True)
    with qt_widget_scope(window):
        window.advance()
        assert window.run is None and not window.busy
        assert 'Finish the current microphone check' in window.status.text()


def test_changed_device_clears_previous_result(qapp, app, qt_widget_scope):
    from samsara.ui.mic_check_qt import MicCheckWindow
    window = MicCheckWindow(app)
    with qt_widget_scope(window):
        window.on_progress('result', (check.evaluate(tone(.1), tone(.0001)), check.SENTENCE, True))
        app.config.update(microphone=5, microphone_name='Another microphone')
        window.refresh_device()
        assert 'Microphone changed' in window.verdict.text()
        assert window.heard.text() == 'Samsara heard: not checked yet'


def test_voice_aliases_and_reviewed_examples_open_check(monkeypatch, app):
    from samsara import plugin_commands, command_catalog
    from plugins.commands import mic_score_voice
    from samsara.ui import mic_check_qt
    importlib.reload(mic_score_voice)
    opened = Mock()
    monkeypatch.setattr(mic_check_qt, 'open_mic_check', opened)
    for phrase in ('check my mic', 'test my microphone'):
        assert plugin_commands.find_command(phrase)
    assert mic_score_voice.check_my_mic(app)
    opened.assert_called_once_with(app)
    assert command_catalog.spoken_help('check my mic')[0] == ['check my mic', 'test my microphone']


def test_settings_button_opens_check(qapp, app, monkeypatch, qt_widget_scope):
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton
    from samsara.ui.settings import audio_page, calm_layout
    from samsara.ui import mic_check_qt
    container = QWidget()
    with qt_widget_scope(container):
        page = NS(container=container, base=QVBoxLayout(container))
        owner = NS(app=app, _widgets={}, make_page=lambda *a: page)
        monkeypatch.setattr(calm_layout, 'group_page', lambda *a: None)
        opened = Mock()
        monkeypatch.setattr(mic_check_qt, 'open_mic_check', opened)
        audio_page.build(owner)
        next(b for b in container.findChildren(QPushButton) if b.text() == 'Check mic').click()
        opened.assert_called_once_with(app)


def test_real_tray_tools_action_opens_check(qapp, monkeypatch):
    from tests.test_tray_qt import _make_app, _submenu
    from samsara.ui.tray_qt import SamsaraTrayQt
    from samsara.ui import mic_check_qt
    app = _make_app()
    opened = Mock()
    monkeypatch.setattr(mic_check_qt, 'open_mic_check', opened)
    tray = SamsaraTrayQt(app)
    try:
        tray._rebuild_menu()
        tools = _submenu(tray._menu, 'Tools')
        next(a for a in tools.actions() if a.text() == 'Check my mic').trigger()
        opened.assert_called_once_with(app)
    finally:
        tray._menu.close()


def test_open_reuses_window_and_posts_to_qt(qapp, app, monkeypatch, qt_widget_scope):
    from samsara.ui import mic_check_qt, qt_runtime
    monkeypatch.setattr(qt_runtime, 'post', lambda callback: callback())
    mic_check_qt.open_mic_check(app)
    with qt_widget_scope(app._mic_check_window):
        original = app._mic_check_window
        mic_check_qt.open_mic_check(app)
        assert app._mic_check_window is original
        assert not original.busy  # Opening alone never opens the microphone.
