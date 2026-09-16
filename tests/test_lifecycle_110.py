"""Queue 110 -- three lifecycle defects.

A. A replaced schedule could resume the old task. One shared threading.Event
   was set by _stop_schedule() and immediately cleared by the replacement's
   _start_schedule(), so an old worker that happened to be inside its effect
   came back to a cleared event and kept firing alongside the new one (Astra
   F7 reproduced the old task firing twice after the replacement started).
   Each schedule now owns its own event, captured by its own closure, and
   "stop the schedule" stops every live one and says how many.

B. tools/check_thread_discipline.py exits 0 on the working tree, and CI runs
   it -- it existed but nothing invoked it, so three unregistered threads and
   two stale allowlist line numbers sat in the tree undetected.

C. Ava could not be interrupted. A mode switch, an abort phrase or leaving
   the session now cancel the in-flight turn: speech stops, the pending reply
   is dropped rather than delivered late, the generation is bumped so a slow
   response cannot land afterwards, a resolved-but-unexecuted action runs
   nothing, and the cancellation is chipped.
"""
import collections
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins.commands import ask_ollama  # noqa: E402
from samsara import execution_policy as ep  # noqa: E402


def _app():
    a = types.SimpleNamespace()
    a.config = {"ollama": {"enabled": True}}
    a.audio_coordinator = Mock()
    a.audio_coordinator.is_speaking = True
    a.audio_coordinator.speak = Mock(return_value=types.SimpleNamespace(utterance_id="u1"))
    a.play_sound = Mock()
    a._show_outcome_chip = Mock()
    a._ava_cmd_generation = 0
    a._ava_cmd_mode_lock = threading.Lock()
    a._ava_session_dispatch_queue = collections.deque(maxlen=3)
    a._ava_session_dispatch_lock = threading.Lock()
    a._ava_session_request_in_flight = False
    return a


@pytest.fixture(autouse=True)
def _clean():
    ask_ollama.clear_pending_action()
    ask_ollama._stop_schedule()
    yield
    ask_ollama.clear_pending_action()
    ask_ollama._stop_schedule()


def _task(label, interval):
    return {"type": "schedule", "interval_seconds": interval, "command": label,
            "key": None, "confirm_text": label, "generation": 0}


# ---------------------------------------------------------------------------
# A. The zombie schedule
# ---------------------------------------------------------------------------

class TestScheduleLifecycle:
    def test_replacing_a_schedule_stops_the_old_worker_mid_effect(self, monkeypatch):
        """The exact Astra F7 shape: the old worker is INSIDE its effect when
        the replacement starts. Real threads, the real start/stop functions."""
        fired = []
        inside_effect = threading.Event()
        release = threading.Event()

        def fake_execute(app, action):
            fired.append(action["confirm_text"])
            if action["confirm_text"] == "old":
                inside_effect.set()
                release.wait(5.0)

        monkeypatch.setattr(ask_ollama, "_execute_safe", fake_execute)
        app = _app()

        ask_ollama._start_schedule(app, _task("old", 0.02))
        assert inside_effect.wait(5.0), "old schedule never fired"

        # The replacement starts while the old worker is still in its effect.
        ask_ollama._start_schedule(app, _task("new", 30.0))
        assert ask_ollama.live_schedule_count() == 1

        fired_at_replacement = list(fired)
        release.set()
        # Many old intervals' worth of wall clock.
        time.sleep(0.5)

        assert fired == fired_at_replacement, (
            f"old task fired again after the replacement started: {fired}")
        assert fired.count("new") == 0, "the 30s replacement should not have fired yet"

    def test_start_never_clears_another_schedules_event(self):
        """The defect in one line: the replacement must not be able to touch
        the event the previous worker is waiting on."""
        app = _app()
        first = ask_ollama._start_schedule(app, _task("first", 30.0))
        assert not first.is_set()
        second = ask_ollama._start_schedule(app, _task("second", 30.0))
        assert first is not second
        assert first.is_set(), "the replaced schedule's own event must stay SET"
        assert not second.is_set()

    def test_stop_schedule_stops_every_live_schedule_and_counts_them(self, monkeypatch):
        """Two live workers (the second started without the usual stop-first)
        are both stopped, and the count is what the voice handler reports."""
        fired = []
        app = _app()
        monkeypatch.setattr(ask_ollama, "_execute_safe",
                            lambda app_, action: fired.append(action["confirm_text"]))
        first = ask_ollama._start_schedule(app, _task("first", 0.02))
        real_stop = ask_ollama._stop_schedule
        monkeypatch.setattr(ask_ollama, "_stop_schedule", lambda: 0)
        second = ask_ollama._start_schedule(app, _task("second", 0.02))
        monkeypatch.setattr(ask_ollama, "_stop_schedule", real_stop)

        assert ask_ollama.live_schedule_count() == 2
        time.sleep(0.2)
        assert "first" in fired and "second" in fired

        spoken = []
        monkeypatch.setattr(ask_ollama, "speak", lambda app_, text, **kw: spoken.append(text))
        ask_ollama.handle_stop_schedule(app)

        assert first.is_set() and second.is_set()
        assert ask_ollama.live_schedule_count() == 0
        assert spoken == ["Stopped 2 schedules."]

        time.sleep(0.1)
        settled = list(fired)
        time.sleep(0.2)
        assert fired == settled, f"a schedule kept firing after stop: {fired}"

    def test_stop_schedule_reports_the_single_case_and_the_empty_case(self, monkeypatch):
        app = _app()
        spoken = []
        monkeypatch.setattr(ask_ollama, "speak", lambda app_, text, **kw: spoken.append(text))

        ask_ollama.handle_stop_schedule(app)
        assert spoken == ["No schedule is running."]

        ask_ollama._start_schedule(app, _task("one", 30.0))
        ask_ollama.handle_stop_schedule(app)
        assert spoken[-1] == "Schedule stopped."
        assert ask_ollama.live_schedule_count() == 0


