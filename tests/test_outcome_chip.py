"""Outcome chip (queue 41): the listening indicator says what just happened.

Before this, a MISS, a mode switch, a refused commit and a failed command all
sounded like the same beep, and a deaf user got nothing. These tests pin:

  * the vocabulary in samsara.session_modes.outcome_chip -- every
    DispatchOutcome kind the module can produce is enumerated from its own
    source (AST), so adding a kind without a chip fails this file;
  * TTL semantics (pending/live have none);
  * the Qt chip painting, clearing, and showing even when the indicator is
    hidden by config;
  * the dictation.py wiring -- without a module-level `import dictation`.
"""
import ast
import os
import pathlib
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from samsara import session_modes as sm
from samsara.session_modes import (
    CHIP_ARROW,
    CHIP_CHECK,
    CHIP_CROSS,
    CHIP_ELLIPSIS,
    CHIP_KINDS,
    CHIP_TTL_MS,
    SessionMode,
    chip_ttl_ms,
    is_mapped_outcome,
    outcome_chip,
)

_SOURCE = pathlib.Path(sm.__file__)


def _dispatch_outcome_kinds() -> set:
    """Every kind= string DispatchOutcome is constructed with in session_modes.

    Walks the AST rather than grepping: StackItem also takes kind= and must
    not be mistaken for an outcome, and the scratch-that site uses a
    conditional expression (`"scratch_success" if ok else "scratch_refuse"`)
    that a regex would read as one kind.
    """
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))

    def strings(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            body, orelse = strings(node.body), strings(node.orelse)
            if body is None or orelse is None:
                return None
            return body + orelse
        return None

    kinds, dynamic = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "DispatchOutcome":
            for kw in node.keywords:
                if kw.arg == "kind":
                    found = strings(kw.value)
                    if found is None:
                        dynamic.append(node.lineno)
                    else:
                        kinds.update(found)
    assert not dynamic, (
        f"DispatchOutcome built with a non-literal kind at lines {dynamic}; "
        "this test can no longer prove every kind is mapped"
    )
    return kinds


# ---------------------------------------------------------------------------
# Coverage: every kind the source can produce is mapped
# ---------------------------------------------------------------------------

class TestEveryKindIsMapped:
    def test_enumeration_found_the_known_set(self):
        """Guards the enumerator itself -- a broken walker returning {} would
        make the coverage test pass vacuously."""
        kinds = _dispatch_outcome_kinds()
        assert len(kinds) >= 23
        assert {"command_miss", "mode_switch", "scratch_refuse",
                "dictate_commit_unavailable"} <= kinds

    @pytest.mark.parametrize("kind", sorted(_dispatch_outcome_kinds()))
    def test_kind_has_a_deliberate_chip(self, kind):
        assert is_mapped_outcome(kind), (
            f"DispatchOutcome kind {kind!r} has no chip -- add it to "
            "session_modes.outcome_chip"
        )

    def test_unknown_kind_is_visibly_flagged_not_hidden(self):
        assert outcome_chip("brand_new_kind") == ("? brand_new_kind", "warning")
        assert is_mapped_outcome("brand_new_kind") is False

    @pytest.mark.parametrize("kind", sorted(_dispatch_outcome_kinds()))
    def test_chip_kind_is_a_known_colour(self, kind):
        chip = outcome_chip(kind)
        if chip is not None:
            assert chip[1] in CHIP_KINDS


# ---------------------------------------------------------------------------
# Labels and colours, as specified
# ---------------------------------------------------------------------------

class TestLabels:
    def test_miss(self):
        assert outcome_chip("command_miss") == ("MISS", "error")

    @pytest.mark.parametrize("kind", ["command_executed", "hands_free_command_executed"])
    def test_executed_uses_the_first_two_words(self, kind):
        assert outcome_chip(kind, {"phrase": "switch window now"}) == (
            f"{CHIP_CHECK} switch window", "success")

    def test_executed_without_a_phrase(self):
        assert outcome_chip("command_executed", {}) == (CHIP_CHECK, "success")

    @pytest.mark.parametrize("mode,label", [
        (SessionMode.COMMAND, "COMMAND"),
        (SessionMode.DICTATE, "DICTATE"),
        (SessionMode.AVA, "AVA"),
    ])
    def test_mode_switch(self, mode, label):
        assert outcome_chip("mode_switch", {"mode": mode}) == (
            f"{CHIP_ARROW} {label}", "accent")

    @pytest.mark.parametrize("kind", [
        "ava_entry_failed", "hands_free_command_failed",
        "dictate_commit_failed", "prefix_switch_failed",
    ])
    def test_failures_use_the_detail_reason(self, kind):
        label, chip_kind = outcome_chip(kind, {"reason": "ollama down"})
        assert (label, chip_kind) == (f"{CHIP_CROSS} ollama down", "error")

    def test_failure_reason_prefers_error_key_too(self):
        label, _ = outcome_chip("dictate_commit_failed", {"error": "paste lost"})
        assert label == f"{CHIP_CROSS} paste lost"

    def test_failure_reason_is_capped_at_24_chars(self):
        label, _ = outcome_chip("ava_entry_failed",
                                {"reason": "the Ava plugin is disabled in settings"})
        reason = label[len(CHIP_CROSS) + 1:]
        assert len(reason) <= 24
        assert reason.endswith(CHIP_ELLIPSIS)

    def test_failure_without_detail_falls_back_to_the_kind(self):
        assert outcome_chip("prefix_switch_failed", {}) == (
            f"{CHIP_CROSS} prefix switch failed", "error")

    @pytest.mark.parametrize("kind", [
        "dictate_commit_blocked_focus_lock", "dictate_suppressed_focus_lock",
    ])
    def test_focus_lock_refusals(self, kind):
        assert outcome_chip(kind) == ("refused: focus lock", "warning")

    def test_blocked_command_explains_a_focus_lock(self):
        assert outcome_chip("hands_free_command_blocked",
                            {"commit_outcome": "dictate_commit_blocked_focus_lock"}) == (
            "refused: focus lock", "warning")

    @pytest.mark.parametrize("kind", ["dictate_commit_refused", "hands_free_command_refused"])
    def test_gate_refusals(self, kind):
        assert outcome_chip(kind) == ("refused: unclear", "warning")

    @pytest.mark.parametrize("kind", ["dictate_committed", "dictate_injected"])
    def test_typed(self, kind):
        assert outcome_chip(kind) == ("typed", "success")

    @pytest.mark.parametrize("kind", ["dictate_staged", "dictation_staged_chunk"])
    def test_staged_is_pending(self, kind):
        assert outcome_chip(kind) == ("staged", "pending")

    def test_undone(self):
        assert outcome_chip("scratch_success") == ("undone", "success")

    def test_ava_dispatched_is_pending(self):
        assert outcome_chip("ava_dispatched") == (f"Ava{CHIP_ELLIPSIS}", "pending")

    def test_ava_nothing_to_do(self):
        assert outcome_chip("ava_rejected_not_substantive") == (
            "Ava: nothing to do", "warning")

    @pytest.mark.parametrize("kind", ["empty", "abort", "dictation_chunk"])
    def test_no_chip(self, kind):
        assert outcome_chip(kind) is None

    def test_kinds_found_in_source_but_not_in_the_brief_are_mapped(self):
        assert outcome_chip("scratch_refuse") == ("refused: undo", "warning")
        assert outcome_chip("dictate_commit_unavailable") == (
            "refused: nothing staged", "warning")

    def test_correction_unavailable_is_deliberate(self):
        assert outcome_chip("dictate_correction_unavailable") == (
            "nothing to correct", "warning")

    def test_non_dict_detail_is_tolerated(self):
        assert outcome_chip("command_executed", None) == (CHIP_CHECK, "success")


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

class TestTtl:
    @pytest.mark.parametrize("chip_kind", ["pending", "live"])
    def test_pending_and_live_have_no_ttl(self, chip_kind):
        assert chip_ttl_ms("dictate_staged", chip_kind) is None

    def test_default_ttl(self):
        assert chip_ttl_ms("command_miss", "error") == CHIP_TTL_MS == 1800

    @pytest.mark.parametrize("kind", ["dictate_committed", "dictate_injected"])
    def test_typed_is_brief(self, kind):
        assert chip_ttl_ms(kind, "success") == 900


# ---------------------------------------------------------------------------
# Qt: painting, clearing, hidden-by-config
# ---------------------------------------------------------------------------

def _pump(qapp, ms=60):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


@pytest.fixture
def indicator(qapp):
    from samsara.ui.listening_indicator import ListeningIndicator

    widget = ListeningIndicator()
    widget.set_position("bottom-center")
    yield widget
    widget.destroy()


class TestChipRendering:
    def test_show_outcome_paints_the_chip_in_its_colour(self, qapp, indicator):
        from samsara.ui import theme
        from PySide6.QtGui import QColor

        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        _pump(qapp)

        _, _, _, chip_rect = indicator._layout()
        assert chip_rect is not None
        image = indicator.grab().toImage()
        # Sample just inside the chip's left edge, clear of the label text.
        px = image.pixelColor(int(chip_rect.x() + 4), int(chip_rect.center().y()))
        expected = QColor(theme.ERROR)
        assert abs(px.red() - expected.red()) < 30
        assert abs(px.green() - expected.green()) < 30
        assert abs(px.blue() - expected.blue()) < 30

    def test_chip_grows_the_widget_and_clears_on_ttl(self, qapp, indicator):
        indicator.show()
        _pump(qapp)
        base_h = indicator.height()

        # TTL well above the "is it showing" pump so the chip cannot expire
        # before that assertion runs.
        indicator.show_outcome("MISS", "error", ttl_ms=300)
        _pump(qapp, 20)
        assert indicator.height() > base_h

        _pump(qapp, 600)
        assert indicator._active_chip() is None
        assert indicator.height() == base_h

    def test_new_call_replaces_the_current_chip(self, qapp, indicator):
        indicator.show()
        indicator.show_outcome("staged", "pending", ttl_ms=None)
        indicator.show_outcome("typed", "success", ttl_ms=None)
        assert indicator._active_chip() == ("typed", "success")

    def test_pending_has_no_ttl(self, qapp, indicator):
        indicator.show()
        indicator.show_outcome("staged", "pending", ttl_ms=40)
        _pump(qapp, 200)
        assert indicator._active_chip() == ("staged", "pending")

    def test_font_is_at_least_13px(self, indicator):
        assert indicator._chip_font().pixelSize() >= 13

    def test_unknown_kind_renders_as_warning(self, indicator):
        indicator.show()
        indicator.show_outcome("? x", "not-a-kind", ttl_ms=None)
        assert indicator._active_chip() == ("? x", "warning")

    def test_empty_label_clears(self, indicator):
        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        indicator.show_outcome("", "error")
        assert indicator._active_chip() is None

    def test_never_takes_focus(self, indicator):
        from PySide6.QtCore import Qt

        flags = indicator.windowFlags()
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
        assert indicator.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


class TestHiddenByConfig:
    """listening_indicator_enabled False: dictation.py never shows the pill,
    but the chip must still appear -- for its TTL only -- then hide."""

    def test_chip_shows_alone_when_the_indicator_is_hidden(self, qapp, indicator):
        assert not indicator.isVisible()

        indicator.show_outcome("MISS", "error", ttl_ms=None)
        _pump(qapp)

        assert indicator.isVisible()
        assert indicator._chip_only is True
        _, _, pill_rect, chip_rect = indicator._layout()
        assert pill_rect is None, "the pill body must not draw when disabled"
        assert chip_rect is not None

    def test_hidden_chip_hides_again_after_its_ttl(self, qapp, indicator):
        indicator.show_outcome("MISS", "error", ttl_ms=300)
        _pump(qapp, 20)
        assert indicator.isVisible()

        _pump(qapp, 600)
        assert not indicator.isVisible()
        assert indicator._chip_only is False

    def test_explicit_show_leaves_chip_only_mode(self, qapp, indicator):
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        indicator.show()
        _, _, pill_rect, chip_rect = indicator._layout()
        assert pill_rect is not None and chip_rect is not None


class TestDeviceLost:
    def test_mic_lost_chip_is_persistent_and_wins(self, qapp, indicator):
        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=40)
        indicator.set_device_lost(True)
        _pump(qapp, 250)

        assert indicator._active_chip() == ("mic lost", "error")

    def test_mic_lost_survives_hide(self, qapp, indicator):
        indicator.show()
        indicator.set_device_lost(True)
        indicator.hide()
        _pump(qapp)

        assert indicator.isVisible()
        assert indicator._active_chip() == ("mic lost", "error")

    def test_clearing_mic_lost_hides_a_chip_only_widget(self, qapp, indicator):
        indicator.set_device_lost(True)
        assert indicator.isVisible()
        indicator.set_device_lost(False)
        assert not indicator.isVisible()


