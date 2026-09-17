"""Queue 176 -- Ava captions, the first accessibility mode.

A deaf user has to be able to hold the same conversation with Ava that a
hearing user has, so the things this file asserts are the things that make
that true and nothing else:

  * Ava's reply reaches the panel, with the text she actually said.
  * The panel is held while she speaks and lingers afterwards, for a
    configurable number of seconds -- reading speed is the whole point of
    the mode, so the linger is not a constant nobody can change.
  * Muting Ava's audio skips the SPEECH and not the CAPTION. A mute that
    also dropped the caption would leave the user with nothing at all.
  * Command acknowledgements and earcons are a different channel: neither
    is muted and neither is captioned in a window labelled Ava.
  * The dragged position survives, through the app's own config path.

The coordinator half drives the REAL `AudioCoordinator.speak()` with a mock
engine, because the hook only earns its place if the one entry point every
Ava utterance passes through is the one being tested. The panel half builds
the REAL widget under the tests' own QApplication.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from samsara.tts.coordinator import (
    AVA_SPEECH_CATEGORIES,
    MUTED_AVA_ID,
    AudioCoordinator,
)
from samsara.tts.engine_base import SpeechHandle
from samsara.ui import ava_captions_qt as captions


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Panel:
    """Records what the coordinator asked the panel to do."""

    def __init__(self):
        self.shown = []          # (text, hold)
        self.released = 0
        self.hidden = 0

    def show_caption(self, app, text, *, hold=True):
        self.shown.append((text, hold))

    def release_caption(self, app):
        self.released += 1

    def hide_captions(self):
        self.hidden += 1


@pytest.fixture
def panel(monkeypatch):
    """Substitute the panel facade, keeping the real config readers.

    The coordinator imports `samsara.ui.ava_captions_qt` and calls three
    functions on it; patching those three leaves the config predicates --
    which decide whether anything happens at all -- as the production ones.
    """
    fake = _Panel()
    monkeypatch.setattr(captions, "show_caption", fake.show_caption)
    monkeypatch.setattr(captions, "release_caption", fake.release_caption)
    monkeypatch.setattr(captions, "hide_captions", fake.hide_captions)
    return fake


def _coordinator(**accessibility):
    app = MagicMock()
    app.is_speaking = False
    app.command_mode_active = False
    app.config = {
        'wake_word_config': {'audio': {'speech_threshold': 0.03}},
        'tts': {'speed': 1.0, 'volume': 0.8},
    }
    if accessibility:
        app.config['accessibility'] = dict(accessibility)
    engine = MagicMock()
    engine.get_engine_state.return_value = 'idle'
    engine.is_speaking.return_value = False
    engine.speak.return_value = SpeechHandle(utterance_id='uid-1')
    return AudioCoordinator(app, engine, config={}), app, engine


REPLY = ("The kettle is on the second shelf. I moved it there "
         "when the cupboard was rearranged.")


# ---------------------------------------------------------------------------
# 1. The caption arrives for a spoken reply
# ---------------------------------------------------------------------------

class TestCaptionArrives:
    def test_an_ava_reply_reaches_the_panel_with_its_own_words(self, panel):
        coord, _app, engine = _coordinator(ava_captions=True)
        coord.speak(REPLY, category="ava_response")
        assert panel.shown == [(REPLY, True)]
        # And it still SPEAKS: captions are an added channel, not a swap.
        assert engine.speak.call_count == 1
        assert engine.speak.call_args.args[0] == REPLY

    @pytest.mark.parametrize("category", sorted(AVA_SPEECH_CATEGORIES))
    def test_every_ava_voice_category_is_captioned(self, panel, category):
        """Ava answers, Ava's status sentences and the Ava command session
        are all Ava talking; a user reading instead of listening must not
        lose one lane because it was added later."""
        coord, _app, _engine = _coordinator(ava_captions=True)
        coord.speak("Ava is offline.", category=category)
        assert panel.shown == [("Ava is offline.", True)]

    def test_nothing_is_captioned_while_the_mode_is_off(self, panel):
        coord, _app, engine = _coordinator()
        coord.speak(REPLY, category="ava_response")
        assert panel.shown == []
        assert engine.speak.call_count == 1

    def test_a_command_acknowledgement_is_not_an_ava_caption(self, panel):
        """`agent_response` is a plugin saying it did something, not Ava.
        Captioning it would put command chatter in Ava's window."""
        coord, _app, engine = _coordinator(ava_captions=True)
        coord.speak("Alarm set for seven.", category="agent_response")
        assert panel.shown == []
        assert engine.speak.call_count == 1

    def test_the_mode_is_read_per_utterance_not_cached(self, panel):
        """It is a settings mode the user can switch mid-conversation; a
        cached flag would mean it only took effect after a restart."""
        coord, app, _engine = _coordinator()
        coord.speak("one", category="ava_response")
        app.config['accessibility'] = {'ava_captions': True}
        coord.speak("two", category="ava_response")
        assert panel.shown == [("two", True)]


