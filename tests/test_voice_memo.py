"""Tests for samsara/voice_memo.py: arm/disarm/expiry state machine and
the WAV + Obsidian note capture path. tmp_path stands in for the vault --
no model, no audio hardware; audio is a synthesized float32 sine buffer.
"""
import time
import types
import wave

import numpy as np
import pytest

from samsara import voice_memo


@pytest.fixture(autouse=True)
def _reset_state():
    voice_memo.disarm(types.SimpleNamespace())
    yield
    voice_memo.disarm(types.SimpleNamespace())


def _sine(duration_s=1.0, sample_rate=16000, freq=440.0):
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _make_app(tmp_path, **cfg_overrides):
    cfg = {
        "vault_dir": str(tmp_path),
        "note_relpath": "Voice Memos.md",
        "attachments_relpath": "Attachments/Memos",
        "arm_timeout_s": 120,
    }
    cfg.update(cfg_overrides)
    app = types.SimpleNamespace()
    app.config = {"voice_memo": cfg}
    app.play_sound = lambda *_a, **_kw: None
    return app


class TestArmDisarmExpiry:
    def test_round_trip(self):
        app = _make_app_bare()
        assert voice_memo.is_armed(app.config) is False

        voice_memo.arm(app)
        assert voice_memo.is_armed(app.config) is True

        was_armed = voice_memo.disarm(app)
        assert was_armed is True
        assert voice_memo.is_armed(app.config) is False

    def test_disarm_when_not_armed_returns_false(self):
        app = _make_app_bare()
        assert voice_memo.disarm(app) is False

    def test_expires_after_timeout(self, monkeypatch):
        app = _make_app_bare(arm_timeout_s=5)
        clock = {"t": 1000.0}
        monkeypatch.setattr(voice_memo.time, "monotonic", lambda: clock["t"])

        voice_memo.arm(app)
        assert voice_memo.is_armed(app.config) is True

        clock["t"] += 5.1
        assert voice_memo.is_armed(app.config) is False
        # Expiry clears the flag -- staying past the deadline doesn't re-arm.
        clock["t"] += 100
        assert voice_memo.is_armed(app.config) is False

    def test_not_yet_expired_stays_armed(self, monkeypatch):
        app = _make_app_bare(arm_timeout_s=120)
        clock = {"t": 1000.0}
        monkeypatch.setattr(voice_memo.time, "monotonic", lambda: clock["t"])

        voice_memo.arm(app)
        clock["t"] += 119
        assert voice_memo.is_armed(app.config) is True


def _make_app_bare(**cfg_overrides):
    cfg = {"arm_timeout_s": 120}
    cfg.update(cfg_overrides)
    app = types.SimpleNamespace()
    app.config = {"voice_memo": cfg}
    return app


class TestCapture:
    def test_writes_valid_wav_and_note(self, tmp_path):
        app = _make_app(tmp_path)
        audio = _sine()

        ok = voice_memo.capture(app, audio, 16000, "hello from the memo")

        assert ok is True
        attachments = tmp_path / "Attachments" / "Memos"
        wavs = list(attachments.glob("memo_*.wav"))
        assert len(wavs) == 1

        with wave.open(str(wavs[0]), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() == len(audio)

        note_path = tmp_path / "Voice Memos.md"
        assert note_path.exists()
        content = note_path.read_text(encoding="utf-8")
        assert content.startswith("# Voice Memos\n")
        assert "## " in content
        expected_embed = f"![[Attachments/Memos/{wavs[0].name}]]"
        assert expected_embed in content
        assert "hello from the memo" in content
        # Forward slashes only -- Obsidian link convention, even on Windows.
        assert "\\" not in expected_embed

    def test_disarms_after_capture(self, tmp_path):
        app = _make_app(tmp_path)
        voice_memo.arm(app)
        assert voice_memo.is_armed(app.config) is True

        voice_memo.capture(app, _sine(), 16000, "text")

        assert voice_memo.is_armed(app.config) is False

    def test_second_capture_appends_single_header(self, tmp_path, monkeypatch):
        # Filenames are second-precision (memo_%Y-%m-%d_%H%M%S.wav, per
        # spec) -- advance the clock a full second between captures so two
        # real, distinct hold-to-dictate recordings don't collide on the
        # same filename the way two same-second test calls would.
        app = _make_app(tmp_path)
        real_datetime = voice_memo.datetime
        base = real_datetime(2026, 7, 23, 22, 0, 0)

        class _FakeDatetime(real_datetime):
            _now = base

            @classmethod
            def now(cls, tz=None):
                return cls._now

        monkeypatch.setattr(voice_memo, "datetime", _FakeDatetime)

        voice_memo.capture(app, _sine(), 16000, "first memo")
        _FakeDatetime._now = base.replace(second=1)
        voice_memo.capture(app, _sine(), 16000, "second memo")

        note_path = tmp_path / "Voice Memos.md"
        content = note_path.read_text(encoding="utf-8")
        assert content.count("# Voice Memos") == 1
        assert content.count("## ") == 2
        assert "first memo" in content
        assert "second memo" in content

        wavs = sorted((tmp_path / "Attachments" / "Memos").glob("memo_*.wav"))
        assert len(wavs) == 2

    def test_unicode_transcript_round_trips_as_utf8(self, tmp_path):
        app = _make_app(tmp_path)
        text = "go to the store → buy milk — café too"

        ok = voice_memo.capture(app, _sine(), 16000, text)

        assert ok is True
        note_path = tmp_path / "Voice Memos.md"
        content = note_path.read_text(encoding="utf-8")
        assert text in content
        # Would raise UnicodeDecodeError if the file were actually cp1252.
        note_path.read_bytes().decode("utf-8")

    def test_unwritable_vault_dir_returns_false_without_raising(self, tmp_path):
        # A plain FILE occupying the vault_dir path -- mkdir(parents=True)
        # must fail (can't create a directory where a file already sits),
        # deterministically, without depending on any particular drive
        # letter existing/not existing on the test machine.
        blocker = tmp_path / "blocked_vault"
        blocker.write_text("not a directory")
        app = _make_app(tmp_path, vault_dir=str(blocker))

        ok = voice_memo.capture(app, _sine(), 16000, "unreachable")

        assert ok is False

    def test_capture_plays_error_sound_on_failure(self, tmp_path):
        blocker = tmp_path / "blocked_vault2"
        blocker.write_text("not a directory")
        app = _make_app(tmp_path, vault_dir=str(blocker))
        sounds_played = []
        app.play_sound = lambda kind, *a, **kw: sounds_played.append(kind)

        voice_memo.capture(app, _sine(), 16000, "text")

        assert "error" in sounds_played

    def test_capture_plays_success_sound(self, tmp_path):
        app = _make_app(tmp_path)
        sounds_played = []
        app.play_sound = lambda kind, *a, **kw: sounds_played.append(kind)

        voice_memo.capture(app, _sine(), 16000, "text")

        assert "success" in sounds_played
