"""Tests for samsara.ui.quick_reference_qt: the live-config Quick Reference
window (v0.21 release gate).

Headless-safe, no window.show() assertions beyond construction -- matching
the established precedent in test_main_window_qt.py: qt_runtime.post() is
monkeypatched to capture what gets posted rather than exercising the real
background-thread event loop (qt_runtime.ensure_started() can only run
once per process and isn't restartable), and widgets that DO need real Qt
behavior are constructed directly against the session-scoped `qapp`
fixture (tests/conftest.py) in-process.

HARD RULE under test: every value the window shows must come from a fresh
resolver call against the live app.config -- never a cached/hardcoded
value. Each resolver test below mutates config and re-calls the resolver
(or the window's refresh()) to prove that, rather than only checking one
static snapshot.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QLabel

from samsara.ui import quick_reference_qt as qr
from samsara import session_modes
from samsara.session_modes import SessionMode


def _make_app(config=None):
    return types.SimpleNamespace(config=config if config is not None else {})


def _all_label_texts(widget) -> list[str]:
    return [lbl.text() for lbl in widget.findChildren(QLabel)]


# ============================================================================
# Resolvers -- live config in, current values out, reflect changes on re-call
# ============================================================================

class TestResolveHotkeys:
    def test_defaults_when_config_empty(self):
        rows = qr._resolve_hotkeys(_make_app())
        by_label = {r["label"]: r for r in rows}
        assert by_label["Dictate (hold to talk)"]["value"] == "Ctrl+Shift"
        assert by_label["Undo last dictation"]["value"] == "Ctrl+Alt+Z"
        assert by_label["Paste staged thought"]["value"] == "Ctrl+Space"
        assert by_label["Paste staged thought"]["enabled"] is False
        # command_mode.enabled defaults False -- Command Mode/Ava rows dim
        assert by_label["Command Mode (hold)"]["enabled"] is False
        assert by_label["Ava"]["enabled"] is False

    def test_reflects_changed_hotkey_on_recall(self):
        app = _make_app({"hotkey": "ctrl+alt+q"})
        rows = qr._resolve_hotkeys(app)
        by_label = {r["label"]: r for r in rows}
        assert by_label["Dictate (hold to talk)"]["value"] == "Ctrl+Alt+Q"

        app.config["hotkey"] = "ctrl+space"
        rows2 = qr._resolve_hotkeys(app)
        by_label2 = {r["label"]: r for r in rows2}
        assert by_label2["Dictate (hold to talk)"]["value"] == "Ctrl+Space"

    def test_command_mode_enabled_reflected(self):
        app = _make_app({"command_mode": {"enabled": True, "button": "mouse4", "mode": "toggle"}})
        rows = qr._resolve_hotkeys(app)
        by_label = {r["label"]: r for r in rows}
        assert by_label["Hands-free Session"]["enabled"] is True
        assert by_label["Hands-free Session"]["value"] == "Mouse 4"
        assert by_label["Paste staged thought"]["enabled"] is True
        assert by_label["Paste staged thought"]["value"] == "Ctrl+Space"
        assert by_label["Ava"]["enabled"] is True

    def test_streaming_enabled_reflected(self):
        app = _make_app({"streaming_mode": True, "streaming_hotkey": "capslock"})
        rows = qr._resolve_hotkeys(app)
        by_label = {r["label"]: r for r in rows}
        assert by_label["Streaming (live partials)"]["enabled"] is True
        # Guidance audit row 170: the key is not configurable -- the hook hardcodes
        # CapsLock and the row says so, whatever streaming_hotkey holds.
        assert by_label["Streaming (live partials)"]["value"].startswith(qr.STREAMING_KEY_LABEL)
        app.config["streaming_hotkey"] = "f9"
        rows2 = qr._resolve_hotkeys(app)
        assert {r["label"]: r for r in rows2}["Streaming (live partials)"]["value"].startswith("CapsLock (fixed)")


class TestResolveSessionPhrases:
    def test_defaults_when_config_empty(self):
        state = qr._resolve_session_phrases(_make_app())
        assert state["wake"]["phrase"] == "jarvis"
        assert state["wake"]["enabled"] is False
        assert "over" in state["send_word"]["words"]
        assert state["lane_switches"]["enabled"] is False
        assert state["dictate_commit"]["phrase"] == "end"
        assert state["scratch_that"]["phrase"] == "scratch that"

    def test_reflects_changed_wake_phrase_on_recall(self):
        app = _make_app({"wake_word_enabled": True,
                          "wake_word_config": {"phrase": "computer", "phrase_options": ["computer"]}})
        state = qr._resolve_session_phrases(app)
        assert state["wake"]["phrase"] == "computer"
        assert state["wake"]["enabled"] is True
        assert state["wake"]["alternates"] == []

        app.config["wake_word_config"]["phrase"] = "hey samsa"
        state2 = qr._resolve_session_phrases(app)
        assert state2["wake"]["phrase"] == "hey samsa"

    def test_lane_switch_phrases_from_session_modes_registry(self):
        state = qr._resolve_session_phrases(_make_app())
        assert "command mode" in state["lane_switches"]["phrases"]["Command"]
        assert "dictate mode" in state["lane_switches"]["phrases"]["Dictate"]
        # Ava moved OUT of the static registry (2026-07-18, match_ava_invocation)
        # -- default invocations, NOT bare "ava" (deliberately excluded, see
        # session_modes.py). Config-empty app -> module default list.
        # ... plus the whole-utterance "ava mode" switch word (2026-09-11) from the
        # static registry -- guidance audit row 165.
        assert state["lane_switches"]["phrases"]["Ava"] == sorted(["hey ava", "so ava", "oracle", "ava mode"])
        assert "ava" not in state["lane_switches"]["phrases"]["Ava"]

    def test_ava_invocation_phrases_reflect_custom_config_on_recall(self):
        """HARD RULE (module docstring): must read live config, not the
        module default, once the user has configured their own list."""
        app = _make_app({"ava_invocations": ["computer"]})
        state = qr._resolve_session_phrases(app)
        assert state["lane_switches"]["phrases"]["Ava"] == sorted(["computer", "ava mode"])

        app.config["ava_invocations"] = ["jarvis", "hey ava"]
        state2 = qr._resolve_session_phrases(app)
        assert state2["lane_switches"]["phrases"]["Ava"] == sorted(["jarvis", "hey ava", "ava mode"])

    def test_abort_enabled_if_either_wake_or_command_mode_on(self):
        assert qr._resolve_session_phrases(
            _make_app({"wake_word_enabled": True}))["abort"]["enabled"] is True
        assert qr._resolve_session_phrases(
            _make_app({"command_mode": {"enabled": True}}))["abort"]["enabled"] is True
        assert qr._resolve_session_phrases(_make_app())["abort"]["enabled"] is False

    def test_custom_cancel_words_reflected(self):
        app = _make_app({"wake_word_enabled": True,
                          "wake_word_config": {"wake_abort_phrase": ["stop that", "nevermind"]}})
        state = qr._resolve_session_phrases(app)
        # Sleep phrases moved to their own row (they keep the draft); the exit
        # list is wake abort words + command_mode.abort_phrases + the exit words.
        assert state["abort"]["words"] == [
            "stop that", "nevermind",
            "stop listening", "exit hands free", "exit command mode",
        ]
        assert state["sleep"]["words"] == list(session_modes.SESSION_SLEEP_PHRASES)
        assert state["stop"]["words"] == list(session_modes.SESSION_STOP_PHRASES)

    def test_hands_free_toggle_data_reads_from_config(self):
        app = _make_app({
            "command_mode": {"enabled": True, "button": "rctrl", "mode": "toggle"},
        })
        state = qr._resolve_session_phrases(app)
        hf = state["hands_free_toggle"]
        assert hf["enabled"] is True
        assert "Right Ctrl" in hf["button"]
        assert hf["mode"] == "toggle"


class TestResolveModesOverview:
    def test_disabled_by_default(self):
        state = qr._resolve_modes_overview(_make_app())
        assert state["enabled"] is False
        names = [m["name"] for m in state["modes"]]
        assert names == ["COMMAND", "HANDS FREE", "AVA"]

    def test_enabled_when_command_mode_on(self):
        state = qr._resolve_modes_overview(_make_app({"command_mode": {"enabled": True}}))
        assert state["enabled"] is True


class TestResolveFormattingTokens:
    def test_enabled_by_default_with_four_tokens(self):
        state = qr._resolve_formatting_tokens(_make_app())
        assert state["enabled"] is True
        phrases = " ".join(t["phrase"] for t in state["tokens"])
        assert "new line" in phrases
        assert "new paragraph" in phrases
        assert "bullet" in phrases
        assert "insert tab" in phrases
        assert len(state["tokens"]) == 4

    def test_disabled_reflected(self):
        app = _make_app({"formatting_tokens": {"enabled": False}})
        state = qr._resolve_formatting_tokens(app)
        assert state["enabled"] is False

        app.config["formatting_tokens"]["enabled"] = True
        state2 = qr._resolve_formatting_tokens(app)
        assert state2["enabled"] is True


# ============================================================================
# Public wrapper -- qt_runtime.post() discipline (monkeypatched, no real
# background thread -- matching test_main_window_qt.py's precedent)
# ============================================================================

class TestQuickReferenceQtShow:
    def test_show_posts_init_window_on_first_call(self, monkeypatch):
        qref = qr.QuickReferenceQt(_make_app())
        posted = []
        monkeypatch.setattr(qr.qt_runtime, "post", posted.append)

        qref.show()

        assert posted == [qref._init_window]
        assert qref._init_posted is True

    def test_show_does_not_double_post_init_on_repeated_calls(self, monkeypatch):
        qref = qr.QuickReferenceQt(_make_app())
        posted = []
        monkeypatch.setattr(qr.qt_runtime, "post", posted.append)

        qref.show()
        qref.show()

        assert posted == [qref._init_window]

    def test_show_refreshes_and_reshows_existing_window(self, monkeypatch):
        qref = qr.QuickReferenceQt(_make_app())
        # Sentinel with the four methods show() posts -- plain object()
        # (matching test_main_window_qt.py's precedent) isn't enough here
        # since this show() also posts refresh(), unlike MainWindowQt's.
        qref._window = types.SimpleNamespace(
            refresh=lambda: None, show=lambda: None,
            raise_=lambda: None, activateWindow=lambda: None,
        )
        posted = []
        monkeypatch.setattr(qr.qt_runtime, "post", posted.append)

        qref.show()

        assert posted == [
            qref._window.refresh, qref._window.show,
            qref._window.raise_, qref._window.activateWindow,
        ]


# ============================================================================
# Window construction + rendering (real widgets, in-process against `qapp`)
# ============================================================================

class TestWindowConstruction:
    def test_constructs_without_error(self, qapp):
        win = qr._QuickReferenceWindow(_make_app())
        try:
            assert win.windowTitle() == "Quick Reference"
            assert win.centralWidget() is not None
        finally:
            win.close()

    def test_close_hides_instead_of_destroying(self, qapp):
        win = qr._QuickReferenceWindow(_make_app())
        try:
            win.show()
            qapp.processEvents()
            win.close()
            qapp.processEvents()
            assert not win.isVisible()
            # still a live object -- closeEvent ignored the close, just hid it
            assert win.windowTitle() == "Quick Reference"
        finally:
            win.hide()


class TestDisabledStateRendering:
    def test_disabled_sections_show_dimmed_marker(self, qapp):
        """command_mode.enabled defaults False -- Command Mode/Ava hotkey
        rows, the whole Voice Session lane-switch rows, and Modes at a
        Glance must all render the '(disabled)' marker, not vanish."""
        win = qr._QuickReferenceWindow(_make_app())
        try:
            texts = _all_label_texts(win)
            disabled_count = sum(1 for t in texts if "(disabled)" in t)
            assert disabled_count > 0
            # the section headings AND the disabled rows' labels themselves
            # must still be present -- the HARD RULE is dim, not vanish
            assert "Dictation Hotkeys" in texts
            assert "Voice Session" in texts
            assert "Modes at a Glance" in texts
            assert "Command Mode (hold)" in texts
            assert any("Right Ctrl" in t and "(disabled)" in t for t in texts)
        finally:
            win.close()

    def test_enabled_feature_has_no_disabled_marker(self, qapp):
        """formatting_tokens.enabled defaults True -- its rows must NOT
        carry the disabled marker."""
        win = qr._QuickReferenceWindow(_make_app())
        try:
            texts = _all_label_texts(win)
            formatting_value_texts = [t for t in texts if "break" in t or "character" in t or "bullet point" == t.strip()]
            assert formatting_value_texts
            assert not any("(disabled)" in t for t in formatting_value_texts)
        finally:
            win.close()

    def test_all_disabled_states_clear_when_features_enabled(self, qapp):
        app = _make_app({
            "command_mode": {"enabled": True, "button": "rctrl", "mode": "toggle"},
            "wake_word_enabled": True,
            "streaming_mode": True,
            "continuous_commit_trigger": "key",
        })
        win = qr._QuickReferenceWindow(app)
        try:
            texts = _all_label_texts(win)
            assert not any("(disabled)" in t for t in texts)
        finally:
            win.close()


class TestRefreshUpdatesLabelText:
    def test_changing_hotkey_then_refresh_updates_label(self, qapp):
        app = _make_app({"hotkey": "ctrl+shift"})
        win = qr._QuickReferenceWindow(app)
        try:
            assert "Ctrl+Shift" in _all_label_texts(win)
            assert "Ctrl+Alt+Q" not in _all_label_texts(win)

            app.config["hotkey"] = "ctrl+alt+q"
            win.refresh()

            assert "Ctrl+Alt+Q" in _all_label_texts(win)
            assert "Ctrl+Shift" not in _all_label_texts(win)
        finally:
            win.close()

    def test_changing_wake_phrase_then_refresh_updates_label(self, qapp):
        app = _make_app({"wake_word_enabled": True,
                          "wake_word_config": {"phrase": "jarvis", "phrase_options": ["jarvis"]}})
        win = qr._QuickReferenceWindow(app)
        try:
            texts = _all_label_texts(win)
            assert any("jarvis" in t for t in texts)

            app.config["wake_word_config"]["phrase"] = "computer"
            win.refresh()

            texts2 = _all_label_texts(win)
            assert any(t.startswith("computer") for t in texts2)
            assert not any(t.startswith("jarvis") for t in texts2)
        finally:
            win.close()

    def test_disabling_command_mode_then_refresh_adds_disabled_marker(self, qapp):
        app = _make_app({"command_mode": {"enabled": True, "button": "rctrl", "mode": "hold"}})
        win = qr._QuickReferenceWindow(app)
        try:
            texts = _all_label_texts(win)
            assert not any("(disabled)" in t for t in texts if "Right Ctrl" in t)

            app.config["command_mode"]["enabled"] = False
            win.refresh()

            texts2 = _all_label_texts(win)
            assert any("(disabled)" in t and "Right Ctrl" in t for t in texts2)
        finally:
            win.close()

    def test_hands_free_section_reflects_live_config_in_rendered_text(self, qapp):
        app = _make_app({
            "command_mode": {"enabled": True, "button": "rctrl", "mode": "toggle"},
            "ava_invocations": ["hey ava"],
            "wake_word_enabled": True,
            "wake_word_config": {"end_words": ["over", "send"]},
        })
        win = qr._QuickReferenceWindow(app)
        try:
            texts = _all_label_texts(win)
            assert any("Right Ctrl" in t and "latches a session" in t for t in texts)
            assert any("send" in t and "over" in t for t in texts)
            assert any("hey ava" in t for t in texts)

            app.config["command_mode"]["button"] = "mouse4"
            app.config["ava_invocations"] = ["echo"]
            app.config["wake_word_config"]["end_words"] = ["done"]
            win.refresh()

            texts2 = _all_label_texts(win)
            assert any("Mouse 4" in t and "latches a session" in t for t in texts2)
            assert not any("over, send" in t for t in texts2)
            assert any("done" in t for t in texts2)
            assert any("echo" in t for t in texts2)
        finally:
            win.close()


# ============================================================================
# Guidance audit 2026-09-13 fixes (rows 148, 170, 200, 201, 202, 250, 489/516)
# ============================================================================

class TestGuidanceAuditFixes:
    def test_dictate_label_follows_mode(self):
        for mode, label in qr._DICTATE_MODE_LABELS.items():
            rows = qr._resolve_hotkeys(_make_app({"mode": mode}))
            assert any(r["label"] == label for r in rows), mode
        assert qr._DICTATE_MODE_LABELS["hold"] == "Dictate (hold to talk)"
        assert "hold" not in qr._DICTATE_MODE_LABELS["toggle"].lower()
        assert "hold" not in qr._DICTATE_MODE_LABELS["continuous"].lower()

    def test_every_hotkey_the_app_reads_has_a_row_read_from_config(self):
        cfg = {"continuous_hotkey": "ctrl+alt+1", "wake_word_hotkey": "ctrl+alt+2",
               "command_hotkey": "ctrl+alt+3", "cancel_hotkey": "f4", "memo_hotkey": "ctrl+alt+5",
               "correction_hotkey": "ctrl+alt+6", "hotkeys": {"capture_correction": "ctrl+alt+7"},
               "continuous_commit_hotkey": "ctrl+alt+8", "continuous_commit_trigger": "key"}
        by_label = {r["label"]: r for r in qr._resolve_hotkeys(_make_app(cfg))}
        assert by_label["Continuous mode (toggle)"]["value"] == "Ctrl+Alt+1"
        assert by_label["Wake word listener (toggle)"]["value"] == "Ctrl+Alt+2"
        assert by_label["Command only (hold)"]["value"] == "Ctrl+Alt+3"
        assert by_label["Cancel recording"]["value"] == "F4"
        assert by_label["Voice memo"]["value"] == "Ctrl+Alt+5"
        assert by_label["Correction report"]["value"] == "Ctrl+Alt+6"
        assert by_label["Correction capture"]["value"] == "Ctrl+Alt+7"
        assert by_label["Continuous commit"]["value"].startswith("Ctrl+Alt+8") and by_label["Continuous commit"]["enabled"]
        assert {r["label"]: r for r in qr._resolve_hotkeys(_make_app({}))}["Continuous commit"]["enabled"] is False

    def test_abort_merges_command_mode_abort_phrases(self):
        app = _make_app({"command_mode": {"enabled": True, "abort_phrases": ["that will do"]}})
        state = qr._resolve_session_phrases(app)
        assert "that will do" in state["abort"]["words"]
        app.config["command_mode"]["abort_phrases"] = ["enough"]
        assert "enough" in qr._resolve_session_phrases(app)["abort"]["words"]

    def test_commit_row_mentions_the_and_homophone(self):
        state = qr._resolve_session_phrases(_make_app({"command_mode": {"enabled": True}}))
        extra = sorted(session_modes._DICTATE_COMMIT_HOMOPHONES - {session_modes.DICTATE_COMMIT_PHRASE})
        assert state["dictate_commit"]["homophones"] == extra
        value = qr._commit_value(state["dictate_commit"])
        assert value.startswith(session_modes.DICTATE_COMMIT_PHRASE)
        assert all(f'"{w}"' in value for w in extra)
        overview = qr._resolve_modes_overview(_make_app({"command_mode": {"enabled": True}}))
        dictate = next(m for m in overview["modes"] if m["name"] == "HANDS FREE")
        assert all(f'"{w}"' in dictate["description"] for w in extra)

    def test_prefix_literal_pause_resume_and_opens_session(self):
        app = _make_app({"command_mode": {"enabled": True}, "wake_word_enabled": True,
                         "wake_word_config": {"opens_session": True, "pause_words": ["hang on"],
                                              "resume_words": ["carry on"]}})
        state = qr._resolve_session_phrases(app)
        assert state["prefix_switch"]["words"] == sorted(session_modes._PREFIX_SWITCHES)
        assert state["literal"]["word"] == "literal"
        assert state["wake"]["opens_session"] is True
        assert state["wake"]["pause_words"] == ["hang on"] and state["wake"]["resume_words"] == ["carry on"]

    def test_rendered_window_shows_the_new_rows(self, qapp):
        app = _make_app({"command_mode": {"enabled": True, "mode": "toggle"}, "wake_word_enabled": True,
                         "wake_word_config": {"opens_session": True}, "mode": "toggle"})
        win = qr._QuickReferenceWindow(app)
        try:
            texts = _all_label_texts(win)
            assert "Dictate (press to start, press to stop)" in texts
            assert any(t.startswith("CapsLock (fixed)") for t in texts)
            assert "Stop (keeps the draft, mic stays on)" in texts
            assert "Sleep (keeps the draft, ends the session)" in texts
            assert any("opens the hands-free session" in t for t in texts)
            assert "Voice memo" in texts and "Correction capture" in texts
        finally:
            win.close()


class TestModesTabRoundTripIntoQuickReference:
    def test_modes_tab_save_output_renders_in_quick_reference(self, qapp):
        """08d: the keys the Modes tab writes are the keys the Quick Reference
        reads -- one config path, no second mapping."""
        from samsara.ui.settings_qt import _SettingsWindow
        from tests.test_settings import _StubApp
        stub = _StubApp()
        stub.config = {"hotkeys": {"capture_correction": "ctrl+alt+x"}}
        win = _SettingsWindow(stub)
        win._widgets['memo_hotkey']._combo = 'ctrl+alt+q'
        win._widgets['capture_correction_hotkey']._combo = 'ctrl+alt+y'
        win._widgets['continuous_commit_hotkey']._combo = 'ctrl+alt+k'
        win._widgets['continuous_commit_trigger'].setCurrentText('key')
        win._widgets['ava_mode_key'].setCurrentText('F13')
        updates = win._save_fns[1]({})
        rows = {r["label"]: r for r in qr._resolve_hotkeys(_make_app(updates))}
        assert rows["Voice memo"]["value"] == "Ctrl+Alt+Q"
        assert rows["Correction capture"]["value"] == "Ctrl+Alt+Y"
        assert rows["Continuous commit"]["value"].startswith("Ctrl+Alt+K") and rows["Continuous commit"]["enabled"]
        assert rows["Streaming (live partials)"]["value"].startswith(qr.STREAMING_KEY_LABEL)
