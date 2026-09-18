"""Speech pace profiles and the fragment-command safety boundary."""
from __future__ import annotations

import pytest

from samsara import session_modes as sm
from samsara.speech_pace import (RELAXED, STANDARD, UNHURRIED, clear_gap_s,
                                 effective_value, measured_interword_pauses,
                                 recommend_profile)


KEYS = {
    "command_mode.dictate_utterance_silence_s": 0.65,
    "silence_threshold": 2.0,
    "wake_word_config.quick_silence_timeout": 1.0,
    "wake_word_config.audio.wake_command_timeout": 5.0,
    "min_speech_duration": 0.3,
}


def _config(pace=STANDARD):
    return {"accessibility": {"speech_pace": pace}, "command_mode": {"dictate_utterance_silence_s": 0.65},
            "silence_threshold": 2.0, "min_speech_duration": 0.3,
            "wake_word_config": {"quick_silence_timeout": 1.0,
                                 "audio": {"wake_command_timeout": 5.0}}}


@pytest.mark.parametrize("value", (0.65, 2.0, 1.25))
def test_standard_is_identity_for_present_and_missing_values(value):
    config = _config()
    config["command_mode"]["dictate_utterance_silence_s"] = value
    assert effective_value(config, "command_mode.dictate_utterance_silence_s", 0.65) == value
    assert effective_value({"accessibility": {"speech_pace": STANDARD}},
                           "command_mode.dictate_utterance_silence_s", 2.0) == 2.0


def test_profiles_scale_all_five_and_custom_key_wins():
    relaxed, unhurried = _config(RELAXED), _config(UNHURRIED)
    for key, baseline in KEYS.items():
        relaxed_value = effective_value(relaxed, key, baseline)
        unhurried_value = effective_value(unhurried, key, baseline)
        if key == "min_speech_duration":
            assert unhurried_value <= relaxed_value < baseline
        else:
            assert unhurried_value > relaxed_value > baseline
    custom = _config("custom")
    custom["accessibility"]["speech_pace_custom"] = {"silence_threshold": 9.0}
    assert effective_value(custom, "silence_threshold", 2.0) == 9.0


def test_slow_fragment_cannot_execute_a_command_but_clear_gap_can():
    now = [0.0]
    commands = []
    manager = sm.SessionModeManager(
        abort_phrases=[], foreground_exe_resolver=lambda: "app.exe", inject_fn=lambda text: text,
        remove_chars_fn=lambda _: None,
        command_dispatch_fn=lambda text: commands.append(text) or sm.CommandDispatchResult(True, text),
        agent_dispatch_fn=lambda *_: None, buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=lambda text: sm.HandsFreeCommandMatch(text, text)
        if text == "switch window" else None, clock=lambda: now[0], fragment_clear_gap_s=3.75,
    )
    manager.reset(initial_mode=sm.SessionMode.DICTATE)
    signals = sm.UtteranceSignals(True, (1.0,))
    assert manager.dispatch_utterance("I told him to", signals).kind == "dictate_staged"
    now[0] = 1.5
    assert manager.dispatch_utterance("switch window", signals).kind == "dictate_staged"
    assert commands == []
    now[0] = 6.0
    assert manager.dispatch_utterance("switch window", signals).kind == "hands_free_command_executed"
    assert commands == ["switch window"]


def test_synthetic_vad_pauses_recommend_without_applying():
    samples = [1.0, 1.0] + [0.0] * 15 + [1.0, 1.0]
    pauses = measured_interword_pauses(samples, 10)
    assert pauses == [1.5]
    assert recommend_profile(pauses) == RELAXED