# ---------------------------------------------------------------------------
# dictation.py wiring -- real methods bound onto a stub
# ---------------------------------------------------------------------------

class _FakeIndicator:
    def __init__(self):
        self.calls = []

    def show_outcome(self, label, kind, ttl_ms=None):
        self.calls.append(("show", label, kind, ttl_ms))

    def clear_outcome(self):
        self.calls.append(("clear",))


def _app():
    """DictationApp stub carrying the REAL chip methods; dictation is imported
    here, never at module level."""
    import dictation as _d

    class _Stub:
        _show_outcome_chip = _d.DictationApp._show_outcome_chip
        _show_dispatch_outcome_chip = _d.DictationApp._show_dispatch_outcome_chip
        _show_hold_pending_chip = _d.DictationApp._show_hold_pending_chip
        _resolve_hold_chip = _d.DictationApp._resolve_hold_chip

        def __init__(self):
            self.listening_indicator = _FakeIndicator()

        def _schedule_ui(self, func, *args):
            func(*args)

    return _Stub()


class TestWiring:
    def test_dispatch_outcome_reaches_the_indicator(self):
        app = _app()
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="command_miss"))
        assert app.listening_indicator.calls == [("show", "MISS", "error", 1800)]

    def test_pending_outcome_is_scheduled_without_a_ttl(self):
        app = _app()
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="dictate_staged"))
        assert app.listening_indicator.calls == [("show", "staged", "pending", None)]

    def test_no_chip_kind_schedules_nothing(self):
        app = _app()
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="empty"))
        assert app.listening_indicator.calls == []

    def test_unmapped_kind_logs_a_warning(self, caplog):
        import logging

        app = _app()
        with caplog.at_level(logging.WARNING):
            app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="brand_new_kind"))

        assert any("brand_new_kind" in r.getMessage() and r.levelno == logging.WARNING
                   for r in caplog.records)
        assert app.listening_indicator.calls[0][1] == "? brand_new_kind"

    def test_hold_pending_chip_is_cleared_when_nothing_resolves_it(self):
        """The watchdog: a hold exit that shows no chip must not leave "..."."""
        app = _app()
        seq = app._show_hold_pending_chip()
        app._resolve_hold_chip(seq)
        assert app.listening_indicator.calls == [
            ("show", CHIP_ELLIPSIS, "pending", None), ("clear",)]

    def test_hold_pending_chip_is_left_alone_once_resolved(self):
        app = _app()
        seq = app._show_hold_pending_chip()
        app._show_outcome_chip("typed", "success", 900)
        app._resolve_hold_chip(seq)
        assert ("clear",) not in app.listening_indicator.calls

    def test_no_indicator_is_harmless(self):
        app = _app()
        app.listening_indicator = None
        app._show_outcome_chip("MISS", "error")
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(kind="command_miss"))


