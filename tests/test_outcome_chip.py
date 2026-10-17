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
import pathlib
import time
import types

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


_INTERACT_SOURCE = _SOURCE.with_name("interact.py")
_CORRECTIONS_SOURCE = _SOURCE.with_name("session_corrections.py")


def _literal_kinds(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        body, orelse = _literal_kinds(node.body), _literal_kinds(node.orelse)
        if body is not None and orelse is not None:
            return body | orelse
    return None


def _interact_reply_kinds() -> set:
    """Read the closed producer set; a computed reply kind fails closed too."""
    tree = ast.parse(_INTERACT_SOURCE.read_text(encoding="utf-8"))
    kinds, dynamic = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "InteractReply":
            value = node.args[0] if node.args else next(
                (kw.value for kw in node.keywords if kw.arg == "kind"), None)
            found = _literal_kinds(value)
            if found is None:
                dynamic.append(node.lineno)
            else:
                kinds.update(found)
    assert not dynamic, f"InteractReply built with a non-literal kind at lines {dynamic}"
    assert kinds, "No InteractReply literal kinds found"
    return kinds


def _dispatch_outcome_kinds() -> set:
    """Enumerate literal outcomes and the source-derived controller reply set.

    Only the verified controller.handle -> reply.kind seam is resolved.
    Any other computed kind still fails the original collection guard.
    """
    trees = []
    for source in (_SOURCE, _CORRECTIONS_SOURCE):
        assert source.is_file(), f"Missing outcome producer: {source}"
        trees.append(ast.parse(source.read_text(encoding="utf-8")))
    tree = trees[0]  # Interact binding analysis belongs to the facade only.
    reply_values = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name != "_dispatch_utterance_locked":
            continue
        assignments = [node for node in ast.walk(fn) if isinstance(node, ast.Assign)]
        def binding(name):
            matches = [node.value for node in assignments if any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets)]
            return matches[0] if len(matches) == 1 else None
        controller, reply = binding("controller"), binding("reply")
        if (isinstance(controller, ast.Attribute) and controller.attr == "_interact_controller"
                and isinstance(controller.value, ast.Name) and controller.value.id == "self"
                and isinstance(reply, ast.Call) and isinstance(reply.func, ast.Attribute)
                and reply.func.attr == "handle" and isinstance(reply.func.value, ast.Name)
                and reply.func.value.id == "controller"):
            reply_values.update(node for node in ast.walk(fn)
                                if isinstance(node, ast.Attribute) and node.attr == "kind"
                                and isinstance(node.value, ast.Name) and node.value.id == "reply")

    kinds, dynamic = set(), []
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "DispatchOutcome":
                value = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == "kind"), None)
                found = _literal_kinds(value)
                if value in reply_values:
                    found = _interact_reply_kinds()
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

    def test_controller_reply_kinds_are_included(self):
        assert _interact_reply_kinds() <= _dispatch_outcome_kinds()

    @pytest.mark.parametrize("kind", sorted(_interact_reply_kinds()))
    def test_controller_chips_preserve_existing_fallback(self, kind):
        assert outcome_chip(kind, {"message": "controller feedback"}) == (f"? {kind}", "warning")
        assert is_mapped_outcome(kind)
        assert chip_ttl_ms(kind, "warning") == CHIP_TTL_MS

    @pytest.mark.parametrize("expression", ["computed", "other.kind", "reply.other", "reply.kind"])
    def test_other_computed_outcomes_still_fail(self, tmp_path, monkeypatch, expression):
        source = tmp_path / "session_modes.py"
        source.write_text(_SOURCE.read_text(encoding="utf-8") +
                          f"\ndef unrelated():\n    return DispatchOutcome(kind={expression})\n",
                          encoding="utf-8")
        monkeypatch.setattr(__import__(__name__, fromlist=["_SOURCE"]), "_SOURCE", source)
        with pytest.raises(AssertionError, match="DispatchOutcome built with a non-literal kind"):
            _dispatch_outcome_kinds()

    def test_changed_reply_binding_still_fails(self, tmp_path, monkeypatch):
        source = tmp_path / "session_modes.py"
        original = _SOURCE.read_text(encoding="utf-8")
        changed = original.replace("reply = controller.handle(", "reply = other.handle(")
        assert changed != original
        source.write_text(changed, encoding="utf-8")
        monkeypatch.setattr(__import__(__name__, fromlist=["_SOURCE"]), "_SOURCE", source)
        with pytest.raises(AssertionError, match="DispatchOutcome built with a non-literal kind"):
            _dispatch_outcome_kinds()

    def test_reply_producer_changes_are_read_from_source(self, tmp_path, monkeypatch):
        source = tmp_path / "interact.py"
        source.write_text('InteractReply("new_literal")\nInteractReply(kind="keyword_literal")\n',
                          encoding="utf-8")
        monkeypatch.setattr(__import__(__name__, fromlist=["_INTERACT_SOURCE"]), "_INTERACT_SOURCE", source)
        assert {"new_literal", "keyword_literal"} <= _dispatch_outcome_kinds()
        source.write_text('InteractReply("known")\nInteractReply(kind=computed)\n', encoding="utf-8")
        with pytest.raises(AssertionError, match="InteractReply built with a non-literal kind"):
            _dispatch_outcome_kinds()

    def test_moved_producer_changes_are_read_from_source(self, tmp_path, monkeypatch):
        source = tmp_path / "session_corrections.py"
        source.write_text('DispatchOutcome(kind="new_correction_literal")\n', encoding="utf-8")
        monkeypatch.setattr(__import__(__name__, fromlist=["_CORRECTIONS_SOURCE"]),
                            "_CORRECTIONS_SOURCE", source)
        assert "new_correction_literal" in _dispatch_outcome_kinds()
        source.write_text('DispatchOutcome(kind=computed)\n', encoding="utf-8")
        with pytest.raises(AssertionError, match="DispatchOutcome built with a non-literal kind"):
            _dispatch_outcome_kinds()

    def test_missing_producer_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(__import__(__name__, fromlist=["_CORRECTIONS_SOURCE"]),
                            "_CORRECTIONS_SOURCE", tmp_path / "missing.py")
        with pytest.raises(AssertionError, match="Missing outcome producer"):
            _dispatch_outcome_kinds()

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
    def test_miss_shows_what_was_heard(self):
        assert outcome_chip("command_miss", {"heard": "switch windy"}) == (
            "heard: switch windy — not a command", "error")

    @pytest.mark.parametrize("kind", ["command_executed", "hands_free_command_executed"])
    def test_executed_shows_heard_and_canonical_phrase(self, kind):
        assert outcome_chip(kind, {"heard": "switch windy", "phrase": "switch window now"}) == (
            "heard: switch windy — ran switch window", "success")

    def test_suggestion_stays_up_for_its_eight_second_reply_window(self):
        assert outcome_chip("command_suggestion_pending", {
            "heard": "switch windy", "phrase": "switch window",
        }) == ("heard: switch windy — did you mean switch window? say yes or no", "warning")
        assert chip_ttl_ms("command_suggestion_pending", "warning") == 8000

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

    def test_failure_error_key_wins_when_both_values_are_present(self):
        label, _ = outcome_chip("dictate_commit_failed", {
            "reason": "generic failure", "error": "paste lost",
        })
        assert label == f"{CHIP_CROSS} paste lost"

    def test_dictate_commit_failure_without_detail_uses_clipboard_fallback(self):
        assert outcome_chip("dictate_commit_failed", {}) == (
            f"{CHIP_CROSS} Paste blocked - clipboard held by another app", "error")

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
        assert outcome_chip("command_executed", None) == (
            "heard: (nothing) — ran that", "success")


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

class TestTtl:
    @pytest.mark.parametrize("chip_kind", ["pending", "live"])
    def test_pending_and_live_have_no_ttl(self, chip_kind):
        assert chip_ttl_ms("dictate_staged", chip_kind) is None

    def test_default_ttl(self):
        assert chip_ttl_ms("command_miss", "error") == 4000

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
        from PySide6.QtGui import QColor, QImage

        indicator.show()
        indicator.show_outcome("MISS", "error", ttl_ms=None)
        _pump(qapp)

        _, _, _, chip_rect = indicator._layout()
        assert chip_rect is not None
        image = QImage(indicator.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        indicator.render(image)
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

        _pump(qapp, 4200)
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

        _pump(qapp, 4200)
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
        app._show_dispatch_outcome_chip(sm.DispatchOutcome(
            kind="command_miss", detail={"heard": "switch windy"}))
        assert app.listening_indicator.calls == [(
            "show", "heard: switch windy — not a command", "error", 4000)]

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
            _resolve_playback_output = _d.DictationApp._resolve_playback_output
            stop_sound_stream = lambda self: None
            _start_sound_stream = lambda self: None

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