# ---------------------------------------------------------------------------
# 2. The linger
# ---------------------------------------------------------------------------

class TestLinger:
    def test_the_caption_is_held_while_she_speaks_then_released(self, panel):
        coord, _app, engine = _coordinator(ava_captions=True)
        coord.speak(REPLY, category="ava_response")
        assert panel.shown == [(REPLY, True)], "held, not already counting down"
        assert panel.released == 0

        # The engine reports the utterance finished: NOW the linger starts.
        engine.speak.call_args.kwargs['on_done']()
        assert panel.released == 1

    def test_the_callers_own_on_done_still_runs(self, panel):
        """The caption wrapper must not swallow the callback the caller
        passed -- Ava's session relies on it to advance."""
        coord, _app, engine = _coordinator(ava_captions=True)
        seen = []
        coord.speak(REPLY, category="ava_response", on_done=lambda: seen.append(1))
        engine.speak.call_args.kwargs['on_done']()
        assert seen == [1] and panel.released == 1

    def test_the_default_linger_is_six_seconds(self):
        assert captions.caption_linger_s({}) == 6.0
        assert captions.LINGER_DEFAULT_S == 6.0

    def test_the_linger_is_configurable(self):
        assert captions.caption_linger_s(
            {'accessibility': {'ava_captions_linger_s': 12}}) == 12.0
        # A flat dotted key is honoured too: a hand-edited config should not
        # lose the setting to a nesting detail.
        assert captions.caption_linger_s(
            {'accessibility.ava_captions_linger_s': 9.5}) == 9.5

    @pytest.mark.parametrize("value, expected", [
        (0.0, captions.LINGER_MIN_S),          # never flash past a reader
        (-4, captions.LINGER_MIN_S),
        (9999, captions.LINGER_MAX_S),         # never a panel that stays forever
        ("soon", captions.LINGER_DEFAULT_S),
        (None, captions.LINGER_DEFAULT_S),
        (float("nan"), captions.LINGER_DEFAULT_S),
    ])
    def test_a_bad_linger_falls_back_rather_than_stranding_the_panel(
            self, value, expected):
        assert captions.caption_linger_s(
            {'accessibility': {'ava_captions_linger_s': value}}) == expected

    def test_a_held_caption_has_a_hard_cap(self):
        """If the engine never reports done -- a cancelled utterance, a dead
        voice -- the panel still comes down. The allowance scales with the
        text, because a long reply legitimately takes longer to speak."""
        short = captions.speech_allowance_s("Yes.")
        long = captions.speech_allowance_s("word " * 200)
        assert short == captions._SPEECH_ALLOWANCE_MIN_S
        assert long > short
        assert long <= captions._SPEECH_ALLOWANCE_MAX_S


# ---------------------------------------------------------------------------
# 3. Mute skips TTS but not the caption
# ---------------------------------------------------------------------------

