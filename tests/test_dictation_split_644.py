"""Tray extraction equivalence without importing or bootstrapping dictation.

The frozen class below is the eight original methods at c9249546. It is a
repository-local oracle so these checks also run in a plain source export.
"""

import ast
import builtins
import hashlib
import itertools
import logging
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara import presentation_state
from samsara.constants import ICON_TICK_FAST, ICON_TICK_MEDIUM, ICON_TICK_SLOW
from samsara.runtime import thread_registry
from samsara.ui import tray_qt


METHODS = (
    'create_icon_image', '_push_tray_icon', '_flash_tray_heard',
    '_request_icon_chase', '_release_icon_chase', '_start_icon_chase',
    '_stop_icon_chase', '_icon_chase_tick',
)
FLAGS = ('recording', 'ava_mode_active', 'command_mode_active',
         'continuous_active', 'wake_word_active', 'snoozed')
logger = logging.getLogger('Samsara')


class Before:
    def create_icon_image(self, rotation=0.0, opacity=1.0):
        """The tray frame for the live state: a tray_qt.MarkFrame.

        Only describes the frame -- tray_qt renders it with render_mark on
        the Qt thread, so this is safe from the icon timer's worker thread.
        rotation is in radians (the chase timer's unit).
        """
        from samsara.ui.tray_qt import MarkFrame
        capture, eye = self._tray_mark()
        return MarkFrame(capture, eye, math.degrees(rotation), opacity)

    def _push_tray_icon(self):
        """Show the live tray state now (outside an animation tick)."""
        if not hasattr(self, 'tray_icon'):
            return
        try:
            self.tray_icon.icon = self.create_icon_image(
                rotation=getattr(self, '_icon_rotation', 0.0))
        except OSError as e:
            logger.debug(f"Tray icon state swap failed: {e}")

    def _flash_tray_heard(self):
        """Wake phrase heard: play tray_qt.HEARD_KEYFRAMES on the tray eye."""
        from samsara.ui.tray_qt import HEARD_KEYFRAMES
        self._tray_heard_generation = getattr(self, '_tray_heard_generation', 0) + 1
        generation = self._tray_heard_generation

        def _step(index):
            if generation != self._tray_heard_generation:
                return   # a newer flash superseded this one
            at_ms, eye = HEARD_KEYFRAMES[index]
            self._tray_heard_eye = eye
            self._push_tray_icon()
            if index + 1 < len(HEARD_KEYFRAMES):
                delay_s = (HEARD_KEYFRAMES[index + 1][0] - at_ms) / 1000.0
                thread_registry.timer(
                    "dictation.tray_heard", delay_s,
                    lambda: _step(index + 1), daemon=True)

        _step(0)

    def _request_icon_chase(self, reason):
        """Register a reason for the icon to animate. Starts animation if not running."""
        self._icon_anim_reasons.add(reason)
        if not self._icon_animating:
            self._start_icon_chase()

    def _release_icon_chase(self, reason):
        """Remove a reason for animation. Stops only when ALL reasons are gone."""
        self._icon_anim_reasons.discard(reason)
        if not self._icon_anim_reasons and self._icon_animating:
            self._stop_icon_chase()

    def _start_icon_chase(self):
        """Start the spinning color-chase animation on the tray icon."""
        self._icon_animating = True
        self._icon_chase_offset = 0
        self._icon_chase_counter = 0
        # _icon_rotation is NOT reset: the head keeps chasing the tail from
        # wherever it stopped, never snapping to an aligned rest pose.
        self._icon_chase_tick()

    def _stop_icon_chase(self):
        """Stop the chase animation and show idle icon."""
        self._icon_animating = False
        if self._icon_chase_timer is not None:
            self._icon_chase_timer.cancel()
            self._icon_chase_timer = None
        self._icon_chase_offset = 0
        self._push_tray_icon()   # keeps the current rotation (no aligned rest)

    def _icon_chase_tick(self):
        """Advance the tray animation and schedule the next tick.

        Motion means capture (queue 19): the ring TURNS in every active
        capture state -- the ouroboros head chasing its tail -- and speed is
        the state channel (tray_qt.SPIN_SECONDS_PER_TURN):
        - recording:     filled red wheel AND turning, 'recording' pace
                         (fill/colour = state, rotation = liveness)
        - transcribing:  the 'recording' reason outlives capture -> fastest
        - continuous:    'listening' pace (ambient)
        - wake_word:     'armed' pace (ambient)
        No opacity pulse any more. At rest the chase is released and the mark
        stands still (_stop_icon_chase). Thinking (2.4 s/turn) is signalled
        only to the listening indicator (set_thinking from ask_ollama / the Ava
        command session), so the tray has no thinking reason. Tick intervals
        stay the existing ICON_TICK_* constants.
        """
        if not self._icon_animating:
            return

        from samsara.ui.tray_qt import SPIN_SECONDS_PER_TURN

        # Highest-priority active reason picks the tick and the pace.
        if getattr(self, 'recording', False):
            tick_interval, pace = ICON_TICK_FAST, 'recording'
        elif 'recording' in self._icon_anim_reasons:
            tick_interval, pace = ICON_TICK_FAST, 'transcribing'
        elif 'continuous' in self._icon_anim_reasons:
            tick_interval, pace = ICON_TICK_MEDIUM, 'listening'
        else:  # wake_word or anything else
            tick_interval, pace = ICON_TICK_SLOW, 'armed'

        self._icon_rotation += 2 * math.pi * tick_interval / SPIN_SECONDS_PER_TURN[pace]

        if hasattr(self, 'tray_icon'):
            try:
                self.tray_icon.icon = self.create_icon_image(rotation=self._icon_rotation)
            except OSError as e:
                # transient WinError during icon handle swap -- skip this frame
                logger.debug(f"Tray icon animation frame swap failed: {e}")

        self._icon_chase_timer = thread_registry.timer(
            "dictation.icon_chase", tick_interval,
            self._icon_chase_tick, daemon=True)



