"""Tests for the media_keys SMTC routing plugin.

All Win32 and WinRT calls are mocked — no actual media playback occurs.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Stub out win32 modules in case they're absent (unlikely on Windows, but safe).
for _mod in ('win32gui', 'win32process'):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Import real psutil for exception classes — psutil is always available on Windows.
import psutil as _psutil_real

# Provide a stub winsdk so the module loads even when the real one is absent
# in a stripped CI environment. Use try/import rather than a sys.modules check
# so we don't overwrite a real winsdk package that was just not imported yet.
try:
    import winsdk as _winsdk_check  # noqa: F401
except ImportError:
    _winsdk_stub = MagicMock()
    sys.modules['winsdk'] = _winsdk_stub
    sys.modules['winsdk.windows'] = _winsdk_stub
    sys.modules['winsdk.windows.media'] = _winsdk_stub
    sys.modules['winsdk.windows.media.control'] = _winsdk_stub

import importlib
import plugins.commands.media_keys as mk
importlib.reload(mk)


def _patch_session_manager(sm_mock):
    """media_keys imports SessionManager lazily, inside _get_session_for_process
    (`from winsdk.windows.media.control import ... as SessionManager`), so there
    is no module-level mk.SessionManager to patch — patching that attribute is a
    no-op against the live code path. Patch the attribute on the actual
    winsdk.windows.media.control module instead (importlib.import_module rather
    than a sys.modules lookup, since with the real winsdk package installed
    that submodule isn't necessarily registered in sys.modules yet just from
    `import winsdk` -- it's real winmd-backed dynamic loading, only guaranteed
    present after something actually imports it).
    """
    control_mod = importlib.import_module('winsdk.windows.media.control')
    return patch.object(control_mod, 'GlobalSystemMediaTransportControlsSessionManager',
                         sm_mock, create=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(source_id: str, *, play_ok=True, pause_ok=True,
                  toggle_ok=True, next_ok=True, prev_ok=True):
    """Build a mock SMTC session."""
    s = MagicMock()
    s.source_app_user_model_id = source_id
    s.try_play_async = AsyncMock(return_value=play_ok)
    s.try_pause_async = AsyncMock(return_value=pause_ok)
    s.try_toggle_play_pause_async = AsyncMock(return_value=toggle_ok)
    s.try_skip_next_async = AsyncMock(return_value=next_ok)
    s.try_skip_previous_async = AsyncMock(return_value=prev_ok)
    return s


def _make_manager(*sessions):
    m = MagicMock()
    m.get_sessions.return_value = list(sessions)
    return m


# ---------------------------------------------------------------------------
# _get_foreground_process_name
# ---------------------------------------------------------------------------

class TestGetForegroundProcessName:

    def test_returns_lowercase_exe_name(self):
        with patch.object(mk.win32gui, 'GetForegroundWindow', return_value=1234), \
             patch.object(mk.win32process, 'GetWindowThreadProcessId', return_value=(0, 999)), \
             patch.object(mk.psutil, 'Process') as mock_proc:
            mock_proc.return_value.name.return_value = 'Spotify.exe'
            assert mk._get_foreground_process_name() == 'spotify.exe'

    def test_returns_none_when_no_foreground(self):
        with patch.object(mk.win32gui, 'GetForegroundWindow', return_value=0):
            assert mk._get_foreground_process_name() is None

    def test_returns_none_on_access_denied(self):
        # Use the real psutil exception class so it's caught by the plugin's
        # except clause (which also references the real psutil via mk.psutil).
        with patch.object(mk.win32gui, 'GetForegroundWindow', return_value=1234), \
             patch.object(mk.win32process, 'GetWindowThreadProcessId', return_value=(0, 999)), \
             patch.object(mk.psutil, 'Process') as mock_proc:
            mock_proc.return_value.name.side_effect = _psutil_real.AccessDenied
            assert mk._get_foreground_process_name() is None

    def test_returns_none_on_no_such_process(self):
        with patch.object(mk.win32gui, 'GetForegroundWindow', return_value=1234), \
             patch.object(mk.win32process, 'GetWindowThreadProcessId', return_value=(0, 999)), \
             patch.object(mk.psutil, 'Process') as mock_proc:
            mock_proc.return_value.name.side_effect = _psutil_real.NoSuchProcess(0)
            assert mk._get_foreground_process_name() is None


# ---------------------------------------------------------------------------
# _get_session_for_process (async)
# ---------------------------------------------------------------------------

class TestGetSessionForProcess:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_matches_by_substring(self):
        spotify_session = _make_session('Spotify.Spotify')
        manager = _make_manager(spotify_session)
        sm = MagicMock()
        sm.request_async = AsyncMock(return_value=manager)
        with _patch_session_manager(sm):
            result = self._run(mk._get_session_for_process('spotify.exe'))
        assert result is spotify_session

    def test_matches_process_stem_without_exe(self):
        firefox_session = _make_session('Firefox-308046B0AF4A39CB')
        manager = _make_manager(firefox_session)
        sm = MagicMock()
        sm.request_async = AsyncMock(return_value=manager)
        with _patch_session_manager(sm):
            result = self._run(mk._get_session_for_process('firefox.exe'))
        assert result is firefox_session

    def test_returns_first_match_when_multiple(self):
        s1 = _make_session('Spotify.Spotify')
        s2 = _make_session('Spotify.SpotifyWeb')
        manager = _make_manager(s1, s2)
        sm = MagicMock()
        sm.request_async = AsyncMock(return_value=manager)
        with _patch_session_manager(sm):
            result = self._run(mk._get_session_for_process('spotify.exe'))
        assert result is s1

    def test_no_match_returns_none(self):
        vlc_session = _make_session('vlc')
        manager = _make_manager(vlc_session)
        sm = MagicMock()
        sm.request_async = AsyncMock(return_value=manager)
        with _patch_session_manager(sm):
            result = self._run(mk._get_session_for_process('spotify.exe'))
        assert result is None

    def test_empty_sessions_returns_none(self):
        manager = _make_manager()
        sm = MagicMock()
        sm.request_async = AsyncMock(return_value=manager)
        with _patch_session_manager(sm):
            result = self._run(mk._get_session_for_process('spotify.exe'))
        assert result is None

    def test_returns_none_when_winsdk_unavailable(self):
        # Simulate winsdk genuinely missing: swap in a bare module object with
        # no GlobalSystemMediaTransportControlsSessionManager attribute, so the
        # `from winsdk.windows.media.control import ... as SessionManager`
        # inside _get_session_for_process raises ImportError, same as it would
        # with winsdk not installed at all.
        import types
        bare_module = types.ModuleType('winsdk.windows.media.control')
        with patch.dict(sys.modules, {'winsdk.windows.media.control': bare_module}):
            result = self._run(mk._get_session_for_process('spotify.exe'))
        assert result is None

    def test_returns_none_when_manager_raises(self):
        sm = MagicMock()
        sm.request_async = AsyncMock(side_effect=RuntimeError("WinRT unavailable"))
        with _patch_session_manager(sm):
            result = self._run(mk._get_session_for_process('spotify.exe'))
        assert result is None

    def test_skips_session_with_bad_source_id(self):
        bad_session = MagicMock()
        type(bad_session).source_app_user_model_id = property(
            fget=lambda s: (_ for _ in ()).throw(RuntimeError("bad")))
        good_session = _make_session('Spotify.Spotify')
        manager = _make_manager(bad_session, good_session)
        sm = MagicMock()
        sm.request_async = AsyncMock(return_value=manager)
        with _patch_session_manager(sm):
            result = self._run(mk._get_session_for_process('spotify.exe'))
        assert result is good_session


# ---------------------------------------------------------------------------
# _send_action (async)
# ---------------------------------------------------------------------------

class TestSendAction:

    def _run(self, coro):
        return asyncio.run(coro)

    def _patch_fg(self, proc_name):
        return patch.object(mk, '_get_foreground_process_name', return_value=proc_name)

    def _patch_session(self, session):
        return patch.object(mk, '_get_session_for_process',
                            new=AsyncMock(return_value=session))

    def test_pause_calls_try_pause_async(self):
        session = _make_session('Spotify.Spotify')
        with self._patch_fg('spotify.exe'), self._patch_session(session):
            ok, msg = self._run(mk._send_action('pause'))
        session.try_pause_async.assert_awaited_once()
        assert ok is True
        assert 'pause' in msg

    def test_play_calls_try_play_async(self):
        session = _make_session('Spotify.Spotify')
        with self._patch_fg('spotify.exe'), self._patch_session(session):
            ok, msg = self._run(mk._send_action('play'))
        session.try_play_async.assert_awaited_once()
        assert ok is True

    def test_toggle_calls_try_toggle_async(self):
        session = _make_session('Spotify.Spotify')
        with self._patch_fg('spotify.exe'), self._patch_session(session):
            ok, msg = self._run(mk._send_action('toggle'))
        session.try_toggle_play_pause_async.assert_awaited_once()

    def test_next_calls_try_skip_next_async(self):
        session = _make_session('Spotify.Spotify')
        with self._patch_fg('spotify.exe'), self._patch_session(session):
            ok, msg = self._run(mk._send_action('next'))
        session.try_skip_next_async.assert_awaited_once()

    def test_previous_calls_try_skip_previous_async(self):
        session = _make_session('Spotify.Spotify')
        with self._patch_fg('spotify.exe'), self._patch_session(session):
            ok, msg = self._run(mk._send_action('previous'))
        session.try_skip_previous_async.assert_awaited_once()

    def test_no_foreground_returns_error(self):
        with self._patch_fg(None):
            ok, msg = self._run(mk._send_action('pause'))
        assert ok is False
        assert 'foreground' in msg

    def test_no_session_returns_error(self):
        with self._patch_fg('spotify.exe'), self._patch_session(None):
            ok, msg = self._run(mk._send_action('pause'))
        assert ok is False
        assert 'no media session' in msg

    def test_unknown_action_returns_error(self):
        session = _make_session('Spotify.Spotify')
        with self._patch_fg('spotify.exe'), self._patch_session(session):
            ok, msg = self._run(mk._send_action('rewind'))
        assert ok is False
        assert 'unknown' in msg

    def test_action_method_raising_returns_error(self):
        session = _make_session('Spotify.Spotify')
        session.try_pause_async = AsyncMock(side_effect=RuntimeError("WinRT error"))
        with self._patch_fg('spotify.exe'), self._patch_session(session):
            ok, msg = self._run(mk._send_action('pause'))
        assert ok is False
        assert 'failed' in msg


# ---------------------------------------------------------------------------
# _run_async
# ---------------------------------------------------------------------------

class TestRunAsync:

    def test_returns_coroutine_result(self):
        async def _coro():
            return 42
        assert mk._run_async(_coro()) == 42

    def test_returns_none_on_exception(self):
        async def _bad():
            raise ValueError("boom")
        assert mk._run_async(_bad()) is None

    def test_closes_loop_after_success(self):
        loops = []
        orig_new = asyncio.new_event_loop

        def _capturing_new():
            loop = orig_new()
            loops.append(loop)
            return loop

        async def _coro():
            return 1

        with patch('asyncio.new_event_loop', side_effect=_capturing_new):
            mk._run_async(_coro())
        assert loops and loops[-1].is_closed()

    def test_closes_loop_after_exception(self):
        loops = []
        orig_new = asyncio.new_event_loop

        def _capturing_new():
            loop = orig_new()
            loops.append(loop)
            return loop

        async def _bad():
            raise RuntimeError("fail")

        with patch('asyncio.new_event_loop', side_effect=_capturing_new):
            mk._run_async(_bad())
        assert loops and loops[-1].is_closed()


# ---------------------------------------------------------------------------
# Command handlers (smoke tests via mocked _run_async)
# ---------------------------------------------------------------------------

class TestCommandHandlers:

    def _call(self, handler, *, result=(True, "ok")):
        # Use new= with a plain MagicMock for _send_action so patch.object
        # does not auto-create an AsyncMock (which would leak an unawaited
        # coroutine into garbage collection and trigger a RuntimeWarning).
        with patch.object(mk, '_run_async', return_value=result), \
             patch.object(mk, '_send_action',
                          new=MagicMock(return_value=(True, "mocked"))):
            return handler(None, '')

    def test_pause_handler_returns_true(self):
        assert self._call(mk.handle_pause_this) is True

    def test_play_handler_returns_true(self):
        assert self._call(mk.handle_play_this) is True

    def test_toggle_handler_returns_true(self):
        assert self._call(mk.handle_toggle_this) is True

    def test_next_handler_returns_true(self):
        assert self._call(mk.handle_next_this) is True

    def test_prev_handler_returns_true(self):
        assert self._call(mk.handle_prev_this) is True

    def test_handler_returns_true_even_when_action_fails(self):
        # Commands always return True (handled=True) even on failure,
        # so they don't fall through to dictation output.
        assert self._call(mk.handle_pause_this, result=None) is True

    def test_pause_debounce_is_1_5(self):
        import importlib
        from samsara import plugin_commands as pc
        saved = dict(pc._REGISTRY)
        try:
            importlib.reload(mk)
            entry = pc._REGISTRY.get('pause this')
            assert entry is not None
            assert entry['debounce'] == 1.5
        finally:
            pc._REGISTRY.clear()
            pc._REGISTRY.update(saved)

    def test_next_debounce_is_0_8(self):
        import importlib
        from samsara import plugin_commands as pc
        saved = dict(pc._REGISTRY)
        try:
            importlib.reload(mk)
            entry = pc._REGISTRY.get('next track this')
            assert entry is not None
            assert entry['debounce'] == 0.8
        finally:
            pc._REGISTRY.clear()
            pc._REGISTRY.update(saved)

    def test_commands_registered_in_media_pack(self):
        import importlib
        from samsara import plugin_commands as pc
        saved = dict(pc._REGISTRY)
        try:
            importlib.reload(mk)
            for phrase in ('pause this', 'play this', 'toggle this',
                           'next track this', 'previous track this'):
                entry = pc._REGISTRY.get(phrase)
                assert entry is not None, f"'{phrase}' not registered"
                assert entry['pack'] == 'media', f"'{phrase}' wrong pack"
        finally:
            pc._REGISTRY.clear()
            pc._REGISTRY.update(saved)