class TestMute:
    def test_mute_skips_the_speech_and_still_shows_the_caption(self, panel):
        coord, _app, engine = _coordinator(ava_captions=True, ava_mute_audio=True)
        handle = coord.speak(REPLY, category="ava_response")
        assert engine.speak.call_count == 0, "Ava's audio was not muted"
        assert panel.shown == [(REPLY, False)], "the caption went with the audio"
        assert handle.utterance_id == MUTED_AVA_ID

    def test_a_muted_caption_starts_its_linger_immediately(self, panel):
        """There is no speech to wait for, so holding the panel open would
        wait for an event that is never coming."""
        coord, _app, _engine = _coordinator(ava_captions=True, ava_mute_audio=True)
        coord.speak(REPLY, category="ava_response")
        assert panel.shown[0][1] is False

    def test_mute_does_not_silence_command_acknowledgements(self, panel):
        """Someone who muted Ava has not asked for a silent app: the short
        confirmations that say a command landed still speak."""
        coord, _app, engine = _coordinator(ava_captions=True, ava_mute_audio=True)
        coord.speak("Alarm set for seven.", category="agent_response")
        assert engine.speak.call_count == 1
        assert panel.shown == []

    def test_mute_does_not_touch_earcons(self, panel):
        """Earcons never go through speak() at all -- they reach the
        coordinator as on_earcon_starting. Asserted so a later refactor that
        routes them through speak() cannot silence them by accident."""
        coord, _app, engine = _coordinator(ava_captions=True, ava_mute_audio=True)
        engine.get_engine_state.return_value = 'playing'
        coord.on_earcon_starting('success')
        assert engine.speak.call_count == 0
        assert engine.cancel_all.call_count == 0

    def test_mute_alone_with_captions_off_still_mutes(self, panel):
        """The two keys are independent. Someone who wants silence without a
        panel gets silence -- and no caption, because they turned it off."""
        coord, _app, engine = _coordinator(ava_mute_audio=True)
        coord.speak(REPLY, category="ava_response")
        assert engine.speak.call_count == 0
        assert panel.shown == []

    def test_a_broken_captions_module_never_costs_the_speech(self, monkeypatch):
        """The failure mode that matters: a UI import problem must degrade to
        'no captions', never to 'Ava stopped talking'."""
        coord, _app, engine = _coordinator(ava_captions=True)
        monkeypatch.setattr(
            captions, "captions_enabled",
            MagicMock(side_effect=RuntimeError("boom")))
        coord.speak(REPLY, category="ava_response")
        assert engine.speak.call_count == 1


# ---------------------------------------------------------------------------
# 4. Position persists
# ---------------------------------------------------------------------------

class TestPosition:
    def test_the_default_is_a_preset(self):
        assert captions.caption_position({}) == "bottom-center"
        assert captions.POSITION_DEFAULT in captions.POSITION_PRESETS

    def test_a_dragged_position_round_trips_through_the_scheme(self):
        stored = captions.serialize_position(r"\\.\DISPLAY2", 0.25, 0.8)
        assert stored.startswith("custom|")
        kind, screen, cx, cy = captions.caption_position(
            {'accessibility': {'ava_captions_position': stored}})
        assert (kind, screen) == ("custom", r"\\.\DISPLAY2")
        assert (round(cx, 4), round(cy, 4)) == (0.25, 0.8)

    def test_it_persists_through_the_apps_own_config_path(self):
        """Written with update_config_and_save, under one lock, like every
        other runtime config change -- not by rewriting the file here."""
        saved = {}
        app = types.SimpleNamespace(
            config={'accessibility': {'ava_captions': True}},
            update_config_and_save=lambda updates: saved.update(updates),
        )
        value = captions.serialize_position("DISPLAY1", 0.5, 0.1)
        assert captions.store_position(app, value) is True
        assert saved['accessibility']['ava_captions_position'] == value
        # The rest of the block survives: the toggles are stored beside it.
        assert saved['accessibility']['ava_captions'] is True

    def test_it_falls_back_to_the_config_dict_without_the_app_helper(self):
        app = types.SimpleNamespace(config={})
        assert captions.store_position(app, "top-left") is True
        assert app.config['accessibility']['ava_captions_position'] == "top-left"

    @pytest.mark.parametrize("stored", [
        "custom|DISPLAY1|nope|0.5",        # non-numeric
        "custom|DISPLAY1",                 # truncated
        "custom|DISPLAY1|nan|0.5",         # non-finite
        "somewhere-else",                  # not a preset
        None,
        42,
    ])
    def test_a_malformed_position_falls_back_to_the_default(self, stored):
        assert captions.caption_position(
            {'accessibility': {'ava_captions_position': stored}}
        ) == captions.POSITION_DEFAULT

    def test_a_custom_position_is_clamped_into_its_monitor(self):
        stored = "custom|DISPLAY1|-3.0|9.0"
        _kind, _screen, cx, cy = captions.caption_position(
            {'accessibility': {'ava_captions_position': stored}})
        assert (cx, cy) == (0.0, 1.0)


# ---------------------------------------------------------------------------
# 5. Config surface
# ---------------------------------------------------------------------------

