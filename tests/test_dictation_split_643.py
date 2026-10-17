"""643: frozen pre-extraction sound bytes and lookup/fallback contracts.

Golden hashes were captured from the original methods at c9249546, with
44.1 kHz tone generation and fake output devices. No dictation import or audio.
"""
import ast
import hashlib
import logging
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
TONE_HASHES = {'start': '4e07f97a9cd45b17af5d8cc58819536f4c82db4564a1ff6f352fa0d2ecd43c8c',
 'stop': '69fde487afc452fcff3170cf2feaf7f83587090a41be4aa6b16e70ccb30e2175',
 'success': 'aa6f07d930ee9a3aa4db742e0e29a93e5831e4bfe6524b14d5297561e9f15385',
 'error': '5a728acaa11423d5603c08724a48086f2f4da9f8a0d1fcac6fc196a35f08c390'}
CACHE_HASHES = {'error': ('float32', [18081, 1], 'dbfb99791da9910110f8f665e9eb1bf1c000900254f61244a2d7e45722b2b9b0'),
 'start': ('float32', [5292, 1], '1176e74c923ed743ad056c79794f3b1a2fbd2b47e2ae1a867a593fefa0a9b159'),
 'stop': ('float32', [4410, 1], 'efbe8af30130ddbe997325ba382547423ecd95bc46ce59269408939a43983089'),
 'success': ('float32', [14112, 1], 'd8e527a014f374c30039c171123c29a97655e469354cf26a83397addbeb4b801')}
LOOKUP_HASHES = {'44100:False:cute': {'capture_started': ('float32',
                                          [10, 1],
                                          '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                      'error': ('float32',
                                [6, 1],
                                '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                      'start': ('float32',
                                [10, 1],
                                '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                      'stereo': ('float32',
                                 [6, 1],
                                 '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                      'stop': ('float32',
                               [6, 1],
                               'e8be9f6b04949df7a22a53c5a714e406d213951baf2d0d84a8b7979370b8c614'),
                      'wide': ('float32',
                               [3, 1],
                               '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '44100:False:None': {'capture_started': ('float32',
                                          [10, 1],
                                          '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                      'error': ('float32',
                                [6, 1],
                                '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                      'start': ('float32',
                                [10, 1],
                                '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                      'stereo': ('float32',
                                 [6, 1],
                                 '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                      'stop': ('float32',
                               [6, 1],
                               'e8be9f6b04949df7a22a53c5a714e406d213951baf2d0d84a8b7979370b8c614'),
                      'wide': ('float32',
                               [3, 1],
                               '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '44100:False:absent': {'start': ('float32',
                                  [10, 1],
                                  '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c')},
 '44100:True:cute': {'capture_started': ('float32',
                                         [10, 1],
                                         '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                     'error': ('float32',
                               [6, 1],
                               '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                     'start': ('float32',
                               [10, 1],
                               '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                     'stereo': ('float32',
                                [6, 1],
                                '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                     'stop': ('float32',
                              [4, 1],
                              '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'success': ('float32',
                                 [4, 1],
                                 '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'wide': ('float32',
                              [3, 1],
                              '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '44100:True:None': {'capture_started': ('float32',
                                         [10, 1],
                                         '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                     'error': ('float32',
                               [6, 1],
                               '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                     'start': ('float32',
                               [10, 1],
                               '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                     'stereo': ('float32',
                                [6, 1],
                                '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                     'stop': ('float32',
                              [4, 1],
                              '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'success': ('float32',
                                 [4, 1],
                                 '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'wide': ('float32',
                              [3, 1],
                              '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '44100:True:absent': {'start': ('float32',
                                 [10, 1],
                                 '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                       'stop': ('float32',
                                [4, 1],
                                '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                       'success': ('float32',
                                   [4, 1],
                                   '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c')},
 '48000:False:cute': {'capture_started': ('float32',
                                          [10, 1],
                                          '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                      'error': ('float32',
                                [6, 1],
                                '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                      'start': ('float32',
                                [10, 1],
                                '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                      'stereo': ('float32',
                                 [6, 1],
                                 '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                      'stop': ('float32',
                               [6, 1],
                               'e8be9f6b04949df7a22a53c5a714e406d213951baf2d0d84a8b7979370b8c614'),
                      'wide': ('float32',
                               [3, 1],
                               '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '48000:False:None': {'capture_started': ('float32',
                                          [10, 1],
                                          '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                      'error': ('float32',
                                [6, 1],
                                '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                      'start': ('float32',
                                [10, 1],
                                '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                      'stereo': ('float32',
                                 [6, 1],
                                 '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                      'stop': ('float32',
                               [6, 1],
                               'e8be9f6b04949df7a22a53c5a714e406d213951baf2d0d84a8b7979370b8c614'),
                      'wide': ('float32',
                               [3, 1],
                               '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '48000:False:absent': {'start': ('float32',
                                  [10, 1],
                                  '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c')},
 '48000:True:cute': {'capture_started': ('float32',
                                         [10, 1],
                                         '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                     'error': ('float32',
                               [6, 1],
                               '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                     'start': ('float32',
                               [10, 1],
                               '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                     'stereo': ('float32',
                                [6, 1],
                                '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                     'stop': ('float32',
                              [4, 1],
                              '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'success': ('float32',
                                 [4, 1],
                                 '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'wide': ('float32',
                              [3, 1],
                              '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '48000:True:None': {'capture_started': ('float32',
                                         [10, 1],
                                         '8392cb2cd39b62e98351b47015fafa86a2f94dce9852cfa16be0c4ca6aea2956'),
                     'error': ('float32',
                               [6, 1],
                               '53ccc8b403164eecccc6a8b1ef476af4464f4d454690b7839c4cc7bbfdf5addb'),
                     'start': ('float32',
                               [10, 1],
                               '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                     'stereo': ('float32',
                                [6, 1],
                                '2de19ee7dd068cbe1eef4c532c032d973278e5508b7d2ae083d993bb2b8e9314'),
                     'stop': ('float32',
                              [4, 1],
                              '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'success': ('float32',
                                 [4, 1],
                                 '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                     'wide': ('float32',
                              [3, 1],
                              '67a7b97c31d4b3c9fca12d92821fa879daa958b6f77133dfb287d431f48986db')},
 '48000:True:absent': {'start': ('float32',
                                 [10, 1],
                                 '120c4492b110c0dd41fcf6206326610feae09e282448eed525fb83544142108c'),
                       'stop': ('float32',
                                [4, 1],
                                '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c'),
                       'success': ('float32',
                                   [4, 1],
                                   '2c4b8ebcc1c4fc457cabe12b8a0493ce685d988f41759d1d3ec9e41885f2557c')}}


