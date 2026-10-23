"""Exercise the pinned client's real handshake and Samsara's refusal path offline."""
import importlib
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import aiohttp
import pytest

from samsara.tts import edge_tts_engine as edge
from samsara.tts.coordinator import AudioCoordinator
from tests.test_network_voice_fallback_542 import app_router, offline_asyncio, rig


_SYNTHESIZE = edge._synthesize_mp3
_CLIENT_TIME = 1760000000.0
_SERVER_TIME = _CLIENT_TIME + 3600
_AUDIO = b'x' * 4096


@pytest.fixture
def handshake(monkeypatch, offline_asyncio):
    client = importlib.import_module('edge_tts.communicate')
    constants = importlib.import_module('edge_tts.constants')
    requests, sent, refusals = [], [], []
    monkeypatch.setattr(client.DRM, 'clock_skew_seconds', 0.0)
    monkeypatch.setattr(client.DRM, 'get_unix_timestamp', staticmethod(
        lambda: _CLIENT_TIME + client.DRM.clock_skew_seconds))

    class Websocket:
        async def __aenter__(self):
            if refusals and refusals.pop(0):
                raise aiohttp.WSServerHandshakeError(
                    SimpleNamespace(real_url='<offline>'), (), status=403,
                    headers={'Date': datetime.fromtimestamp(
                        _SERVER_TIME, timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')})
            return self

        async def __aexit__(self, *args):
            pass

        async def send_str(self, data):
            sent.append(data)

        async def __aiter__(self):
            headers = b'X-RequestId:fixture\r\nPath:audio\r\nContent-Type:audio/mpeg\r\n'
            yield SimpleNamespace(type=aiohttp.WSMsgType.BINARY,
                                  data=len(headers).to_bytes(2, 'big') + headers + _AUDIO)
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                  data='Path:turn.end\r\n\r\n')

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, url, **kwargs):
            requests.append((urlsplit(url), kwargs))
            return Websocket()

    monkeypatch.setattr(client.aiohttp, 'ClientSession', Session)
    # Keep the real isolated synthesis loop, without registry/app workers.
    def spawn(name, worker, **kwargs):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread
    monkeypatch.setattr(edge.thread_registry, 'spawn', spawn)
    return SimpleNamespace(client=client, constants=constants, requests=requests,
                           sent=sent, refusals=refusals)


def test_pinned_client_builds_authenticated_handshake(handshake):
    import edge_tts

    requirements = (Path(__file__).resolve().parents[1] / 'requirements.txt').read_text()
    pin = re.search(r'^edge-tts==([^\s#]+)', requirements, re.MULTILINE).group(1)
    assert edge_tts.__version__ == pin
    assert _SYNTHESIZE('en-US-AriaNeural', 'Example', '+10%', '-20%') == _AUDIO
    assert len(handshake.requests) == 1
    url, kwargs = handshake.requests[0]
    params = parse_qs(url.query)
    constants = handshake.constants
    assert url.scheme == 'wss' and url.hostname == 'speech.platform.bing.com'
    assert url.path == '/consumer/speech/synthesize/readaloud/edge/v1'
    assert params['TrustedClientToken'] == [constants.TRUSTED_CLIENT_TOKEN]
    assert re.fullmatch(r'[a-fA-F0-9]{32}', params['ConnectionId'][0])
    # edge-tts 7.3.1 (2026-10-23 hotfix): Microsoft changed the token. It is now a
    # 16-hex nonce, a dash and a 64-hex keyed digest; the nonce is random, so the test
    # pins the shape the service accepts rather than recomputing the value.
    assert re.fullmatch(r'[0-9A-F]{16}-[0-9A-F]{64}', params['Sec-MS-GEC'][0])
    assert params['Sec-MS-GEC-Version'] == [f'2-{constants.CHROMIUM_FULL_VERSION}']
    headers = kwargs['headers']
    assert headers['User-Agent'] == constants.BASE_HEADERS['User-Agent']
    assert f'Edg/{constants.CHROMIUM_MAJOR_VERSION}.0.0.0' in headers['User-Agent']
    assert headers['Origin'] == constants.WSS_HEADERS['Origin']
    assert headers['Sec-WebSocket-Version'] == '13'
    assert re.fullmatch(r'muid=[A-F0-9]{32};', headers['Cookie'])
    assert kwargs['ssl'] is not False
    message = next(message for message in handshake.sent if 'Path:ssml' in message)
    ssml = ElementTree.fromstring(message.split('\r\n\r\n', 1)[1])
    assert ssml.find('.//{*}voice').get('name') == (
        'Microsoft Server Speech Text to Speech Voice (en-US, AriaNeural)')
    prosody = ssml.find('.//{*}prosody')
    assert prosody.get('rate') == '+10%' and prosody.get('volume') == '-20%'


def test_client_repairs_clock_skew_before_samsara_fallback(handshake):
    handshake.refusals[:] = [True, False]
    assert _SYNTHESIZE('en-US-AriaNeural', 'Example', '+0%', '+0%') == _AUDIO
    assert len(handshake.requests) == 2
    first, second = [parse_qs(url.query) for url, _ in handshake.requests]
    assert first['Sec-MS-GEC'] != second['Sec-MS-GEC']
    assert handshake.client.DRM.clock_skew_seconds == 3600


def test_persistent_403_uses_local_voice_and_existing_notice_once(rig, handshake, monkeypatch):
    # rig queues only outer jobs; the actual client still runs on its own loop.
    synth_spawn = edge.thread_registry.spawn
    def spawn(name, worker, **kwargs):
        if name == 'EdgeTTS-worker':
            rig.jobs.append(worker)
            return None
        return synth_spawn(name, worker, **kwargs)
    monkeypatch.setattr(edge.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(edge, '_synthesize_mp3', _SYNTHESIZE)
    app, router = app_router(rig)
    app.config['tts']['voice_id'] = 'en-US-AriaNeural'
    handshake.refusals[:] = [True] * 4  # library clock correction retries once
    for _ in range(3):
        handle = router.speak('Example', category='readback')
        rig.jobs.pop(0)()
        assert handle.voice_result == 'windows' and handle._state == 'done'
    assert len(handshake.requests) == 4  # two refusals, then Samsara cooldown
    assert handle.network_bypassed and not handle.network_failed
    assert len(rig.local.calls) == 3 and not rig.played
    assert all(args.get('voice_id') is None for _, args, _ in rig.local.calls)
    AudioCoordinator(app, rig.engine)._on_network_fallback()  # rebuilt router
    app._schedule_ui.assert_called_once_with(
        app.listening_indicator.show_outcome,
        'Network voice unavailable - using the Windows voice.', 'warning', 10000)
    assert len(app._outcome_ring) == 1
    assert app._outcome_ring[0].source == 'network_voice_fallback'
    assert app.config['tts']['engine'] == 'edge'