@pytest.fixture(scope='module')
def facade():
    source = (Path(__file__).resolve().parents[1] / 'dictation.py').read_text(
        encoding='utf-8-sig')
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'DictationApp')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in (*METHODS, '_tray_mark')]
    assert len(methods) == len(METHODS) + 1
    namespace = {'_presentation_state': presentation_state}
    exec(compile(ast.Module(body=methods, type_ignores=[]),
                 '<tray-644-facade>', 'exec'), namespace)
    return type('Facade', (), {n.name: namespace[n.name] for n in methods})


def app_of(cls, **state):
    app = cls()
    for name in FLAGS:
        setattr(app, name, False)
    app._tray_heard_eye = None
    app._icon_anim_reasons = set()
    app._icon_animating = False
    app._icon_rotation = 0.61
    app._icon_chase_offset = 0
    app._icon_chase_counter = 0
    app._icon_chase_timer = None
    app._tray_mark = lambda: presentation_state.tray_mark(vars(app))
    app.tray_icon = SimpleNamespace(icon=None)
    for name, value in state.items():
        setattr(app, name, value)
    return app


def image_hash(frame, size):
    image = tray_qt.render_mark(*frame[:2], size, *frame[2:],
                                small_max=tray_qt.TASKBAR_SMALL_MAX)
    return hashlib.sha256(bytes(image.constBits())).hexdigest()


@pytest.mark.parametrize('flags', itertools.product((False, True), repeat=6))
def test_every_live_and_heard_state_has_identical_pixels(qapp, facade, flags):
    before = app_of(Before, **dict(zip(FLAGS, flags)))
    after = app_of(facade, **dict(zip(FLAGS, flags)))
    for eye in (None, 'heard', 'armed', 'asleep', 'off'):
        before._tray_heard_eye = after._tray_heard_eye = eye
        expected = before.create_icon_image(rotation=0.61)
        actual = after.create_icon_image(rotation=0.61)
        assert actual == expected
        for size in (16, 24, 32):
            assert image_hash(actual, size) == image_hash(expected, size)


@pytest.mark.parametrize('rotation', (0.0, -math.pi / 3, math.pi, 200 * math.pi))
@pytest.mark.parametrize('opacity', (0.0, 0.4, 1.0))
def test_rotation_and_opacity_remain_pixel_identical(qapp, facade, rotation, opacity):
    for capture, eye in itertools.product(('idle', 'listening', 'recording', 'ava'),
                                           ('off', 'asleep', 'armed', 'heard')):
        before, after = app_of(Before), app_of(facade)
        before._tray_mark = after._tray_mark = lambda: (capture, eye)
        expected = before.create_icon_image(rotation, opacity)
        actual = after.create_icon_image(rotation, opacity)
        assert actual == expected
        for size in (16, 24, 32, 48):
            assert image_hash(actual, size) == image_hash(expected, size)


class Timer:
    def __init__(self, name, delay, callback, daemon):
        self.name, self.delay, self.callback, self.daemon = name, delay, callback, daemon
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def record_timers(monkeypatch):
    timers = []

    def timer(name, delay, callback, daemon):
        result = Timer(name, delay, callback, daemon)
        timers.append(result)
        return result

    monkeypatch.setattr(thread_registry, 'timer', timer)
    return timers


@pytest.mark.parametrize('reasons', [set(x) for n in range(4) for x in
                                    itertools.combinations(('wake_word', 'continuous', 'recording'), n)])