def _methods(app_root):
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in {"_setup_sounds", "_load_sound_cache"}]
    assert len(methods) == 2
    sd = SimpleNamespace(query_devices=Mock(return_value={"default_samplerate": 44100}))
    registry = SimpleNamespace(spawn=Mock())
    ns = dict(np=np, wave=wave, Path=Path, threading=threading,
              logger=logging.getLogger("Samsara"), sd=sd, thread_registry=registry,
              __file__=str(app_root / "dictation.py"))
    exec(compile(ast.Module(body=methods, type_ignores=[]), "cue-facade-643", "exec"), ns)
    return ns, sd, registry


def _app(tmp_path):
    ns, sd, registry = _methods(tmp_path)
    app = SimpleNamespace(config={"sound_theme": "cute"}, output_device=23,
                          _start_sound_stream=Mock(), _watch_output_device=Mock())
    app._load_sound_cache = ns["_load_sound_cache"].__get__(app)
    return app, ns["_setup_sounds"].__get__(app), sd, registry


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _cache_hashes(app):
    return {name: (str(data.dtype), list(data.shape), _hash(data.tobytes()))
            for name, data in sorted(app._sound_cache.items())}


def _tone_hashes(app):
    return {name: _hash(path.read_bytes()) for name, path in app.sound_files.items()}


def _write_wav(path, values, width=2, channels=1, rate=22050):
    path.parent.mkdir(parents=True, exist_ok=True)
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(np.array(values, dtype=dtype).tobytes())


class _Decoded:
    sample_width = 2

    def set_channels(self, channels):
        assert channels == 1
        return self

    def set_frame_rate(self, rate):
        assert rate in (44100, 48000)
        return self

    def get_array_of_samples(self):
        return [0, -32768, 16384, 32767]


def _decoder(monkeypatch, available):
    loader = Mock(return_value=_Decoded())
    monkeypatch.setitem(sys.modules, "pydub", SimpleNamespace(AudioSegment=SimpleNamespace(from_file=loader))
                        if available else None)
    return loader