# ---------------------------------------------------------------------------
# B. Thread discipline
# ---------------------------------------------------------------------------

class TestThreadDiscipline:
    def test_checker_passes_on_the_working_tree(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B",
             str(ROOT / "tools" / "check_thread_discipline.py")],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert result.returncode == 0, (
            f"thread discipline check failed:\n{result.stdout}\n{result.stderr}")

    def test_ci_runs_the_checker(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "check_thread_discipline.py" in ci, (
            "CI must invoke tools/check_thread_discipline.py -- an unrun checker "
            "is how three violations and two stale allowlist lines survived")

    def test_ava_edit_worker_goes_through_the_registry(self):
        src = (ROOT / "samsara" / "ava_edit" / "session.py").read_text(encoding="utf-8")
        assert 'thread_registry.spawn("ava-edit-propose"' in src
        assert "threading.Thread(" not in src

    def test_allowlist_entries_point_at_real_lines(self):
        """The allowlist is keyed by line number, so a moved line silently
        stops covering the site it was written for. Both stale entries
        (sleep_overlay 75->76, coordinator 216->254) were exactly that."""
        allow = (ROOT / "tools" / "thread_discipline_allow.txt").read_text(encoding="utf-8")
        for entry in allow.splitlines():
            entry = entry.strip()
            if not entry or entry.startswith("#"):
                continue
            rel, lineno = entry.rsplit(":", 1)
            path = ROOT / rel
            assert path.exists(), f"allowlist points at a missing file: {rel}"
            lines = path.read_text(encoding="utf-8").splitlines()
            n = int(lineno)
            assert 1 <= n <= len(lines), f"{entry}: line out of range"
            assert ("threading.Thread(" in lines[n - 1]
                    or "threading.Timer(" in lines[n - 1]), (
                f"{entry} no longer points at a thread/timer construction: "
                f"{lines[n - 1].strip()!r}")


# ---------------------------------------------------------------------------
# C. Cancelling Ava mid-answer
# ---------------------------------------------------------------------------

class TestAvaCancellation:
    def test_turn_is_live_while_ava_is_still_speaking(self):
        """The in-flight flag drops when the answer is handed to TTS, which is
        the exact window the owner hit on 2026-09-15."""
        app = _app()
        assert ask_ollama.turn_is_live(app) is False   # nothing stamped yet
        ask_ollama.speak(app, "a long answer")
        assert app._ava_session_request_in_flight is False
        assert ask_ollama.turn_is_live(app) is True

    def test_turn_is_not_live_once_the_generation_moved_on(self):
        app = _app()
        ask_ollama.speak(app, "a long answer")
        ep.bump_generation(app, "something else")
        assert ask_ollama.turn_is_live(app) is False

    def test_cancel_stops_speech_bumps_the_generation_and_chips(self):
        app = _app()
        ask_ollama.speak(app, "a long answer")
        before = ep.current_generation(app)

        cleared = ask_ollama.cancel_turn(app, "mode switch to command")

        assert cleared is not None
        assert cleared["speech"] is True
        app.audio_coordinator.cancel_speech.assert_called_once()
        assert ep.current_generation(app) == before + 1
        app._show_outcome_chip.assert_called_once_with("Ava: cancelled", "accent")
        assert app._ava_turn_outcome == ("Ava: cancelled", "accent")

    def test_cancel_with_nothing_live_does_nothing(self):
        app = _app()
        app.audio_coordinator.is_speaking = False
        before = ep.current_generation(app)
        assert ask_ollama.cancel_turn(app, "mode switch") is None
        assert ep.current_generation(app) == before
        app.audio_coordinator.cancel_speech.assert_not_called()
        app._show_outcome_chip.assert_not_called()

    def test_cancel_executes_no_action_the_turn_had_resolved(self, monkeypatch):
        """A staged 'Close Notepad?' is dropped, and a later 'yes' runs
        nothing because there is no pending action left to approve."""
        app = _app()
        ask_ollama.speak(app, "Close Notepad?")
        ask_ollama._pending_action = {
            "type": "action", "command": "close notepad", "confirm_text": "Close Notepad?",
            "generation": ep.current_generation(app), "expires": time.time() + 30,
        }

        ran = []
        monkeypatch.setattr(ask_ollama, "_execute_action2", lambda *a, **k: ran.append(a))

        cleared = ask_ollama.cancel_turn(app, "abort phrase")

        assert cleared["pending"] is True
        assert ask_ollama._pending_action is None
        assert ran == []

    def test_a_response_arriving_after_cancellation_is_discarded_on_generation(self, monkeypatch):
        """Not delivered late: the model answers AFTER the cancel, and neither
        handle_response nor speak ever sees it."""
        app = _app()
        app.audio_coordinator.is_speaking = False
        app._ava_session_request_in_flight = True

        model_called = threading.Event()
        release = threading.Event()
        delivered = []
        spoken = []

        def slow_model(prompt, app_, **kw):
            model_called.set()
            release.wait(5.0)
            return "CONFIRM Close Notepad.\nACTION2 close | notepad"

        monkeypatch.setattr(ask_ollama, "ask_ollama", slow_model)
        monkeypatch.setattr(ask_ollama, "handle_response", lambda *a, **k: delivered.append(a))
        monkeypatch.setattr(ask_ollama, "speak", lambda app_, text, **kw: spoken.append(text))
        monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda *a, **k: False)

        done = threading.Event()
        ask_ollama.handle_ask_ava(app, remainder="close notepad", on_done=done.set)

        assert model_called.wait(5.0), "the fake model was never called"
        app._ava_session_request_in_flight = False
        ep.stop_all(app, "mode switch to dictate", chip=False)
        release.set()

        assert done.wait(5.0), "the Ava worker never finished"
        assert delivered == [], "a cancelled turn delivered its reply"
        assert spoken == [], "a cancelled turn spoke after the cancel"

    def test_a_response_arriving_before_any_cancel_is_delivered(self, monkeypatch):
        """The guard must not swallow the normal case."""
        app = _app()
        app.audio_coordinator.is_speaking = False
        delivered = []
        monkeypatch.setattr(ask_ollama, "ask_ollama", lambda *a, **k: "just an answer")
        monkeypatch.setattr(ask_ollama, "handle_response",
                            lambda *a, **k: delivered.append(a) or None)
        monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda *a, **k: False)

        done = threading.Event()
        ask_ollama.handle_ask_ava(app, remainder="hello", on_done=done.set)
        assert done.wait(5.0)
        assert len(delivered) == 1