class TestConfigSurface:
    def test_both_toggles_default_to_off(self):
        from samsara.config_schema import SETTINGS_SCHEMA

        for key in ("accessibility.ava_captions", "accessibility.ava_mute_audio"):
            assert SETTINGS_SCHEMA[key]["type"] == "bool"
            assert SETTINGS_SCHEMA[key]["default"] is False
            assert SETTINGS_SCHEMA[key]["tab"] == "general"

    def test_the_defaults_table_carries_them(self):
        from samsara import config_defaults

        assert config_defaults.DEFAULTS["accessibility.ava_captions"] is False
        assert config_defaults.DEFAULTS["accessibility.ava_mute_audio"] is False
        assert config_defaults.DEFAULTS["accessibility.ava_captions_linger_s"] == 6.0
        assert config_defaults.DEFAULTS["accessibility.ava_captions_position"] \
            == captions.POSITION_DEFAULT

    def test_audio_stays_on_by_default(self):
        """The owner's design: captions are additive. Enabling them must not
        take the voice away from someone who can hear it."""
        assert captions.ava_audio_muted({}) is False
        assert captions.ava_audio_muted(
            {'accessibility': {'ava_captions': True}}) is False

    def test_a_non_dict_accessibility_block_is_survivable(self):
        """A hand-edited config must not cost the user their assistant."""
        for broken in ("yes", 7, [], None):
            assert captions.captions_enabled({'accessibility': broken}) is False
            assert captions.caption_linger_s({'accessibility': broken}) == 6.0


# ---------------------------------------------------------------------------
# 6. The panel itself
# ---------------------------------------------------------------------------

def _app_with(**accessibility):
    return types.SimpleNamespace(
        config={'accessibility': dict(accessibility)} if accessibility else {},
        update_config_and_save=lambda updates: None,
    )


@pytest.fixture
def window(qapp):
    captions.reset_for_test()
    yield
    captions.reset_for_test()


class TestThePanel:
    def test_no_qt_object_exists_until_a_caption_is_posted(self, window):
        """Queue 173 created a QObject at module import and the app
        access-violated at splash on every start."""
        assert captions.active_window() is None

    def test_showing_a_caption_builds_the_panel_and_paints_the_text(
            self, qapp, window):
        app = _app_with(ava_captions=True)
        captions.show_caption(app, REPLY)
        qapp.processEvents()
        panel = captions.active_window()
        assert panel is not None
        assert panel._text.text() == REPLY
        assert panel.isVisible()

    def test_the_caption_text_clears_the_body_type_floor(self, qapp, window):
        from samsara.ui import theme

        captions.show_caption(_app_with(ava_captions=True), REPLY)
        qapp.processEvents()
        panel = captions.active_window()
        assert f"{theme.TYPE_TITLE}px" in panel._text.styleSheet()
        assert theme.TYPE_TITLE >= 16
        assert panel._text.wordWrap() is True

    def test_it_never_takes_focus_from_the_window_being_dictated_into(
            self, qapp, window):
        from PySide6.QtCore import Qt

        captions.show_caption(_app_with(ava_captions=True), REPLY)
        qapp.processEvents()
        panel = captions.active_window()
        flags = panel.windowFlags()
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
        assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def test_it_auto_sizes_to_the_reply_and_wraps_instead_of_widening(
            self, qapp, window):
        app = _app_with(ava_captions=True)
        captions.show_caption(app, "Yes.")
        qapp.processEvents()
        short_height = captions.active_window().height()

        captions.show_caption(app, REPLY * 3)
        qapp.processEvents()
        panel = captions.active_window()
        assert panel.height() > short_height, "it did not grow for a long reply"
        assert panel.width() <= captions._MAX_WIDTH_PX + 2, "it widened instead of wrapping"

    def test_a_click_dismisses_it(self, qapp, window):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        captions.show_caption(_app_with(ava_captions=True), REPLY)
        qapp.processEvents()
        panel = captions.active_window()
        spot = QPointF(panel.rect().center())

        def _event(kind):
            return QMouseEvent(kind, spot, panel.mapToGlobal(spot.toPoint()),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)

        panel.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress))
        panel.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease))
        qapp.processEvents()
        assert not panel.isVisible()

    def test_a_drag_moves_it_and_does_not_dismiss_it(self, qapp, window):
        """The two gestures share a button, so a user repositioning the
        panel must not lose the caption they were still reading."""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        saved = {}
        app = types.SimpleNamespace(
            config={'accessibility': {'ava_captions': True}},
            update_config_and_save=lambda updates: saved.update(updates))
        captions.show_caption(app, REPLY)
        qapp.processEvents()
        panel = captions.active_window()
        start = QPointF(panel.rect().center())
        far = start + QPointF(120, -60)

        def _event(kind, local):
            return QMouseEvent(kind, local, panel.mapToGlobal(local.toPoint()),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)

        panel.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, start))
        panel.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, far))
        panel.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, far))
        qapp.processEvents()

        assert panel.isVisible(), "a drag dismissed the caption"
        stored = saved['accessibility']['ava_captions_position']
        assert stored.startswith("custom|")
        assert captions.caption_position(saved)[0] == "custom"

    def test_release_starts_the_countdown_only_on_a_visible_panel(
            self, qapp, window):
        app = _app_with(ava_captions=True)
        captions.release_caption(app)         # nothing built yet
        qapp.processEvents()
        assert captions.active_window() is None

        captions.show_caption(app, REPLY)
        qapp.processEvents()
        panel = captions.active_window()
        captions.release_caption(app)
        qapp.processEvents()
        assert panel._timer.isActive()
        assert panel._timer.remainingTime() <= 6000

    def test_an_empty_reply_shows_nothing(self, qapp, window):
        captions.show_caption(_app_with(ava_captions=True), "   ")
        qapp.processEvents()
        assert captions.active_window() is None

    def test_hide_takes_the_panel_down_and_stops_the_timer(self, qapp, window):
        app = _app_with(ava_captions=True)
        captions.show_caption(app, REPLY)
        qapp.processEvents()
        panel = captions.active_window()
        captions.hide_captions()
        qapp.processEvents()
        assert not panel.isVisible()
        assert not panel._timer.isActive()

    def test_restyle_reads_the_live_palette_every_time(self, qapp, window):
        """A stylesheet built once keeps the palette it was born with, which
        is a window that does not switch (tests/test_colour_tokens.py)."""
        from samsara.ui import theme

        try:
            theme.set_theme("dark", refresh=False)
            captions.show_caption(_app_with(ava_captions=True), REPLY)
            qapp.processEvents()
            panel = captions.active_window()
            dark_sheet = panel._card.styleSheet()

            theme.set_theme("light", refresh=False)
            panel.restyle()
            assert panel._card.styleSheet() != dark_sheet
            assert theme.PALETTES["light"]["BG1"] in panel._card.styleSheet()
        finally:
            theme.set_theme("dark", refresh=False)


