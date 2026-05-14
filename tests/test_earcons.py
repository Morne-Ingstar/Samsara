"""Tests for the Phase-2 earcon vocabulary.

Covers:
  - Every new earcon WAV exists in every theme directory
  - play_sound() accepts the new names without raising
  - play_sound() with an unknown name logs a warning instead of crashing
  - Smart Actions earcons_enabled=False suppresses Smart Actions earcons
    but not unrelated Samsara sounds
"""

import importlib.util
import io
import sys
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


REPO_ROOT = Path(__file__).parent.parent
THEMES_ROOT = REPO_ROOT / "sounds" / "themes"
THEMES = ['cute', 'chirpy', 'warm', 'zen', 'classic']
NEW_EARCONS = [
    'capture_started',
    'capture_saved',
    'agent_routing',
    'agent_response',
    'confirm_required',
    'action_complete',
    'thinking_pulse',
]
LEGACY_EARCONS = ['start', 'stop', 'success', 'error']


# Load smart_actions the same way the Phase-1 test does -- the plugin lives
# outside the samsara package.
_SA_PATH = REPO_ROOT / "plugins" / "commands" / "smart_actions.py"
_spec = importlib.util.spec_from_file_location("smart_actions", _SA_PATH)
smart_actions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smart_actions)


# ---------------------------------------------------------------------------
# WAV existence + format
# ---------------------------------------------------------------------------

class TestEarconFilesExist:

    @pytest.mark.parametrize("theme", THEMES)
    @pytest.mark.parametrize("earcon", NEW_EARCONS)
    def test_new_earcon_present_in_theme(self, theme, earcon):
        path = THEMES_ROOT / theme / f"{earcon}.wav"
        assert path.exists(), f"Missing earcon: {path}"
        assert path.stat().st_size > 0, f"Empty earcon: {path}"

    @pytest.mark.parametrize("theme", THEMES)
    @pytest.mark.parametrize("earcon", LEGACY_EARCONS)
    def test_legacy_earcons_still_present(self, theme, earcon):
        # Phase 2 must NOT touch the existing four earcons.
        path = THEMES_ROOT / theme / f"{earcon}.wav"
        assert path.exists(), f"Legacy earcon disappeared: {path}"

    @pytest.mark.parametrize("theme", THEMES)
    @pytest.mark.parametrize("earcon", NEW_EARCONS)
    def test_earcon_is_valid_wav(self, theme, earcon):
        path = THEMES_ROOT / theme / f"{earcon}.wav"
        with wave.open(str(path), 'rb') as wf:
            assert wf.getnchannels() == 1, "mono required"
            assert wf.getsampwidth() == 2, "16-bit PCM required"
            assert wf.getframerate() == 44100, "44.1 kHz required"
            assert wf.getnframes() > 0, "non-empty required"


# ---------------------------------------------------------------------------
# play_sound API
# ---------------------------------------------------------------------------

class _FakeApp:
    """Minimal app implementing the play_sound contract we want to test.

    Mirrors DictationApp's two-pass cache: legacy four names from
    sounds/<name>.wav (we don't load them here -- not under test), and
    extended names auto-discovered from the active theme directory.
    """

    def __init__(self, theme='cute'):
        import numpy as np
        self._np = np
        self.config = {'audio_feedback': True, 'sound_volume': 0.5,
                       'sound_theme': theme}
        self._sound_cache = {}
        self._warned_sound_misses = set()
        self.played = []  # for inspection by tests
        self._load_theme(theme)

    def _load_theme(self, theme):
        np = self._np
        theme_dir = THEMES_ROOT / theme
        for wav_path in theme_dir.glob('*.wav'):
            with wave.open(str(wav_path), 'rb') as wf:
                data = wf.readframes(wf.getnframes())
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            self._sound_cache[wav_path.stem] = arr.reshape(-1, 1)

    def play_sound(self, sound_type, use_winsound=False):
        # Mirror DictationApp.play_sound's behavior contract.
        if not self.config.get('audio_feedback', True):
            return
        cached = self._sound_cache.get(sound_type)
        if cached is None:
            if sound_type not in self._warned_sound_misses:
                self._warned_sound_misses.add(sound_type)
                print(f"[AUDIO] No cached sound for '{sound_type}'")
            return
        self.played.append(sound_type)