class TestCancellationReachesTheWorker:
    """The wiring: a mode switch and an abort phrase both arrive at
    ask_ollama.cancel_turn through DictationApp._cancel_ava_turn."""

    def test_cancel_ava_turn_delegates_to_the_one_seam(self, monkeypatch):
        import dictation

        calls = []
        monkeypatch.setattr(ask_ollama, "cancel_turn",
                            lambda app_, reason: calls.append(reason) or {"speech": True})
        app = _app()
        result = dictation.DictationApp._cancel_ava_turn(app, "mode switch to dictate")
        assert calls == ["mode switch to dictate"]
        assert result == {"speech": True}

    def test_cancel_ava_turn_never_breaks_the_mode_change(self, monkeypatch):
        """A missing or broken Ava plugin must not stop a mode switch."""
        import dictation

        def boom(app_, reason):
            raise RuntimeError("plugin gone")

        monkeypatch.setattr(ask_ollama, "cancel_turn", boom)
        assert dictation.DictationApp._cancel_ava_turn(_app(), "mode switch") is None

    def test_the_mode_change_and_abort_hooks_both_call_it(self):
        """Source-level, because both hooks are closures built inside
        _ensure_session_mode_manager: whichever way they are refactored, the
        cancellation must still be on both paths."""
        src = (ROOT / "dictation.py").read_text(encoding="utf-8")
        mode_hook = src.split("def _on_mode_change(mode:")[1].split("def ")[0]
        abort_hook = src.split("def _on_abort() -> None:")[1].split("def ")[0]
        assert "_cancel_ava_turn" in mode_hook
        assert "_cancel_ava_turn" in abort_hook


class TestStopAllStopsSpeech:
    def test_stop_all_cancels_in_progress_speech(self):
        app = _app()
        cleared = ep.stop_all(app, "voice stop", chip=False)
        app.audio_coordinator.cancel_speech.assert_called_once()
        assert cleared["speech"] is True

    def test_stop_all_survives_a_coordinator_that_cannot_cancel(self):
        app = _app()
        app.audio_coordinator.cancel_speech.side_effect = RuntimeError("engine gone")
        cleared = ep.stop_all(app, "voice stop", chip=False)
        assert cleared["generation"] > 0

    def test_stop_all_without_a_coordinator_is_fine(self):
        app = _app()
        app.audio_coordinator = None
        cleared = ep.stop_all(app, "voice stop", chip=False)
        assert cleared["speech"] is False
