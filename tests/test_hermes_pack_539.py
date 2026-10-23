"""Hermes measured contract and async lifecycle, entirely in memory."""
import io
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from plugins.commands import ask_agent
from samsara.agent_channel import cli_agents, hermes, hermes_voice as voice
from samsara.command_registry import CommandMatcher, DispatchState
from tests.test_settings_revisit_543 import window as settings_window

SID = "20261011_123456_abcd"
FORBIDDEN = {"--yolo", "--ignore-rules", "--ignore-user-config", "--safe-mode"}


def events(text="Four.", session=SID):
    return [json.dumps({"type": "system", "session_id": session}),
            json.dumps({"type": "text", "text": text}),
            json.dumps({"type": "result", "text": text, "session_id": session, "exit_code": 0})]


class Input(io.StringIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class Process:
    def __init__(self, output, errors="", code=0):
        self.stdin = Input()
        self.stdout = io.StringIO("\n".join(output))
        self.stderr = io.StringIO(errors)
        self.code = code
        self.pid = 999999
        self.killed = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(cli_agents, "_closing", threading.Event())
    monkeypatch.setattr(hermes.shutil, "which", lambda name: r"C:\fake install\hermes.cmd")
    config = {"agent_channel": {"cwd": str(tmp_path), "timeout_s": 240},
              "command_packs": {"hermes": True}, "hermes": {"notice_accepted": True},
              "agent_voice": {"digest": "off"},
              "tts": {"feedback_level": "full"}}
    state = SimpleNamespace(output=events(), errors="session_id: " + SID, code=0,
                            calls=[], processes=[], workers=[], after_wait=None)
    agent = hermes.HermesAgent()
    original_spawn = hermes.thread_registry.spawn

    def popen(argv, **kwargs):
        assert FORBIDDEN.isdisjoint(argv)
        assert kwargs["shell"] is False
        state.calls.append((argv, kwargs))
        process = Process(state.output, state.errors, state.code)
        if state.after_wait:
            process.wait = lambda timeout=None: (state.after_wait(), process.code)[1]
        state.processes.append(process)
        return process

    def spawn(name, fn, args=(), **kwargs):
        if name.startswith("hermes.std"):
            fn(*args)
        elif name.startswith(("agent.hermes", "hermes.")):
            state.workers.append((name, fn))
        else:
            return original_spawn(name, fn, args=args, **kwargs)

    def kill(process):
        process.killed = True

    monkeypatch.setattr(hermes.subprocess, "Popen", popen)
    monkeypatch.setattr(cli_agents, "_kill_tree", kill)
    monkeypatch.setattr(hermes.thread_registry, "spawn", spawn)
    spoken, chips, sounds, waiting = [], [], [], []
    app = SimpleNamespace(config=config, save_config=lambda: None, _hermes_channel=agent, recording=False,
        _schedule_ui=lambda fn: fn(), _show_outcome_chip=lambda *args: chips.append(args),
        audio_coordinator=SimpleNamespace(speak=lambda text, **kwargs: spoken.append(text)),
        play_sound=sounds.append,
        listening_indicator=SimpleNamespace(set_agent_waiting=lambda *args: waiting.append(args)))
    state.app, state.agent = app, agent
    state.spoken, state.chips, state.sounds, state.waiting = spoken, chips, sounds, waiting
    state.home = tmp_path / "home"
    return state


def run_worker(fake, name="agent.hermes"):
    index = next(i for i, (label, _) in enumerate(fake.workers) if label == name)
    _, fn = fake.workers.pop(index)
    fn()


def test_stdin_cmd_shim_and_flags(fake):
    message = 'literal "quotes" $(x) `backticks` & percent%\nnext line'
    reply = fake.agent.ask(message, fake.app.config)
    argv, kwargs = fake.calls[0]
    assert argv[0].endswith("hermes.cmd")
    assert message not in argv
    # 802: Hermes 0.22 refuses a custom --source and --run-budget, so neither is sent.
    assert "--source" not in argv and "--run-budget" not in argv
    assert fake.processes[0].stdin.sent == hermes.PREAMBLE + "\n\n" + message
    assert reply.text == "Four."
    assert not (fake.home / "agent_transcripts").exists()


def test_resume_then_new_session(fake):
    first = fake.agent.ask("first", fake.app.config)
    second = fake.agent.ask("second", fake.app.config)
    assert "--resume" not in first.argv
    assert second.argv[second.argv.index("--resume") + 1] == SID
    assert ask_agent.new_hermes_session(fake.app)
    third = fake.agent.ask("third", fake.app.config)
    assert "--resume" not in third.argv


def test_other_session_keys_are_preserved(fake):
    fake.home.mkdir()
    path = fake.home / "agent_sessions.json"
    path.write_text(json.dumps({"gpt": "gpt-id", "claude": "claude-id"}))
    fake.agent.ask("first", fake.app.config)
    fake.agent.new_conversation()
    assert json.loads(path.read_text()) == {"gpt": "gpt-id", "claude": "claude-id"}


@pytest.mark.parametrize("settings", [{}, {"profile": "voice", "model": "provider/model:4", "effort": "ultra"}])
def test_optional_settings_and_no_bypass(settings):
    argv = hermes.hermes_argv(["hermes.cmd"], None, settings, 240)
    assert FORBIDDEN.isdisjoint(argv)
    assert ("-p" in argv) == bool(settings)
    if settings:
        assert argv.index("-p") < argv.index("chat")
        assert argv[argv.index("-m") + 1] == settings["model"]
        assert argv[argv.index("--reasoning") + 1] == settings["effort"]
    else:
        assert "-m" not in argv and "--reasoning" not in argv


@pytest.mark.parametrize("key,value", [("profile", "bad & path"), ("model", "$(x)"),
    ("model", "-x " ), ("effort", "invented"), ("profile", "a" * 81)])
def test_invalid_settings_rejected_before_launch(fake, key, value):
    fake.app.config["hermes"][key] = value
    with pytest.raises(ValueError):
        fake.agent.ask("question", fake.app.config)
    assert not fake.calls


@pytest.mark.parametrize("session", ["short", "bad session", "id;bad", "x" * 65])
def test_invalid_resume_rejected(fake, session):
    fake.home.mkdir()
    (fake.home / "agent_sessions.json").write_text(json.dumps({"hermes": session}))
    with pytest.raises(ValueError):
        fake.agent.ask("question", fake.app.config)
    assert not fake.calls


def test_tools_unknown_and_multiple_text_events_never_spoken(fake):
    fake.output = ["not json", "[]", json.dumps({"type": "system", "session_id": SID}),
        json.dumps({"type": "tool_use", "text": "tool request"}),
        json.dumps({"type": "tool_result", "text": "tool output"}),
        *[json.dumps({"type": "text", "text": t}) for t in ("One", "Two", "Three")],
        json.dumps({"type": "unknown", "text": "ignored"}),
        json.dumps({"type": "result", "text": "Exactly the result.", "session_id": SID})]
    voice.ask(fake.app, "question")
    run_worker(fake)
    assert fake.spoken == ["Exactly the result."]


@pytest.mark.parametrize("source", ["result", "system", "stderr"])
def test_session_fallback_and_text_concatenation(source):
    lines = [json.dumps({"type": "text", "text": "A "}), json.dumps({"type": "text", "text": "reply."})]
    if source == "system":
        lines.insert(0, json.dumps({"type": "system", "session_id": SID}))
    lines.append(json.dumps({"type": "result", "session_id": SID if source == "result" else None}))
    assert hermes.parse_hermes(lines, ["Resumed session ignored title", "session_id: " + SID]) == ("A reply.", SID)


def test_result_session_id_wins_over_system_and_stderr():
    lines = [json.dumps({"type": "system", "session_id": "system_id"}),
             json.dumps({"type": "result", "session_id": SID, "text": "Answer"})]
    assert hermes.parse_hermes(lines, ["session_id: stderr_id"])[1] == SID


def test_background_busy_refused_not_queued(fake):
    assert ask_agent.ask_hermes(fake.app, "first") == DispatchState.QUEUED
    assert fake.waiting == [("Hermes", True)]
    assert fake.sounds == ["agent_routing"]
    assert not fake.calls  # No turn runs on the caller's command thread.
    assert ask_agent.ask_hermes(fake.app, "second") == DispatchState.REJECTED
    assert len([n for n, _ in fake.workers if n == "agent.hermes"]) == 1
    assert fake.chips[-1][0] == "Hermes: Hermes is still working on the last one."
    # Dictation can continue by changing the independent app capture state.
    fake.app.recording = False
    run_worker(fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("action", ["stop", "new"])
def test_stop_or_new_kills_tree_and_discards_late_completion(fake, action):
    voice.ask(fake.app, "question")
    turn = fake.agent._turn
    callback = voice.stop if action == "stop" else voice.new
    fake.after_wait = lambda: callback(fake.app)
    run_worker(fake)
    assert fake.processes[0].killed
    assert not fake.agent.valid(turn)
    assert not fake.spoken
    assert not hasattr(fake.app, "_cli_agent_last")
    assert all("Four." not in chip[0] for chip in fake.chips)
    assert fake.waiting[-1] == ("Hermes", False)
    if fake.home.exists():
        if action == "stop":
            assert fake.agent._sessions(fake.home)["hermes"] == SID
        else:
            assert "hermes" not in fake.agent._sessions(fake.home)


def test_stop_preserves_conversation_and_says_once(fake):
    fake.agent.ask("first", fake.app.config)
    voice.ask(fake.app, "second")
    voice.stop(fake.app)
    run_worker(fake, "hermes.notice")
    run_worker(fake)
    assert fake.spoken == ["Hermes stopped; your conversation is kept."]
    assert fake.agent._sessions(fake.home)["hermes"] == SID
    assert len(fake.calls) == 1


def test_shutdown_kills_running_process_and_does_not_join(fake):
    turn = fake.agent.begin()
    process = Process([])
    turn.process = process
    fake.agent.shutdown()
    assert process.killed and not fake.agent.valid(turn)
    with pytest.raises(hermes.Cancelled):
        fake.agent.begin()


@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_level_is_read_at_playback(fake, level):
    voice.ask(fake.app, "question")
    fake.app.config["tts"]["feedback_level"] = level
    run_worker(fake)
    assert fake.chips[-1][0].startswith("Hermes: Four.")
    assert fake.spoken == (["Four."] if level == "full" else [])
    assert ("agent_response" in fake.sounds) == (level == "brief")


@pytest.mark.parametrize("capture", ["recording", "hotkey_pressed", "_hold_down_key",
    "_command_mode_key_held", "_ava_mode_key_held", "wake", "surface"])
def test_answer_waits_for_capture_and_rechecks_level(fake, monkeypatch, capture):
    voice.ask(fake.app, "question")
    if capture == "wake":
        fake.app._wake_consumer = SimpleNamespace(_speech_start_ns=123)
    elif capture == "surface":
        fake.app.listening_indicator.controller = SimpleNamespace(model=SimpleNamespace(capture="recording"))
    else:
        setattr(fake.app, capture, True)
    waited = []

    def release(_):
        assert not fake.spoken
        assert fake.chips[-1][0].startswith("Hermes: Four.")
        waited.append(True)
        fake.app.config["tts"]["feedback_level"] = "silent"
        if capture == "wake":
            fake.app._wake_consumer._speech_start_ns = None
        elif capture == "surface":
            fake.app.listening_indicator.controller.model.capture = "off"
        else:
            setattr(fake.app, capture, False)
    monkeypatch.setattr(voice.time, "sleep", release)
    run_worker(fake)
    assert waited and not fake.spoken


@pytest.mark.parametrize("action", ["stop", "new"])
def test_reset_discards_answer_deferred_during_capture(fake, monkeypatch, action):
    voice.ask(fake.app, "question")
    fake.app.recording = True
    monkeypatch.setattr(voice.time, "sleep", lambda _: (voice.stop if action == "stop" else voice.new)(fake.app))
    run_worker(fake)
    assert not fake.spoken


@pytest.mark.parametrize("level", ["silent", "brief", "full"])
def test_followups_respect_current_level_and_paging(fake, level):
    pages = [" ".join([word] * 60) for word in ("First.", "Second.", "Third.")]
    fake.output = events(" ".join(pages))
    voice.ask(fake.app, "question")
    run_worker(fake)
    fake.spoken.clear()
    fake.sounds.clear()
    fake.app.config["tts"]["feedback_level"] = level
    # A digest keeps the complete reply; paging starts at its first page.
    state = fake.app._cli_agent_voice_state
    for index, page in enumerate(pages):
        for action in ("read the rest", "repeat that"):
            fake.spoken.clear()
            fake.sounds.clear()
            assert cli_agents.followup(fake.app, action)
            assert not fake.spoken and not fake.sounds  # Delivery stays off the caller.
            run_worker(fake, "hermes.readback")
            assert state["last"] == page
            assert state["rest"] == " ".join(pages[index + 1:])
            assert fake.spoken == ([page] if level == "full" else [])
            assert fake.sounds == (["agent_response"] if level == "brief" else [])
    assert cli_agents.followup(fake.app, "read the rest")
    run_worker(fake, "hermes.readback")
    assert state["last"] == "That's all of the Hermes reply."
    voice.stop(fake.app)
    assert not cli_agents.followup(fake.app, "repeat that")


def test_followup_waits_for_capture_and_rechecks_level(fake, monkeypatch):
    voice.ask(fake.app, "question")
    run_worker(fake)
    fake.spoken.clear()
    fake.sounds.clear()
    assert cli_agents.followup(fake.app, "repeat that")
    fake.app.recording = True
    waited = []

    def release(_):
        assert not fake.spoken and not fake.sounds
        waited.append(True)
        fake.app.config["tts"]["feedback_level"] = "brief"
        fake.app.recording = False

    monkeypatch.setattr(voice.time, "sleep", release)
    run_worker(fake, "hermes.readback")
    assert waited and not fake.spoken and fake.sounds == ["agent_response"]


def test_stop_discards_queued_followup(fake):
    voice.ask(fake.app, "question")
    run_worker(fake)
    fake.spoken.clear()
    fake.sounds.clear()
    assert cli_agents.followup(fake.app, "repeat that")
    voice.stop(fake.app)
    run_worker(fake, "hermes.readback")
    assert not fake.spoken and not fake.sounds


@pytest.mark.parametrize("mode", ["missing", "timeout", "nonzero", "no_result", "approval"])
def test_failures_and_approval_answer(fake, monkeypatch, mode):
    if mode == "missing":
        monkeypatch.setattr(hermes.shutil, "which", lambda _: None)
        expected = "Hermes isn't installed on this computer."
    elif mode == "timeout":
        fake.app.config["agent_channel"]["timeout_s"] = .01
        monkeypatch.setattr(hermes.queue.Queue, "get", lambda *a, **kw: (_ for _ in ()).throw(hermes.queue.Empty()))
        expected = "Hermes took too long; your conversation is kept."
    elif mode == "nonzero":
        fake.code, fake.errors = 2, "first error\nsecond error"
        expected = "Hermes couldn't answer: first error"
    elif mode == "no_result":
        fake.output, fake.errors = events()[:2], "first error\nsecond error"
        expected = "Hermes couldn't answer: first error"
    else:
        expected = "The command was blocked by the safety approval system and did not run."
        fake.output = events(expected)
    voice.ask(fake.app, "question")
    run_worker(fake)
    assert fake.spoken == [expected]
    assert fake.chips[-1][0] == "Hermes: " + expected


@pytest.mark.parametrize("accepted,enabled", [(False, True), (True, False), (False, False),
                                             ("true", True), (True, "true")])
def test_nothing_sent_before_notice_acceptance(fake, accepted, enabled):
    fake.app.config["hermes"]["notice_accepted"] = accepted
    fake.app.config["command_packs"]["hermes"] = enabled
    assert voice.ask(fake.app, "question") == DispatchState.FAILED
    assert not fake.calls and not any(n == "agent.hermes" for n, _ in fake.workers)
    with pytest.raises(RuntimeError, match="accept its notice"):
        fake.agent.ask("question", fake.app.config)
    assert not fake.calls


def test_pack_off_commands_absent_and_reference_hidden(fake):
    from samsara import command_packs, plugin_commands
    from samsara.ui.command_cheatsheet_qt import _annotate_disabled
    fake.app.config["command_packs"]["hermes"] = False
    enabled = command_packs.get_enabled_packs(fake.app.config)
    assert "hermes" not in enabled
    matcher = CommandMatcher()
    matcher.set_enabled_packs(enabled)
    matcher.load_plugins(plugin_commands._MODULE_ENTRIES[ask_agent.__name__])
    matcher.freeze()
    assert matcher.match("ask hermes question")[0] is None
    rows = [{"phrase": "ask hermes", "pack": "hermes"}, {"phrase": "ask gpt", "pack": "core"}]
    assert [r["phrase"] for r in _annotate_disabled(rows, {"hermes"})] == ["ask gpt"]


def test_pack_requires_path_and_consent(fake, monkeypatch):
    from samsara.command_packs import get_enabled_packs, default_pack_config
    assert default_pack_config()["hermes"] is False
    assert "hermes" in get_enabled_packs(fake.app.config)
    fake.app.config["hermes"]["notice_accepted"] = False
    assert "hermes" not in get_enabled_packs(fake.app.config)
    fake.app.config["hermes"]["notice_accepted"] = True
    monkeypatch.setattr(hermes.shutil, "which", lambda _: None)
    assert "hermes" not in get_enabled_packs(fake.app.config)


@pytest.mark.parametrize("effort", hermes.EFFORTS)
def test_effort_voice_form(fake, effort):
    assert ask_agent.new_hermes_session(fake.app, f"with {effort} effort")
    assert fake.app.config["hermes"]["effort"] == effort


def test_unlisted_effort_never_resets_session(fake):
    fake.agent.ask("first", fake.app.config)
    assert voice.new(fake.app, "with invented effort") == DispatchState.FAILED
    assert fake.agent._sessions(fake.home)["hermes"] == SID


def test_settings_missing_hidden_and_consent_once(fake, qapp, monkeypatch):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from samsara.ui.settings.hermes_section import HermesSection, add_section
    from samsara.ui import hermes_notice
    parent = QWidget()
    layout = QVBoxLayout(parent)
    owner = SimpleNamespace(app=fake.app, _widgets={"_pack_checkboxes": {}})
    monkeypatch.setattr(hermes.shutil, "which", lambda _: None)
    add_section(owner, layout)
    assert not owner._widgets["_pack_checkboxes"]
    monkeypatch.setattr(hermes.shutil, "which", lambda _: "hermes.cmd")
    fake.app.config["hermes"]["notice_accepted"] = False
    fake.app.config["command_packs"]["hermes"] = False
    prompts = []
    monkeypatch.setattr(hermes_notice, "request_acceptance", lambda _: prompts.append(True) or False)
    # Use a real QWidget as the dialog owner in product; acceptance seam is mocked here.
    section = HermesSection(owner)
    section.enabled.setChecked(True)
    assert not section.enabled.isChecked() and not section.accepted
    monkeypatch.setattr(hermes_notice, "request_acceptance", lambda _: prompts.append(True) or True)
    section.enabled.setChecked(True)
    assert section.accepted
    section.enabled.setChecked(False)
    section.enabled.setChecked(True)
    assert len(prompts) == 2
    assert section.updates()["notice_accepted"] is True
    assert not fake.calls
    section.deleteLater()
    parent.deleteLater()


def test_both_displays_have_owned_hermes_waiting(fake, qapp, monkeypatch):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui import qt_runtime
    monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
    compact = ListeningIndicator()
    compact.set_agent_waiting("Hermes", True)
    assert compact._resolve_colors()[2] == "Hermes"
    compact.set_session_mode("COMMAND", "#ffffff")
    assert compact._resolve_colors()[2] == "Hermes"
    compact.set_listening(True)
    assert compact._resolve_colors()[2] == "COMMAND"
    compact.set_listening(False)
    compact.set_session_mode(None, None)
    compact.set_thinking(True)
    compact.set_agent_waiting("Hermes", False)
    assert compact._thinking and compact._resolve_colors()[2] != "Hermes"
    compact.hide()
    compact.deleteLater()
    controller = LiveSurfaceController(fake.app)
    full = LiveSurfaceIndicatorAdapter(controller)
    full.set_agent_waiting("Hermes", True)
    assert controller.view().notice.message == "Hermes"
    full.set_thinking(True)
    full.set_agent_waiting("Hermes", False)
    assert controller.view().notice.message == "Ava thinking"


def test_logs_lengths_not_message_or_answer(fake, caplog):
    with caplog.at_level(logging.INFO, logger=hermes.__name__):
        fake.agent.ask("synthetic private question", fake.app.config)
    assert "input_chars=" in caplog.text and "output_chars=" in caplog.text
    assert "synthetic private question" not in caplog.text and "Four." not in caplog.text


def test_new_conversation_alias_and_hermes_table(fake):
    from samsara import plugin_commands
    matcher = CommandMatcher()
    matcher.set_enabled_packs({"core", "hermes"})
    matcher.load_plugins(plugin_commands._MODULE_ENTRIES[ask_agent.__name__])
    matcher.freeze()
    entry, remainder = matcher.match("new conversation with hermes")
    assert entry.handler is ask_agent.new_hermes_session and remainder == ""
    entry, remainder = matcher.match("tell hermes a question")
    assert entry.handler is ask_agent.ask_hermes and remainder == "a question"


@pytest.mark.parametrize("other_speaker", [False, True])
def test_stop_cancels_hermes_readback_without_interrupting_other_speaker(fake, other_speaker):
    audio = fake.app.audio_coordinator
    owned = object()
    cancelled = []

    def speak(text, **kwargs):
        audio._active_handle = owned
        return owned
    audio.speak = speak
    audio.cancel_speech = lambda: cancelled.append(True)
    voice.ask(fake.app, "question")
    run_worker(fake)
    if other_speaker:
        audio._active_handle = object()
    voice.stop(fake.app)
    assert bool(cancelled) == (not other_speaker)


def test_timeout_keeps_session_seen_before_completion(fake):
    def timeout():
        raise hermes.subprocess.TimeoutExpired("hermes", 240)
    fake.after_wait = timeout
    with pytest.raises(TimeoutError):
        fake.agent.ask("question", fake.app.config)
    assert fake.agent._sessions(fake.home)["hermes"] == SID


def test_consent_persists_immediately_and_unrelated_apply_keeps_it(fake, settings_window, monkeypatch, qapp):
    from samsara.ui import hermes_notice
    win = settings_window
    win.open_tool('Command library')
    qapp.processEvents()
    section = win._widgets['hermes_section']
    assert not section.accepted
    assert win._config_at_open['hermes']['notice_accepted'] is False
    prompts = []
    monkeypatch.setattr(hermes_notice, 'request_acceptance', lambda _: prompts.append(True) or True)
    section.enabled.setChecked(True)
    win.app.save_config.assert_called_once()
    assert win.app.config['hermes']['notice_accepted'] is True
    assert not win.app.config.get('command_packs', {}).get('hermes', False)
    assert not fake.calls
    # Audio Apply in this retained window does not save command-library edits.
    win.show_tab('Audio')
    qapp.processEvents()
    win._widgets['tts_speed'].setValue(1.4)
    win._apply_and_close()
    assert win.app.config['hermes']['notice_accepted'] is True
    assert win.app.config['tts']['speed'] == 1.4
    win.show_tab('Audio')
    win.open_tool('Command library')
    qapp.processEvents()
    rebuilt = win._widgets['hermes_section']
    assert rebuilt is not section and rebuilt.accepted
    assert not rebuilt.enabled.isChecked()
    rebuilt.enabled.setChecked(True)
    assert len(prompts) == 1


def test_hermes_controls_rebuild_from_live_config_and_apply_in_tool(fake, settings_window, monkeypatch, qapp):
    from samsara.ui import hermes_notice
    win = settings_window
    win.close()
    win.app.config['hermes'].update(notice_accepted=True, profile='current', model='provider/current', effort='high')
    win.app.config.setdefault('command_packs', {})['hermes'] = True
    win.show_tab('Audio')
    qapp.processEvents()
    # 785: Hermes setup and its scoped apply now belong to Integrations.
    win.open_tool('Integrations')
    qapp.processEvents()
    assert win._tool_name == 'Integrations'
    section = win._widgets['hermes_section']
    assert section.accepted and section.enabled.isChecked()
    assert section.profile.text() == 'current'
    assert section.model.text() == 'provider/current'
    assert section.effort.currentData() == 'high'
    section.model.setText('provider/edited')
    section.enabled.setChecked(False)
    win._apply_and_close()
    assert win.app.config['hermes']['model'] == 'provider/edited'
    assert win.app.config['hermes']['notice_accepted'] is True
    assert win.app.config['command_packs']['hermes'] is False
    win.show()
    qapp.processEvents()
    assert win._widgets['hermes_section'].model.text() == 'provider/edited'
    assert not win._widgets['hermes_section'].enabled.isChecked()


def test_hermes_acceptance_from_retired_visit_is_discarded(fake, settings_window, monkeypatch, qapp):
    from samsara.ui import hermes_notice
    win = settings_window
    win.open_tool('Command library')
    qapp.processEvents()
    old = win._widgets['hermes_section']
    def retired_dialog(_):
        win.close()
        win.show_tab('Audio')
        win.open_tool('Command library')
        return True
    monkeypatch.setattr(hermes_notice, 'request_acceptance', retired_dialog)
    old.enabled.setChecked(True)
    qapp.processEvents()
    assert win._widgets['hermes_section'] is not old
    assert win.app.config['hermes']['notice_accepted'] is False
    win.app.save_config.assert_not_called()
    assert not fake.calls