class TestPlaySoundAcceptsNewNames:

    @pytest.mark.parametrize("earcon", NEW_EARCONS)
    def test_accepts_new_earcon_without_raising(self, earcon):
        app = _FakeApp()
        # If decoding or cache lookup throws, this raises -- we want a silent
        # success.
        app.play_sound(earcon)
        assert earcon in app.played

    def test_legacy_names_still_work_via_theme_dir(self):
        # The fake loads everything in the theme dir, so legacy names should
        # also resolve. (In production legacy names come from sounds/<x>.wav
        # via pass 1; this just sanity-checks the cache contract.)
        app = _FakeApp()
        app.play_sound('start')
        app.play_sound('success')
        assert 'start' in app.played
        assert 'success' in app.played

    def test_unknown_name_logs_warning_does_not_raise(self):
        app = _FakeApp()
        buf = io.StringIO()
        with redirect_stdout(buf):
            app.play_sound('this_earcon_does_not_exist')
            # Second call must NOT log again -- we dedupe per name.
            app.play_sound('this_earcon_does_not_exist')
        out = buf.getvalue()
        assert 'this_earcon_does_not_exist' in out
        assert out.count('this_earcon_does_not_exist') == 1
        assert 'this_earcon_does_not_exist' in app._warned_sound_misses

    def test_audio_feedback_off_suppresses_everything(self):
        app = _FakeApp()
        app.config['audio_feedback'] = False
        app.play_sound('capture_saved')
        app.play_sound('success')
        assert app.played == []


# ---------------------------------------------------------------------------
# Theme switching
# ---------------------------------------------------------------------------

class TestThemeSwitching:

    def test_each_theme_loads_all_new_earcons(self):
        for theme in THEMES:
            app = _FakeApp(theme=theme)
            for earcon in NEW_EARCONS:
                assert earcon in app._sound_cache, (
                    f"theme {theme} cache missing {earcon}")

    def test_themes_produce_different_audio(self):
        # Different themes must actually produce different samples for the
        # same earcon -- otherwise the per-theme tuning is a no-op.
        cute = _FakeApp(theme='cute')
        warm = _FakeApp(theme='warm')
        a = cute._sound_cache['capture_saved']
        b = warm._sound_cache['capture_saved']
        # Could share length but not bit-identical samples.
        if a.shape == b.shape:
            assert not (a == b).all(), "themes produced identical audio"


# ---------------------------------------------------------------------------
# Smart Actions earcon suppression
# ---------------------------------------------------------------------------

class TestSmartActionsEarconGating:
    """When Smart Actions earcons_enabled is False, Smart Actions' OWN
    earcon calls must be suppressed -- but other Samsara sounds keep playing.
    """

    def _make_app(self, earcons_enabled, tmp_path):
        app = Mock()
        app.config = {
            'audio_feedback': True,
            'smart_actions': {
                'brain_dump_path': str(tmp_path / 'brain.md'),
                'earcons_enabled': earcons_enabled,
            },
        }
        app.play_sound = Mock()
        return app

    def test_smart_actions_earcons_off_suppresses_capture_sounds(self, tmp_path):
        app = self._make_app(earcons_enabled=False, tmp_path=tmp_path)
        ok = smart_actions.handle_note(app, "test thought")
        assert ok is True
        # File still written despite silence
        assert (tmp_path / 'brain.md').exists()
        # Plugin must NOT have called play_sound at all.
        app.play_sound.assert_not_called()

    def test_smart_actions_earcons_on_plays_dedicated_names(self, tmp_path):
        app = self._make_app(earcons_enabled=True, tmp_path=tmp_path)
        smart_actions.handle_note(app, "test thought")
        played = [c.args[0] for c in app.play_sound.call_args_list]
        # Phase 2 ships dedicated names, not aliases to start/success.
        assert 'capture_started' in played
        assert 'capture_saved' in played
        assert 'start' not in played
        assert 'success' not in played

    def test_unrelated_app_sounds_still_play_when_smart_actions_off(self, tmp_path):
        # The earcons_enabled flag is plugin-local. It must NOT touch the
        # global audio_feedback flag or anyone else's play_sound calls.
        app = self._make_app(earcons_enabled=False, tmp_path=tmp_path)
        smart_actions.handle_note(app, "silent capture")
        # Simulate another Samsara subsystem firing a sound directly:
        app.play_sound('success')
        played = [c.args[0] for c in app.play_sound.call_args_list]
        assert played == ['success']