# ---------------------------------------------------------------------------
# 7. The Settings controls
# ---------------------------------------------------------------------------

class TestSettingsControls:
    def _page(self, config=None):
        from tests._theme_stub_app import build_settings_window

        return build_settings_window(config)

    def test_both_toggles_live_in_the_one_accessibility_section(self, qapp):
        window = self._page()
        try:
            assert 'ava_captions' in window._widgets
            assert 'ava_mute_audio' in window._widgets
            assert window._widgets['ava_captions'].accessibleName() \
                == "Show Ava captions"
            assert window._widgets['ava_mute_audio'].accessibleName() \
                == "Mute Ava's voice"
        finally:
            window.close()

    def test_they_show_what_is_stored_and_save_what_is_shown(self, qapp):
        window = self._page({'accessibility': {'ava_captions': True,
                                               'ava_mute_audio': False}})
        try:
            assert window._widgets['ava_captions'].isChecked() is True
            assert window._widgets['ava_mute_audio'].isChecked() is False
            window._widgets['ava_mute_audio'].setChecked(True)
            updates = {}
            for save in window._save_fns:
                result = save({})
                if isinstance(result, dict):
                    updates.update(result)
            assert updates['accessibility']['ava_captions'] is True
            assert updates['accessibility']['ava_mute_audio'] is True
        finally:
            window.close()

    def test_saving_keeps_the_stored_caption_position(self, qapp):
        """The panel writes its dragged position into the same block; an
        unrelated Settings save must not throw it away."""
        stored = captions.serialize_position("DISPLAY1", 0.4, 0.9)
        window = self._page({'accessibility': {'ava_captions_position': stored}})
        try:
            updates = {}
            for save in window._save_fns:
                result = save({})
                if isinstance(result, dict):
                    updates.update(result)
            assert updates['accessibility']['ava_captions_position'] == stored
        finally:
            window.close()