class TestTestButtonVolume:
    """play_sound(volume=) plays at the given level and never touches config."""

    def _sound_app(self, saved=0.5):
        import threading
        import numpy as np
        import dictation as _d

        class _Stub:
            play_sound = _d.DictationApp.play_sound

            def __init__(self):
                self.config = {"sound_volume": saved, "audio_feedback": True}
                self._sound_cache = {"success": np.ones(4, dtype=np.float32)}
                self._buffer_lock = threading.Lock()
                self._playback_buffer = None
                self.audio_coordinator = None

        return _Stub()

    def test_volume_override_scales_this_playback(self):
        app = self._sound_app(saved=0.5)
        app.play_sound("success", volume=0.2)
        assert float(app._playback_buffer[0]) == pytest.approx(0.2)

    def test_override_does_not_change_saved_config(self):
        app = self._sound_app(saved=0.5)
        app.play_sound("success", volume=0.9)
        assert app.config["sound_volume"] == 0.5

    def test_no_override_uses_saved_volume(self):
        app = self._sound_app(saved=0.5)
        app.play_sound("success")
        assert float(app._playback_buffer[0]) == pytest.approx(0.5)

    def test_zero_override_is_silent(self):
        app = self._sound_app(saved=0.5)
        app.play_sound("success", volume=0.0)
        assert app._playback_buffer is None

    def test_out_of_range_override_is_clamped(self):
        app = self._sound_app(saved=0.5)
        app.play_sound("success", volume=7)
        assert float(app._playback_buffer[0]) == pytest.approx(1.0)