@pytest.mark.parametrize('recording', (False, True))
def test_priority_cadence_and_full_turn_frames(qapp, facade, monkeypatch, reasons, recording):
    timers = record_timers(monkeypatch)
    before = app_of(Before, recording=recording, _icon_animating=True,
                    _icon_anim_reasons=set(reasons))
    after = app_of(facade, recording=recording, _icon_animating=True,
                   _icon_anim_reasons=set(reasons))
    if recording:
        delay, pace = ICON_TICK_FAST, 'recording'
    elif 'recording' in reasons:
        delay, pace = ICON_TICK_FAST, 'transcribing'
    elif 'continuous' in reasons:
        delay, pace = ICON_TICK_MEDIUM, 'listening'
    else:
        delay, pace = ICON_TICK_SLOW, 'armed'
    count = math.ceil(tray_qt.SPIN_SECONDS_PER_TURN[pace] / delay)
    for index in range(count):
        before._icon_chase_tick()
        after._icon_chase_tick()
        assert before._icon_rotation == after._icon_rotation
        assert after._icon_rotation == pytest.approx(
            0.61 + (index + 1) * 2 * math.pi * delay / tray_qt.SPIN_SECONDS_PER_TURN[pace])
        assert before.tray_icon.icon == after.tray_icon.icon
        for size in (16, 24, 32):
            assert image_hash(before.tray_icon.icon, size) == image_hash(after.tray_icon.icon, size)
        for timer, app in zip(timers[-2:], (before, after)):
            assert (timer.name, timer.delay, timer.daemon) == ('dictation.icon_chase', delay, True)
            assert timer.callback == app._icon_chase_tick


def test_reason_lifetime_stop_restart_and_pending_callback(facade, monkeypatch):
    timers = record_timers(monkeypatch)
    for cls in (Before, facade):
        app = app_of(cls)
        app._request_icon_chase('wake_word')
        first = timers[-1]
        angle = app._icon_rotation
        app._request_icon_chase('wake_word')
        app._request_icon_chase('recording')
        assert timers[-1] is first
        app._release_icon_chase('missing')
        app._release_icon_chase('wake_word')
        assert app._icon_animating and not first.cancelled
        app._release_icon_chase('recording')
        assert not app._icon_animating and first.cancelled
        assert app._icon_chase_timer is None and app._icon_chase_offset == 0
        assert app._icon_rotation == angle
        count = len(timers)
        first.callback()
        assert len(timers) == count
        app._request_icon_chase('continuous')
        assert app._icon_rotation > angle


def test_heard_flash_cadence_and_new_generation_supersedes_old(facade, monkeypatch):
    timers = record_timers(monkeypatch)
    for cls in (Before, facade):
        app = app_of(cls, wake_word_active=True)
        frames = []
        app._push_tray_icon = lambda: frames.append(app._tray_mark())
        app._flash_tray_heard()
        stale = timers[-1]
        app._flash_tray_heard()
        count = len(frames), len(timers)
        stale.callback()
        assert (len(frames), len(timers)) == count
        for index in range(len(tray_qt.HEARD_KEYFRAMES) - 1):
            timer = timers[-1]
            at_ms = tray_qt.HEARD_KEYFRAMES[index][0]
            next_ms = tray_qt.HEARD_KEYFRAMES[index + 1][0]
            assert (timer.name, timer.delay, timer.daemon) == (
                'dictation.tray_heard', (next_ms - at_ms) / 1000, True)
            timer.callback()
        assert [eye for _, eye in frames] == ['heard', 'heard', 'armed', 'heard', 'armed', 'asleep', 'armed']


@pytest.mark.parametrize('has_tray', (False, True))
def test_missing_tray_and_transient_swap_failure_keep_timer_alive(facade, monkeypatch, caplog, has_tray):
    timers = record_timers(monkeypatch)

    class BrokenTray:
        @property
        def icon(self):
            return None

        @icon.setter
        def icon(self, frame):
            raise OSError('transient icon swap')

    for cls in (Before, facade):
        app = app_of(cls, _icon_animating=True)
        if has_tray:
            app.tray_icon = BrokenTray()
        else:
            del app.tray_icon
        app._push_tray_icon()
        app._icon_chase_tick()
        assert timers[-1].callback == app._icon_chase_tick


def test_facade_imports_animation_only_on_call(facade, monkeypatch):
    calls = []
    original = builtins.__import__

    def track(name, *args, **kwargs):
        if name == 'samsara.tray_animation':
            calls.append(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', track)
    app = app_of(facade)
    assert calls == []
    assert isinstance(app.create_icon_image(), tray_qt.MarkFrame)
    assert calls == ['samsara.tray_animation']


def test_timer_path_only_describes_frames(facade, monkeypatch):
    record_timers(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail('timer path must leave QImage rendering to the Qt tray')

    monkeypatch.setattr(tray_qt, 'render_mark', forbidden)
    app = app_of(facade)
    app._request_icon_chase('recording')
    assert isinstance(app.tray_icon.icon, tray_qt.MarkFrame)
