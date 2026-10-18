"""No hardware: prove output identities are resolved at playback time."""

from types import SimpleNamespace
from pathlib import Path
import importlib.util

from samsara import output_devices


class _SoundDevice:
    def __init__(self, devices, default_output):
        self.devices = devices
        self.default = SimpleNamespace(device=(0, default_output))

    def query_devices(self, index=None, _kind=None):
        return self.devices if index is None else self.devices[index]

    @staticmethod
    def query_hostapis():
        return [{"name": "Windows WASAPI"}, {"name": "MME"}]


def _output(name, hostapi=0):
    return {"name": name, "hostapi": hostapi, "max_output_channels": 2, "default_samplerate": 48000}


def test_index_shift_resolves_the_saved_name_and_host_api_not_the_old_index():
    sd = _SoundDevice([_output("HDMI"), _output("Speakers")], default_output=0)
    assert output_devices.resolve_playback_device(sd, "Speakers", "Windows WASAPI") == 1
    # HDMI sleeps and PortAudio reindexes the same selected endpoint to zero.
    sd.devices = [_output("Speakers"), _output("HDMI")]
    assert output_devices.resolve_playback_device(sd, "Speakers", "Windows WASAPI") == 0


def test_follow_default_reads_a_changed_windows_default_on_each_play():
    sd = _SoundDevice([_output("HDMI"), _output("Speakers")], default_output=0)
    assert output_devices.resolve_playback_device(sd) == 0
    sd.default.device = (0, 1)
    assert output_devices.resolve_playback_device(sd) == 1


def test_sounds_page_keeps_default_label_short_and_refreshes_device_on_show(
    qapp, monkeypatch, qt_widget_scope,
):
    from PySide6.QtCore import QCoreApplication, QEvent
    from samsara.ui.settings_qt import _SettingsWindow

    settings_test_path = Path(__file__).with_name("test_settings.py")
    spec = importlib.util.spec_from_file_location(
        "_samsara_settings_test_support", settings_test_path
    )
    settings_test_support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(settings_test_support)

    sd = _SoundDevice([_output("Roku TV")], default_output=0)
    monkeypatch.setitem(__import__('sys').modules, "sounddevice", sd)
    app = settings_test_support._StubApp()
    app.get_available_output_devices = lambda: [
        {"id": 0, "name": "Roku TV", "hostapi": "Windows WASAPI"}
    ]
    with qt_widget_scope(_SettingsWindow(app)) as window:
        # 284 removed the duplicate Sounds selector/current-device label.
        # The one Audio selector stores an unresolved default identity.
        window.show_tab('Audio')
        combo = window._widgets['output_combo']
        assert combo.itemText(0) == 'System default'
        assert 'Roku TV' not in combo.itemText(0)
        assert window._widgets['output_label_map']['System default'] == (None, None, None)
        assert output_devices.output_identity(sd, output_devices.resolve_playback_device(sd))[0] == 'Roku TV'

        sd.devices = [_output('Roku TV'), _output('New Windows Default')]
        sd.default.device = (0, 1)
        QCoreApplication.sendEvent(window._pages['Audio'], QEvent(QEvent.Type.Show))
        assert combo.currentText() == 'System default'
        assert output_devices.output_identity(sd, output_devices.resolve_playback_device(sd))[0] == 'New Windows Default'


def test_missing_selected_output_falls_back_to_default_and_logs_once(caplog):
    output_devices._missing_playback_logged.clear()
    sd = _SoundDevice([_output("Speakers")], default_output=0)
    assert output_devices.resolve_playback_device(sd, "Unplugged HDMI", "Windows WASAPI") == 0
    assert output_devices.resolve_playback_device(sd, "Unplugged HDMI", "Windows WASAPI") == 0
    assert sum("following Windows default" in record.message for record in caplog.records) == 1


def test_earcons_and_both_tts_engines_share_the_live_resolver_seam():
    root = Path(__file__).resolve().parents[1]
    output_source = (root / "samsara" / "output_devices.py").read_text(encoding="utf-8")
    app_source = (root / "dictation.py").read_text(encoding="utf-8")
    edge_source = (root / "samsara" / "tts" / "edge_tts_engine.py").read_text(encoding="utf-8")
    winrt_source = (root / "samsara" / "tts" / "winrt_engine.py").read_text(encoding="utf-8")
    assert "def resolve_playback_device" in output_source
    assert "def _resolve_playback_output" in app_source
    runtime_source = (root / 'samsara/tts/runtime.py').read_text(encoding='utf-8')
    assert 'rebuild_tts_runtime' in app_source
    assert "factory = EdgeTTSEngine if provider == 'edge' else WinRTEngine" in runtime_source
    assert "factory(output_device_resolver=getattr(app, '_resolve_playback_output', None))" in runtime_source
    assert "device = self._resolve_playback_output()" in app_source
    assert "output_device_resolver" in edge_source and "self._playback_device()" in edge_source
    assert "output_device_resolver" in winrt_source and "_refresh_output_at_play_time" in winrt_source
