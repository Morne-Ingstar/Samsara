"""Queue 199: independent capability facts behind the startup ready ladder."""

from __future__ import annotations


def test_each_ready_signal_changes_only_its_own_ladder_line():
    from samsara.boot import ReadyLadder

    clock = iter((10.0, 11.4, 13.1, 14.8))
    ladder = ReadyLadder(clock=lambda: next(clock))
    initial = ladder.snapshot()

    ladder.set_hotkey_ready()
    hotkey = ladder.snapshot()
    assert hotkey[0] == ("hotkey", "hotkey: ready \u2713 1.4 s", "ready")
    assert hotkey[1:] == initial[1:]

    ladder.set_wake_state("ready")
    wake = ladder.snapshot()
    assert wake[1] == ("wake", "wake: ready \u2713 3.1 s", "ready")
    assert wake[0] == hotkey[0]
    assert wake[2] == initial[2]

    ladder.set_ava_readiness("OFFLINE")
    ava = ladder.snapshot()
    assert ava[2] == ("ava", "Ava: offline", "offline")
    assert ava[:2] == wake[:2]


def test_disabled_wake_and_ava_warming_have_visible_text_states():
    from samsara.boot import ReadyLadder

    ladder = ReadyLadder(clock=lambda: 4.0)
    ladder.set_wake_state("off")
    ladder.set_ava_readiness("WARMING")

    assert ladder.lines() == (
        ("hotkey: loading\u2026", "loading"),
        ("wake: off", "off"),
        ("Ava: warming\u2026", "warming"),
    )


def test_speech_model_status_publishes_only_the_hotkey_ready_signal():
    from samsara.boot import ReadyLadder

    published = []
    ladder = ReadyLadder(clock=lambda: 2.0)
    ladder.attach_splash(type("Splash", (), {
        "set_ready_ladder": lambda _self, lines: published.append(lines),
    })())

    ladder.observe_status("Speech model ready...")

    assert published[-1][0] == ("hotkey: ready \u2713 0.0 s", "ready")
    assert published[-1][1:] == (
        ("wake: checking\u2026", "checking"),
        ("Ava: checking\u2026", "checking"),
    )
