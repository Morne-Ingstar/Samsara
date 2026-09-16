"""Queue 118: END-TO-END dictation latency -- last word spoken until the text
is present in the target window. Measured, not derived.

    python tools/e2e_latency_bench_118.py --lane hotkey --trials 30
    python tools/e2e_latency_bench_118.py --lane handsfree --trials 30

Samsara has quoted "~750-900 ms" from decode time plus assumptions. This
measures the whole chain on a wall clock, in one pass per trial, and polls
the target window for the text instead of trusting the paste call -- a paste
can return before the application has rendered anything.

WHAT ONE TRIAL DOES
  1. Loads one of the owner's own hold-to-dictate dumps (real voice, real
     microphone, real room) and finds the end of speech with the SAME VAD the
     app runs live (faster_whisper.vad.SileroVADModel on the bundled
     silero_vad_v6.onnx, 100 ms frames, probability > LIVE_VAD_PROB_THRESHOLD
     -- dictation.py _vad_is_speech).
  2. Feeds the clip frame by frame in REAL TIME (100 ms frames, FRAME_MS),
     so every wait a live speaker would experience is on the clock.
       hotkey    the capture ends when the file ends: that is the moment the
                 owner released the key, and the gap between the last speech
                 and the file end is his own release reaction, reported
                 separately as release_delay_ms.
       handsfree the capture ends when the app's own endpoint rule fires:
                 once speaking, accumulate silence and flush at
                 command_mode.dictate_utterance_silence_s
                 (wake_consumer.py:1066 and :1317-1319).
  3. Decodes with faster-whisper using the app's parameters for the chosen
     profile (see PROFILES).
  4. Pastes through the app's real injection path,
     samsara.clipboard.paste_with_preservation -- including its 50 ms
     CLIPBOARD_PASTE_DELAY before Ctrl+V.
  5. A poller thread reads the target window every 2 ms and records the first
     moment the decoded text is present.

  e2e_ms          = text visible - end of speech          <- the felt number
  app_ms          = text visible - end of capture         <- what the app owns
  release_ms      = end of capture - end of speech        <- hotkey: the human
  endpoint_ms     = end of capture - end of speech        <- handsfree: the rule

WHAT IT CANNOT SEE (stated because the number would flatter us otherwise)
  * It is the PIPELINE, not the shipping process: Samsara's own threading,
    queueing and Qt work between capture and paste are not here, so the real
    app is this PLUS its orchestration. The app's own logs are the only way
    to bound that, and they carry no "last word" timestamp.
  * The hotkey hook itself (key press -> capture starts) is not included.
  * Microphone and driver input latency are not included: audio comes from a
    file, not the sound card.
  * The GPU is shared with the running app; decode times include whatever
    contention existed (--note records it).
  * A Win32 EDIT control renders faster than a real editor. --target notepad
    measures a real application through UI Automation for comparison, whose
    own polling cost is reported.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import random
import statistics
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

FRAME_MS = 100                      # samsara/audio_engine/frame.py
LIVE_VAD_PROB = 0.5                 # samsara/constants.py LIVE_VAD_PROB_THRESHOLD
SAMPLE_RATE = 16000
OUT_JSON = REPO / "perf_artifacts" / "comparison_baseline_118_latency.json"

# dictation.py _get_transcribe_params, minus the personal initial_prompt.
PROFILES = {
    # What a first run gets: model_size "base" (config_schema.py), device
    # "auto" -> cuda here (dictation.py:5587), performance_mode "balanced".
    "default": dict(model="base", params=dict(
        beam_size=3, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
        condition_on_previous_text=False, without_timestamps=True,
        word_timestamps=False, no_speech_threshold=0.6, log_prob_threshold=-1.0)),
    # What this machine is actually configured for today.
    "owner": dict(model="medium", params=dict(
        beam_size=5, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=300),
        condition_on_previous_text=True, without_timestamps=False,
        word_timestamps=False, no_speech_threshold=0.6, log_prob_threshold=-1.0)),
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
WM_GETTEXT, WM_GETTEXTLENGTH, WM_SETTEXT = 0x000D, 0x000E, 0x000C
WS_POPUP, WS_VISIBLE, WS_BORDER, ES_MULTILINE = 0x80000000, 0x10000000, 0x00800000, 0x0004


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

class EditTarget:
    """A plain Win32 EDIT control. The floor: no application logic between the
    paste and the text being readable.

    The window is created on its own thread WITH A MESSAGE PUMP: a control
    whose owning thread never dispatches messages cannot process the Ctrl+V
    at all (the first version of this harness measured nothing for that
    reason), and a cross-thread WM_GETTEXT needs that pump too.
    """

    name = "win32-edit"

    def __init__(self):
        self.hwnd = None
        self._ready = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or not self.hwnd:
            raise OSError("target window never came up")
        self.focus()

    def _pump(self):
        self.hwnd = user32.CreateWindowExW(
            0, "EDIT", "samsara e2e target",
            WS_POPUP | WS_VISIBLE | WS_BORDER | ES_MULTILINE,
            80, 80, 900, 220, None, None, None, None)
        self._ready.set()
        if not self.hwnd:
            return
        msg = wt.MSG()
        while not self._stop:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.001)

    def focus(self):
        """Windows refuses SetForegroundWindow to a process that does not own
        the foreground, so borrow the foreground thread's input queue for the
        call (the documented AttachThreadInput dance) and retry briefly."""
        user32.ShowWindow(self.hwnd, 5)
        for _ in range(5):
            fg = user32.GetForegroundWindow()
            if fg == self.hwnd:
                return
            fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            me = kernel32.GetCurrentThreadId()
            attached = bool(user32.AttachThreadInput(fg_thread, me, True)) if fg_thread else False
            try:
                user32.BringWindowToTop(self.hwnd)
                user32.SetForegroundWindow(self.hwnd)
                user32.SetActiveWindow(self.hwnd)
                user32.SetFocus(self.hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(fg_thread, me, False)
            time.sleep(0.06)

    def is_foreground(self) -> bool:
        return user32.GetForegroundWindow() == self.hwnd

    def clear(self):
        user32.SendMessageW(self.hwnd, WM_SETTEXT, 0, "")

    def text(self) -> str:
        n = user32.SendMessageW(self.hwnd, WM_GETTEXTLENGTH, 0, 0)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.SendMessageW(self.hwnd, WM_GETTEXT, n + 1, buf)
        return buf.value

    def close(self):
        self._stop = True
        self._thread.join(timeout=2)
        user32.DestroyWindow(self.hwnd)


class NotepadTarget:
    """A real application, read through UI Automation."""

    name = "notepad-uia"

    def __init__(self):
        import subprocess  # noqa: PLC0415

        import uiautomation as auto  # noqa: PLC0415

        self.auto = auto
        self.proc = subprocess.Popen(["notepad.exe"])
        deadline = time.time() + 15
        self.window = None
        while time.time() < deadline:
            window = auto.WindowControl(searchDepth=1, ProcessId=self.proc.pid)
            if window.Exists(0.4, 0.2):
                self.window = window
                break
        if self.window is None:
            raise RuntimeError("Notepad window never appeared")
        self.window.SetActive()
        self.edit = self.window.EditControl() if self.window.EditControl().Exists(3, 0.3) else \
            self.window.DocumentControl()
        self.focus()

    def focus(self):
        self.window.SetActive()
        try:
            self.edit.SetFocus()
        except Exception:
            pass

    def is_foreground(self) -> bool:
        try:
            return bool(self.window.IsTopmost()) or self.auto.GetForegroundControl().NativeWindowHandle == \
                self.window.NativeWindowHandle
        except Exception:
            return True

    def clear(self):
        self.focus()
        self.auto.SendKeys("{Ctrl}a{Delete}", waitTime=0)
        time.sleep(0.05)

    def text(self) -> str:
        try:
            return self.edit.GetValuePattern().Value
        except Exception:
            try:
                return self.edit.Name or ""
            except Exception:
                return ""

    def close(self):
        try:
            self.proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Audio + VAD
# ---------------------------------------------------------------------------

def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != SAMPLE_RATE:
        idx = np.linspace(0, len(audio) - 1, int(len(audio) * SAMPLE_RATE / sr))
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return audio


class Vad:
    """The app's live VAD: the bundled Silero ONNX, one probability per
    frame, speech when any probability exceeds LIVE_VAD_PROB_THRESHOLD."""

    def __init__(self):
        from faster_whisper.utils import get_assets_path  # noqa: PLC0415
        from faster_whisper.vad import SileroVADModel  # noqa: PLC0415

        self.model = SileroVADModel(str(Path(get_assets_path()) / "silero_vad_v6.onnx"))

    def frame_is_speech(self, frame: np.ndarray) -> bool:
        """dictation.py _vad_probabilities + _vad_is_speech, verbatim: the
        wrapper is stateless, takes a flat float32 buffer whose length is a
        multiple of 512, and a sub-frame tail is discarded."""
        audio = np.ascontiguousarray(frame, dtype=np.float32).reshape(-1)
        usable = (audio.size // 512) * 512
        if usable == 0:
            return False
        probs = np.asarray(self.model(audio[:usable]), dtype=np.float32).reshape(-1)
        return bool(np.any(probs > LIVE_VAD_PROB))

    def speech_end_s(self, audio: np.ndarray) -> float:
        """End of the last frame carrying speech."""
        step = SAMPLE_RATE * FRAME_MS // 1000
        last = 0.0
        for start in range(0, len(audio) - step + 1, step):
            if self.frame_is_speech(audio[start:start + step]):
                last = (start + step) / SAMPLE_RATE
        return last


# ---------------------------------------------------------------------------
# One trial
# ---------------------------------------------------------------------------

def poll_until(target, expect_words, deadline_s, interval_s=0.002):
    """First instant the decoded text is present in the target window."""
    expect = " ".join((expect_words or "").split()).lower()[:40]
    end = time.perf_counter() + deadline_s
    while time.perf_counter() < end:
        got = " ".join(target.text().split()).lower()
        if expect and expect in got:
            return time.perf_counter()
        time.sleep(interval_s)
    return None


def run_trial(model, opts, vad, clip, target, lane, silence_gap_s, paste_fn):
    audio = clip["audio_data"]
    step = SAMPLE_RATE * FRAME_MS // 1000
    frames = [audio[i:i + step] for i in range(0, len(audio), step)]

    target.clear()
    if not target.is_foreground():
        target.focus()
        time.sleep(0.05)
        if not target.is_foreground():
            return {"error": "target lost focus"}

    # --- real-time capture ------------------------------------------------
    t_start = time.perf_counter()
    speaking = False
    silence_start = None
    t_capture_end = None
    fed = []
    for i, frame in enumerate(frames):
        due = t_start + (i * FRAME_MS / 1000.0)
        now = time.perf_counter()
        if due > now:
            time.sleep(due - now)
        fed.append(frame)
        if lane == "handsfree":
            if vad.frame_is_speech(frame):
                speaking, silence_start = True, None
            elif speaking:
                if silence_start is None:
                    silence_start = time.perf_counter()
                elif time.perf_counter() - silence_start >= silence_gap_s:
                    t_capture_end = time.perf_counter()
                    break
    t_speech_end = t_start + clip["speech_end_s"]
    if t_capture_end is None:
        # hotkey: the capture ends with the file (the key release), and for a
        # handsfree clip whose trailing silence is shorter than the gap, the
        # remaining wait is added so the rule is not credited with a shortcut.
        t_capture_end = time.perf_counter()
        if lane == "handsfree":
            wait = silence_gap_s - (t_capture_end - t_speech_end)
            if wait > 0:
                time.sleep(wait)
                t_capture_end = time.perf_counter()

    # --- decode -----------------------------------------------------------
    captured = np.concatenate(fed) if fed else audio
    segments, _info = model.transcribe(captured, **opts)
    text = "".join(s.text for s in segments).strip()
    t_decode_done = time.perf_counter()
    if not text:
        return {"error": "empty decode", "clip": clip["name"]}

    # --- inject + watch the window ---------------------------------------
    # Focus is re-asserted HERE, not only at the start: the real-time feed
    # takes seconds and anything on the machine can take the foreground in
    # between, which would send Ctrl+V somewhere else entirely.
    if not target.is_foreground():
        target.focus()
    if not target.is_foreground():
        return {"error": "target lost focus before paste", "clip": clip["name"]}
    result = {}

    def _watch():
        result["t_visible"] = poll_until(target, text, deadline_s=8.0)

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    t_paste_start = time.perf_counter()
    paste_fn(text)
    t_paste_return = time.perf_counter()
    watcher.join(timeout=8.5)
    t_visible = result.get("t_visible")
    if t_visible is None:
        return {"error": "text never appeared", "clip": clip["name"], "text": text[:60],
                "target_had": target.text()[:60],
                "foreground_at_paste": target.is_foreground()}

    ms = lambda a, b: round((b - a) * 1000.0, 1)  # noqa: E731
    return {
        "clip": clip["name"],
        "audio_s": round(len(audio) / SAMPLE_RATE, 2),
        "speech_end_s": round(clip["speech_end_s"], 2),
        "text": text,
        "capture_gap_ms": ms(t_speech_end, t_capture_end),
        "decode_ms": ms(t_capture_end, t_decode_done),
        "paste_call_ms": ms(t_paste_start, t_paste_return),
        "decode_to_visible_ms": ms(t_decode_done, t_visible),
        "app_ms": ms(t_capture_end, t_visible),
        "e2e_ms": ms(t_speech_end, t_visible),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def pick_clips(n, seed, min_s, max_s, min_trailing_s, vad):
    pool = sorted(Path.home().joinpath(".samsara/debug").glob("hotkey_*.wav"))
    pool = [p for p in pool if min_s * 32000 + 44 <= p.stat().st_size <= max_s * 32000 + 44]
    rng = random.Random(seed)
    rng.shuffle(pool)
    clips = []
    for path in pool:
        audio = load_wav(path)
        speech_end = vad.speech_end_s(audio)
        total = len(audio) / SAMPLE_RATE
        if speech_end <= 0.5 or (total - speech_end) < min_trailing_s:
            continue
        clips.append({"name": path.name, "audio_data": audio, "speech_end_s": speech_end,
                      "total_s": total})
        if len(clips) >= n:
            break
    return clips


def summarise(rows, key):
    values = [r[key] for r in rows if key in r]
    if not values:
        return None
    s = sorted(values)

    def q(p):
        k = (len(s) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(s) - 1)
        return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)

    return {"n": len(s), "p50": q(0.5), "p95": q(0.95), "max": s[-1],
            "mean": round(statistics.mean(s), 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lane", choices=["hotkey", "handsfree"], default="hotkey")
    ap.add_argument("--profile", choices=list(PROFILES), default="default")
    ap.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    ap.add_argument("--target", choices=["edit", "notepad"], default="edit")
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--seed", type=int, default=118)
    ap.add_argument("--min-s", type=float, default=2.0)
    ap.add_argument("--max-s", type=float, default=9.0)
    ap.add_argument("--silence-gap", type=float, default=None,
                    help="handsfree endpoint gap; default reads the live config")
    ap.add_argument("--note", default="")
    ap.add_argument("--out", default=str(OUT_JSON))
    args = ap.parse_args(argv)

    from faster_whisper import WhisperModel  # noqa: PLC0415

    from samsara.clipboard import paste_with_preservation  # noqa: PLC0415

    gap = args.silence_gap
    gap_source = "--silence-gap"
    if gap is None:
        try:
            cfg = json.loads(Path.home().joinpath(".samsara/config.json").read_text(encoding="utf-8"))
            gap = float(cfg.get("command_mode", {}).get("dictate_utterance_silence_s", 0.65))
            gap_source = "~/.samsara/config.json command_mode.dictate_utterance_silence_s"
        except Exception:
            gap, gap_source = 0.65, "wake_consumer.py fallback"

    print(f"[SETUP] VAD + clips ({args.trials} trials, lane={args.lane}, profile={args.profile})",
          flush=True)
    vad = Vad()
    clips = pick_clips(args.trials, args.seed, args.min_s, args.max_s,
                       min_trailing_s=0.2, vad=vad)
    if len(clips) < args.trials:
        print(f"[WARN] only {len(clips)} clips matched the filter", flush=True)
    profile = PROFILES[args.profile]
    compute = "float16" if args.device == "cuda" else "int8"
    model = WhisperModel(profile["model"], device=args.device, compute_type=compute)
    opts = dict(profile["params"])
    opts["language"] = "en"
    model.transcribe(clips[0]["audio_data"][:SAMPLE_RATE], **opts)   # warm

    target = EditTarget() if args.target == "edit" else NotepadTarget()
    rows, errors = [], []
    try:
        for i, clip in enumerate(clips, 1):
            row = run_trial(model, opts, vad, clip, target, args.lane, gap,
                            paste_with_preservation)
            if "error" in row:
                errors.append(row)
                print(f"  {i:>3}/{len(clips)} SKIP {row['error']}", flush=True)
                if row["error"] == "target lost focus":
                    break
                continue
            rows.append(row)
            print(f"  {i:>3}/{len(clips)} e2e {row['e2e_ms']:>7} ms "
                  f"(gap {row['capture_gap_ms']}, decode {row['decode_ms']}, "
                  f"paste->visible {row['decode_to_visible_ms']})", flush=True)
    finally:
        target.close()

    summary = {
        "lane": args.lane, "profile": args.profile, "model": profile["model"],
        "device": args.device, "compute_type": compute, "target": target.name,
        "trials": len(rows), "errors": errors, "note": args.note,
        "silence_gap_s": gap if args.lane == "handsfree" else None,
        "silence_gap_source": gap_source if args.lane == "handsfree" else None,
        "e2e_ms": summarise(rows, "e2e_ms"),
        "app_ms": summarise(rows, "app_ms"),
        "capture_gap_ms": summarise(rows, "capture_gap_ms"),
        "decode_ms": summarise(rows, "decode_ms"),
        "decode_to_visible_ms": summarise(rows, "decode_to_visible_ms"),
        "paste_call_ms": summarise(rows, "paste_call_ms"),
        "rows": rows,
    }
    out = Path(args.out)
    blob = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"runs": []}
    blob["runs"] = [r for r in blob.get("runs", [])
                    if not (r["lane"] == args.lane and r["profile"] == args.profile
                            and r["device"] == args.device and r["target"] == target.name)]
    blob["runs"].append(summary)
    out.write_text(json.dumps(blob, indent=1), encoding="utf-8")
    print(f"\n{args.lane}/{args.profile}/{args.device}/{target.name}: "
          f"e2e {summary['e2e_ms']}\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