def _lookup_app(tmp_path, rate, config):
    app, _, _, _ = _app(tmp_path)
    app.sounds_dir = tmp_path / "sounds"
    app._sound_stream_sr = rate
    custom = app.sounds_dir / "custom"
    theme = app.sounds_dir / "themes" / "cute"
    app.sound_files = {"start": custom / "start.ogg", "stop": custom / "stop.wav",
                       "success": custom / "success.raw", "error": custom / "missing.wav",
                       "corrupt": custom / "corrupt.wav", "gone": custom / "gone.wav"}
    _write_wav(custom / "start.wav", [-32768, -123, 0, 321, 32767])
    for ext in (".mp3", ".ogg", ".flac", ".m4a"):
        (custom / ("start" + ext)).write_bytes(b"fake encoded asset")
        (custom / ("stop" + ext)).write_bytes(b"fake encoded asset")
    (custom / "success.raw").write_bytes(b"fake original-path asset")
    (custom / "corrupt.wav").write_bytes(b"invalid wave")
    _write_wav(theme / "start.wav", [5, 6])  # Must not replace legacy start.
    _write_wav(theme / "stop.wav", [100, 200, 300])  # Fallback without decoder.
    _write_wav(theme / "error.wav", [-500, 0, 500])  # Missing legacy fallback.
    _write_wav(theme / "capture_started.wav", [0, 128, 255, 64, 192], width=1)
    _write_wav(theme / "stereo.wav", [-32768, 32767, 1000, 3000, -1000, -3000], channels=2)
    _write_wav(theme / "wide.wav", [-2147483648, 0, 2147483647], width=4, rate=44100)
    (theme / "broken.wav").write_bytes(b"invalid wave")
    if config is None:
        del app.config
    else:
        app.config = {"sound_theme": config}
    return app


def test_generated_wav_and_cache_bytes_match_before_extraction(tmp_path, monkeypatch):
    _decoder(monkeypatch, False)
    app, setup, sd, registry = _app(tmp_path)
    setup()
    assert _tone_hashes(app) == TONE_HASHES
    assert _cache_hashes(app) == CACHE_HASHES
    assert app.sounds_dir == tmp_path / "sounds"
    assert app._sound_stream_sr == 44100
    sd.query_devices.assert_called_once_with(23, "output")
    app._start_sound_stream.assert_called_once_with()
    app._watch_output_device.assert_not_called()
    registry.spawn.assert_called_once_with('samsara-output-watcher', app._watch_output_device, daemon=True)
    assert app._playback_buffer.shape == (0, 1)
    assert app._playback_buffer.dtype == np.float32
    assert app._sound_stream is None
    assert not app._output_watcher_stop.is_set()
    # Existing user assets are never overwritten; only the missing one returns.
    app.sound_files["start"].write_bytes(b"custom asset stays intact")
    app.sound_files["error"].unlink()
    setup()
    assert app.sound_files["start"].read_bytes() == b"custom asset stays intact"
    assert _hash(app.sound_files["error"].read_bytes()) == TONE_HASHES["error"]


@pytest.mark.parametrize("rate", [44100, 48000])
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("config", ["cute", None, "absent"])
def test_cache_bytes_search_order_and_missing_asset_fallback(tmp_path, monkeypatch, rate, available, config):
    loader = _decoder(monkeypatch, available)
    app = _lookup_app(tmp_path, rate, config)
    app._sound_cache = {"stale": np.ones((1, 1))}
    app._load_sound_cache()
    key = f"{rate}:{available}:{config}"
    assert _cache_hashes(app) == LOOKUP_HASHES[key]
    assert "stale" not in app._sound_cache
    assert "gone" not in app._sound_cache
    assert "corrupt" not in app._sound_cache
    assert "broken" not in app._sound_cache
    assert ("error" in app._sound_cache) == (config != "absent")
    if available:
        assert [Path(call.args[0]).name for call in loader.call_args_list] == ["stop.mp3", "success.raw"]
    else:
        loader.assert_not_called()
        assert "success" not in app._sound_cache
    first = _cache_hashes(app)
    app._load_sound_cache()
    assert _cache_hashes(app) == first


@pytest.mark.parametrize("extension", [".wav", ".mp3", ".ogg", ".flac", ".m4a", ".raw"])
def test_extension_precedence_and_original_path_fallback(tmp_path, monkeypatch, extension):
    loader = _decoder(monkeypatch, True)
    app, _, _, _ = _app(tmp_path)
    app.sounds_dir = tmp_path
    app._sound_stream_sr = 44100
    app.sound_files = {"custom": tmp_path / "asset.raw"}
    order = [".wav", ".mp3", ".ogg", ".flac", ".m4a", ".raw"]
    for ext in order[order.index(extension):]:
        if ext == ".wav":
            _write_wav(tmp_path / ("asset" + ext), [0, 32767], rate=44100)
        else:
            (tmp_path / ("asset" + ext)).write_bytes(b"fake encoded asset")
    app._load_sound_cache()
    if extension == ".wav":
        loader.assert_not_called()
        expected = np.array([0, 32767], dtype=np.float32) / 32768
    else:
        loader.assert_called_once_with(str(tmp_path / ("asset" + extension)))
        expected = np.array([0, -32768, 16384, 32767], dtype=np.float32) / 32768
    np.testing.assert_array_equal(app._sound_cache["custom"], expected.reshape(-1, 1))
