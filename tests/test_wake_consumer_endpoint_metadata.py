"""Regression tests for wake and hands-free endpoint metadata transport."""
from __future__ import annotations

import collections
import sys
import threading
import types

from samsara.audio_engine import wake_consumer as wake_module
from samsara.stage_timing import StageTiming


def test_dictation_module_is_not_imported():
    assert "dictation" not in sys.modules


def _make_timing_sink(tmp_path):
    rows = []

    def record_endpoint(last_voice_ns, speech_end_ns):
        timing = StageTiming.create("wake", "dictate", enabled=True, home_dir=tmp_path)
        timing.mark_at("last_voice", last_voice_ns, last_voice_source="vad")
        timing.mark_at("speech_end", speech_end_ns)
        rows.append(timing)
        timing.complete()

    return rows, record_endpoint


def test_fake_consumer_flush_carries_endpoint_times_into_wake_timing(tmp_path):
    consumer = object.__new__(wake_module.WakeConsumer)
    consumer._app = types.SimpleNamespace(
        command_mode_active=False,
        config={},
        app_state="wake_session",
        wake_word_triggered=True,
        _wake_rearm_needs_onset=False,
        _wake_profile_detectors={},
        _wake_detector=None,
        _oww_wake_detected=False,
    )
    consumer._hands_free_capture_duck_token = 41
    rows, record_endpoint = _make_timing_sink(tmp_path)
    calls = []

    def process(buffer, src_rate, **kwargs):
        calls.append((buffer, src_rate, kwargs))
        record_endpoint(kwargs["last_voice_ns"], kwargs["speech_end_ns"])

    consumer._app.process_wake_word_buffer = process
    audio = [object()]
    consumer._flush(
        audio, owner_token=41,
        last_voice_ns=3_000_000_000,
        speech_end_ns=3_875_000_000,
    )

    assert calls[0][0] is audio
    assert calls[0][1] == 16000
    assert (calls[0][2]["last_voice_ns"], calls[0][2]["speech_end_ns"]) == (
        3_000_000_000, 3_875_000_000,
    )
    assert rows[0].endpoint_wait_ms == 875.0
    assert rows[0].last_voice_source == "vad"


def test_toggle_flush_preserves_endpoint_times_through_fifo(tmp_path, monkeypatch):
    consumer = object.__new__(wake_module.WakeConsumer)
    consumer._app = types.SimpleNamespace(
        command_mode_active=True,
        config={"command_mode": {"mode": "toggle"}},
    )
    consumer._hands_free_capture_duck_token = None
    consumer._toggle_queue = collections.deque()
    consumer._toggle_queue_lock = threading.Lock()
    consumer._toggle_worker_active = False
    rows, record_endpoint = _make_timing_sink(tmp_path)
    delivered = []

    def handle(buffer, src_rate, *, last_voice_ns, speech_end_ns):
        delivered.append((buffer, src_rate, last_voice_ns, speech_end_ns))
        record_endpoint(last_voice_ns, speech_end_ns)

    consumer._app._handle_command_mode_utterance = handle
    monkeypatch.setattr(
        wake_module.thread_registry, "spawn",
        lambda name, target, daemon=True: target(),
    )
    audio = [object()]
    consumer._flush(audio, last_voice_ns=8_000_000_000, speech_end_ns=8_650_000_000)

    assert delivered == [(audio, 16000, 8_000_000_000, 8_650_000_000)]
    assert rows[0].endpoint_wait_ms == 650.0
    assert rows[0].last_voice_source == "vad"
