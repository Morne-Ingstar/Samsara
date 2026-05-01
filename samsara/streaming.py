"""Streaming dictation: live partial transcription with overlay.

Architecture (converged after two ARC rounds):

  - Audio capture stays in dictation.py (audio_callback writes app.audio_data).
  - StreamingSession owns the lifecycle: spawn worker, manage overlay,
    finalize on hotkey release.
  - StreamingWorker (daemon thread) snapshots app.audio_data every ~1.5s,
    runs Whisper at beam_size=1, schedules overlay updates on the Tk thread.
  - On stop_event the worker runs a final beam_size=5 pass with full
    Grammar-Lite cleanup, then schedules paste + overlay close on the
    Tk thread.
  - All Tk widgets are created/touched only from the main thread via
    root.after(0, ...). The overlay is a CTkToplevel parented to app.root
    -- never a separate Tk root (Tcl_AsyncDelete crash).

Latency budget (for 'hold' streaming):

  pre-buffer skipped + 1.0s first chunk + ~0.3s transcribe = ~1.3s
  to first partial. Subsequent partials at ~1.5s cadence.

Locks:

  - app.model_lock (existing): serializes Whisper calls. Acquired
    non-blocking for partials -- if held, skip and try next interval.
  - audio_data is read via list() snapshot; CPython list ops are
    GIL-atomic, so a separate buffer_lock would be cosmetic.
"""

import ctypes
import sys
import threading
import time
import tkinter as tk

import customtkinter as ctk
import numpy as np
import pyautogui

try:
    import pyperclip
except ImportError:
    pyperclip = None

from samsara.cleanup import clean_text


# ---- Modifier-release plumbing (Windows) -----------------------------------
#
# In streaming direct-paste mode the user is physically holding the hotkey
# (e.g. Ctrl+Shift). Sending Backspace while those modifiers are held turns
# into Ctrl+Shift+Backspace, which most apps interpret as "delete previous
# word" or "do nothing" rather than a single-character delete. Same problem
# for the synthesized Ctrl+V inside _paste_preserving_clipboard.
#
# Before the backspace+paste sequence we send key-up events for any held
# Ctrl/Shift keys so the target app sees clean events; after, we re-press
# only the ones that were actually held when we started. State-aware
# restore matters because the FINAL paste runs after the user has already
# released the hotkey -- re-pressing keys that aren't held would inject a
# phantom down event with no matching up.

if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32

    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _VK_LCONTROL = 0xA2
    _VK_RCONTROL = 0xA3
    _VK_LSHIFT = 0xA0
    _VK_RSHIFT = 0xA1
    _MODIFIER_VKS = (_VK_LCONTROL, _VK_RCONTROL, _VK_LSHIFT, _VK_RSHIFT)

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _MOUSEINPUT(ctypes.Structure):
        # Declared so the INPUT union has the correct size on x64.
        _fields_ = [("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    def _send_key_event(vk, key_up):
        try:
            inp = _INPUT()
            inp.type = _INPUT_KEYBOARD
            inp.u.ki = _KEYBDINPUT(
                wVk=vk, wScan=0,
                dwFlags=_KEYEVENTF_KEYUP if key_up else 0,
                time=0, dwExtraInfo=None)
            _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        except Exception as e:
            print(f"[STREAM] SendInput vk={vk:#x} failed: {e}")

    def _release_held_modifiers():
        """Release any currently-held Ctrl/Shift keys. Returns the list of
        VKs that were held so the caller can press them back afterwards."""
        held = []
        try:
            for vk in _MODIFIER_VKS:
                if _user32.GetAsyncKeyState(vk) & 0x8000:
                    held.append(vk)
            for vk in held:
                _send_key_event(vk, key_up=True)
        except Exception as e:
            print(f"[STREAM] release_held_modifiers failed: {e}")
        return held

    def _press_modifiers(vks):
        if not vks:
            return
        try:
            for vk in vks:
                _send_key_event(vk, key_up=False)
        except Exception as e:
            print(f"[STREAM] press_modifiers failed: {e}")

else:
    def _release_held_modifiers():
        return []

    def _press_modifiers(vks):
        pass


MOD_GUARD_SETTLE_S = 0.01


FIRST_CHUNK_S = 0.7
CHUNK_INTERVAL_S = 1.0
MAX_DURATION_S = 120.0
MIN_PARTIAL_AUDIO_S = 0.5

OVERLAY_W = 500
OVERLAY_MIN_H = 60
OVERLAY_MAX_H = 200
TASKBAR_RESERVE = 50
OVERLAY_GAP_ABOVE_TASKBAR = 80

BG_COLOR = "#1a1a2a"
TEXT_COLOR = "#ffffff"
LISTENING_BORDER = "#5fb4a2"
DONE_BORDER = "#3ad26a"
FONT_FAMILY = "Segoe UI"
FONT_SIZE = 14
DIM_FONT_SIZE = 11
ALPHA = 0.92
DIM_ALPHA = 0.65

PARTIAL_BEAM = 1
FINAL_BEAM = 5
NO_SPEECH_THRESHOLD = 0.6
LOG_PROB_THRESHOLD = -1.0

# Direct-paste mode: replace the previous partial via Ctrl+Z undo,
# then paste the new text. Relies on the target app having a per-paste
# undo entry (true for Notepad, RichEdit, browsers, IDEs we tested).
# Tunable settles -- bumping these helps slow apps but adds latency.
UNDO_SETTLE_S = 0.05
PASTE_SETTLE_S = 0.02


class StreamingOverlay:
    """Floating overlay window. All methods run on the Tk main thread."""

    STATE_LISTENING = "listening"
    STATE_PROCESSING = "processing"
    STATE_DONE = "done"

    def __init__(self, root, dim=False):
        self._root = root
        self._dim = dim
        self._top = None
        self._frame = None
        self._label = None
        self._fade_after = None

    def show(self):
        if self._top is not None:
            return
        top = ctk.CTkToplevel(self._root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        base_alpha = DIM_ALPHA if self._dim else ALPHA
        try:
            top.attributes("-alpha", base_alpha)
        except tk.TclError:
            pass
        top.configure(fg_color=BG_COLOR)

        # Border lives on a left-edge frame so we can recolor it per state.
        self._border = ctk.CTkFrame(
            top, width=4, fg_color=LISTENING_BORDER, corner_radius=0)
        self._border.pack(side="left", fill="y")

        self._frame = ctk.CTkFrame(top, fg_color=BG_COLOR, corner_radius=0)
        self._frame.pack(side="left", fill="both", expand=True,
                         padx=(8, 12), pady=10)

        font_size = DIM_FONT_SIZE if self._dim else FONT_SIZE
        initial_text = ("Listening (direct paste)..."
                        if self._dim else "Listening...")
        self._label = ctk.CTkLabel(
            self._frame,
            text=initial_text,
            text_color=TEXT_COLOR,
            font=ctk.CTkFont(family=FONT_FAMILY, size=font_size),
            wraplength=OVERLAY_W - 40,
            justify="left",
            anchor="w",
        )
        self._label.pack(fill="both", expand=True)

        self._top = top
        self._position()

    def update_text(self, text, state=None):
        if self._top is None or self._label is None:
            return
        try:
            self._label.configure(text=text or "")
            if state is not None:
                self._set_border(state)
            self._position()
        except tk.TclError:
            pass

    def flash_done_and_fade(self, on_complete):
        if self._top is None:
            if on_complete:
                on_complete()
            return
        try:
            self._set_border(self.STATE_DONE)
        except tk.TclError:
            pass
        start_alpha = DIM_ALPHA if self._dim else ALPHA
        self._begin_fade(on_complete, alpha=start_alpha)

    def close(self):
        if self._fade_after is not None:
            try:
                self._root.after_cancel(self._fade_after)
            except tk.TclError:
                pass
            self._fade_after = None
        if self._top is not None:
            try:
                self._top.destroy()
            except tk.TclError:
                pass
            self._top = None
            self._label = None
            self._frame = None

    def _set_border(self, state):
        if state == self.STATE_DONE:
            color = DONE_BORDER
        else:
            color = LISTENING_BORDER
        try:
            self._border.configure(fg_color=color)
        except tk.TclError:
            pass

    def _position(self):
        if self._top is None:
            return
        try:
            self._top.update_idletasks()
            sw = self._top.winfo_screenwidth()
            sh = self._top.winfo_screenheight()
            req_h = max(OVERLAY_MIN_H,
                        min(OVERLAY_MAX_H, self._top.winfo_reqheight()))
            x = (sw - OVERLAY_W) // 2
            y = sh - TASKBAR_RESERVE - OVERLAY_GAP_ABOVE_TASKBAR - req_h
            self._top.geometry(f"{OVERLAY_W}x{req_h}+{x}+{y}")
        except tk.TclError:
            pass

    def _begin_fade(self, on_complete, alpha=ALPHA):
        if self._top is None:
            if on_complete:
                on_complete()
            return
        next_alpha = alpha - 0.08
        if next_alpha <= 0.05:
            self.close()
            if on_complete:
                on_complete()
            return
        try:
            self._top.attributes("-alpha", max(0.0, next_alpha))
        except tk.TclError:
            self.close()
            if on_complete:
                on_complete()
            return
        self._fade_after = self._root.after(
            40, lambda: self._begin_fade(on_complete, next_alpha))


class StreamingWorker(threading.Thread):
    """Daemon thread: runs partial Whisper passes, then a final pass on stop."""

    def __init__(self, session):
        super().__init__(daemon=True, name="streaming-worker")
        self._session = session
        self._stop_event = session.stop_event
        self._cancel_event = session.cancel_event

    def run(self):
        try:
            self._loop_partials()
        except Exception as e:
            print(f"[STREAM] Worker partial loop crashed: {e}")
        if self._cancel_event.is_set():
            self._session.on_cancelled()
            return
        try:
            self._final_pass()
        except Exception as e:
            print(f"[STREAM] Final pass crashed: {e}")
            self._session.on_final(None)

    def _loop_partials(self):
        first = True
        while not self._stop_event.is_set():
            wait_s = FIRST_CHUNK_S if first else CHUNK_INTERVAL_S
            first = False
            if self._stop_event.wait(timeout=wait_s):
                return
            if time.time() - self._session.start_time > MAX_DURATION_S:
                self._session.on_timeout()
                return
            text = self._transcribe_partial()
            if text:
                print(f"[STREAM] Partial: {text}")
                self._session.on_partial(text)

    def _transcribe_partial(self):
        app = self._session.app
        lock = app.model_lock
        if not lock.acquire(blocking=False):
            return None
        try:
            audio = self._snapshot_audio()
            if audio is None:
                return None
            params = self._partial_params()
            segments, _ = app.model.transcribe(audio, **params)
            text = "".join(seg.text for seg in segments).strip()
            return self._partial_cleanup(text)
        except Exception as e:
            print(f"[STREAM] Partial transcribe failed: {e}")
            return None
        finally:
            lock.release()

    def _final_pass(self):
        app = self._session.app
        try:
            with app.model_lock:
                audio = self._snapshot_audio()
                if audio is None:
                    self._session.on_final(None)
                    return
                params = self._final_params()
                t0 = time.time()
                segments, _ = app.model.transcribe(audio, **params)
                text = "".join(seg.text for seg in segments).strip()
                duration_s = len(audio) / app.model_rate
                elapsed_ms = int((time.time() - t0) * 1000)

            if not text:
                self._session.on_final(None, duration_s=duration_s,
                                       elapsed_ms=elapsed_ms)
                return

            try:
                text = app.voice_training_window.apply_corrections(text)
            except Exception:
                pass

            try:
                text = app.process_transcription(text)
            except Exception:
                pass

            raw = text
            cleaned = clean_text(text,
                                 mode=app.config.get('cleanup_mode', 'clean'))
            if app.config.get('add_trailing_space', True):
                cleaned = cleaned + " "
            self._session.on_final(cleaned, raw_text=raw,
                                   duration_s=duration_s,
                                   elapsed_ms=elapsed_ms)
        except Exception as e:
            print(f"[STREAM] Final pass error: {e}")
            self._session.on_final(None)

    def _snapshot_audio(self):
        app = self._session.app
        chunks = list(app.audio_data)
        if not chunks:
            return None
        try:
            audio = np.concatenate(chunks, axis=0).flatten()
        except ValueError:
            return None
        if audio.size == 0:
            return None
        if audio.size / app.capture_rate < MIN_PARTIAL_AUDIO_S:
            return None
        from dictation import resample_audio
        return resample_audio(audio, app.capture_rate, app.model_rate)

    def _partial_params(self):
        app = self._session.app
        try:
            prompt = app.voice_training_window.get_initial_prompt()
        except Exception:
            prompt = None
        return {
            'language': app.config.get('language', 'en'),
            'initial_prompt': prompt,
            'beam_size': PARTIAL_BEAM,
            'vad_filter': False,
            'no_speech_threshold': NO_SPEECH_THRESHOLD,
            'log_prob_threshold': LOG_PROB_THRESHOLD,
            'condition_on_previous_text': False,
            'without_timestamps': True,
            'word_timestamps': False,
            'temperature': 0.0,
        }

    def _final_params(self):
        app = self._session.app
        try:
            params = app.get_transcription_params()
        except Exception:
            params = {
                'language': app.config.get('language', 'en'),
                'initial_prompt': None,
            }
        params = dict(params)
        params['beam_size'] = FINAL_BEAM
        params['vad_filter'] = False
        return params

    def _partial_cleanup(self, text):
        if not text:
            return text
        # Capitalize the first character only -- no filler removal so
        # words don't disappear mid-sentence in the overlay.
        for i, ch in enumerate(text):
            if ch.isalpha():
                return text[:i] + ch.upper() + text[i + 1:]
        return text


class StreamingSession:
    """Owns one streaming dictation: overlay + worker + state machine.

    States: IDLE -> RECORDING -> STREAMING -> FINALIZING -> PASTING -> IDLE
    Construct in 'hold' mode when config['streaming_mode'] is True. The
    app calls start() right after the audio stream is up, finalize() on
    hotkey release, cancel() on ESC.
    """

    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_STREAMING = "streaming"
    STATE_FINALIZING = "finalizing"
    STATE_PASTING = "pasting"
    STATE_DONE = "done"

    def __init__(self, app):
        self.app = app
        self.start_time = time.time()
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self._state = self.STATE_RECORDING
        self._state_lock = threading.Lock()
        self._direct_paste = bool(
            app.config.get('streaming_direct_paste', False))
        # Whether the last direct-paste operation actually pasted
        # something. Drives the Ctrl+Z undo before each new paste so we
        # replace only our most recent partial, never pre-existing
        # content. Reset to False at session start and after the final
        # replacement.
        self._last_pasted = False
        # Foreground window captured at session creation. We bail out
        # of direct paste if focus changes mid-stream so we don't type
        # into a different app.
        self._target_hwnd = None
        if sys.platform == "win32":
            try:
                self._target_hwnd = _user32.GetForegroundWindow()
            except Exception:
                self._target_hwnd = None
        # Serializes select+paste between the worker thread (partials),
        # the Tk thread (final), and the cancel undo thread.
        self._paste_lock = threading.Lock()
        self._overlay = StreamingOverlay(app.root, dim=self._direct_paste)
        self._worker = StreamingWorker(self)
        self._last_partial = ""

    # ---- Public lifecycle (call from main thread) -----------------------

    def start(self):
        """Begin partial loop. Audio stream must already be running."""
        self.app.root.after(0, self._overlay.show)
        self._worker.start()
        with self._state_lock:
            self._state = self.STATE_STREAMING

    def finalize(self):
        """Hotkey released: worker will run final pass + paste."""
        with self._state_lock:
            if self._state in (self.STATE_FINALIZING, self.STATE_PASTING,
                               self.STATE_DONE):
                return
            self._state = self.STATE_FINALIZING
        self.stop_event.set()

    def cancel(self):
        """Dismiss overlay, no paste, no history. In direct-paste mode,
        also delete whatever partials were typed into the focused app."""
        with self._state_lock:
            if self._state in (self.STATE_DONE,):
                return
            self._state = self.STATE_DONE
        self.cancel_event.set()
        self.stop_event.set()
        if self._direct_paste and self._last_pasted:
            threading.Thread(target=self._undo_direct_paste,
                             daemon=True,
                             name="streaming-cancel-undo").start()
        self.app.root.after(0, self._overlay.close)

    # ---- Worker -> session callbacks (called from worker thread) --------

    def on_partial(self, text):
        self._last_partial = text
        self.app.root.after(0, self._overlay.update_text, text,
                            StreamingOverlay.STATE_PROCESSING)
        if self._direct_paste and text:
            # Runs on the worker thread -- pyautogui + clipboard ops are
            # blocking and must not run on the Tk main thread.
            self._direct_paste_partial(text)

    def on_timeout(self):
        print("[STREAM] Max duration reached -- auto-finalizing")
        # Tell the app to stop the audio stream too -- mirror hotkey release.
        self.app.root.after(0, self._on_timeout_main)

    def on_cancelled(self):
        self.app.root.after(0, self._overlay.close)

    def on_final(self, final_text, raw_text=None, duration_s=0.0,
                 elapsed_ms=0):
        self.app.root.after(0, self._deliver_final, final_text, raw_text,
                            duration_s, elapsed_ms)

    # ---- Main-thread handlers -------------------------------------------

    def _on_timeout_main(self):
        try:
            if self.app.recording:
                self.app.stop_recording()
        except Exception as e:
            print(f"[STREAM] Auto-stop after timeout failed: {e}")

    def _deliver_final(self, text, raw_text, duration_s, elapsed_ms):
        with self._state_lock:
            self._state = self.STATE_PASTING
        if not text or not text.strip():
            print("[STREAM] No speech detected")
            if duration_s > MIN_PARTIAL_AUDIO_S:
                try:
                    self.app._log_history(
                        raw_text="",
                        display_text="(no speech detected)",
                        duration_ms=int(duration_s * 1000),
                        mode="streaming",
                        status="empty",
                    )
                except Exception:
                    pass
            # In direct-paste mode, the user has partial text typed into
            # the target app -- erase it on no-speech so we leave a clean
            # slate. Off-thread to avoid blocking Tk.
            if self._direct_paste and self._last_pasted:
                threading.Thread(target=self._undo_direct_paste,
                                 daemon=True,
                                 name="streaming-empty-undo").start()
            self._overlay.update_text("(no speech)",
                                      StreamingOverlay.STATE_DONE)
            self._overlay.flash_done_and_fade(self._mark_done)
            return

        # Update overlay to show final text.
        self._overlay.update_text(text.rstrip(),
                                  StreamingOverlay.STATE_DONE)
        print(f"[OK] {text}")
        try:
            self.app.play_sound("success")
        except Exception:
            pass
        try:
            self.app.add_to_history(text.strip(), is_command=False)
        except Exception as e:
            print(f"[STREAM] add_to_history failed: {e}")
        try:
            self.app._log_history(
                raw_text=raw_text if raw_text is not None else text,
                display_text=text.strip(),
                duration_ms=int(duration_s * 1000),
                mode="streaming",
                status="success",
            )
        except Exception:
            pass
        try:
            self.app._notify_main_window(text.strip())
        except Exception:
            pass

        if self.app.config.get('auto_paste', True):
            if self._direct_paste:
                # Replace the partials already in the target app with the
                # cleaned final text. Off-thread because backspace + paste
                # is blocking and we are on the Tk main thread here.
                threading.Thread(target=self._direct_paste_final,
                                 args=(text,),
                                 daemon=True,
                                 name="streaming-final-paste").start()
            else:
                self._paste_with_retry(text)

        self._overlay.flash_done_and_fade(self._mark_done)

    def _paste_with_retry(self, text):
        try:
            self.app._paste_preserving_clipboard(text)
            return
        except Exception as e:
            print(f"[STREAM] Paste failed once: {e} -- retrying in 100ms")
        self.app.root.after(100, self._paste_retry_then_clipboard, text)

    def _paste_retry_then_clipboard(self, text):
        try:
            self.app._paste_preserving_clipboard(text)
        except Exception as e:
            print(f"[STREAM] Paste retry failed: {e}")
            try:
                import pyperclip
                pyperclip.copy(text)
                print("[STREAM] Text copied to clipboard -- paste manually")
            except Exception:
                pass

    def _mark_done(self):
        with self._state_lock:
            self._state = self.STATE_DONE

    # ---- Direct-paste helpers (off the Tk main thread) ------------------

    def _direct_paste_partial(self, text):
        """Replace the previously pasted partial with `text` via Ctrl+Z
        undo + Ctrl+V paste. Runs on the worker thread between
        transcribe iterations.

        The CapsLock streaming hotkey does not produce held Ctrl/Shift,
        so the synthesized Ctrl+Z and Ctrl+V chords land cleanly. The
        target app's per-paste undo entry (Notepad / RichEdit / IDEs)
        is what makes this safe -- pre-existing content stays intact
        because we only undo our own paste."""
        text = text.replace('\n', ' ').replace('\r', ' ')
        with self._paste_lock:
            if self.cancel_event.is_set():
                return
            if not self._focus_unchanged():
                self._last_pasted = False
                self.cancel_event.set()
                print("[STREAM] Focus changed -- aborting direct paste")
                return
            try:
                if self._last_pasted:
                    pyautogui.hotkey('ctrl', 'z')
                    time.sleep(UNDO_SETTLE_S)
                self.app._paste_preserving_clipboard(text)
                self._last_pasted = True
                if PASTE_SETTLE_S:
                    time.sleep(PASTE_SETTLE_S)
                print(f"[STREAM] Direct paste: {len(text.split())} words")
            except Exception as e:
                print(f"[STREAM] direct partial paste failed: {e}")

    def _direct_paste_final(self, text):
        """Replace the last partial with the cleaned final text.

        Daemon thread, runs after the user released the hotkey. We
        still release any lingering Ctrl/Shift via SendInput as a
        belt-and-braces guard against a brief release-mid-flight race;
        the safety net is harmless when nothing is held. We never
        re-press the modifiers afterwards -- a synthetic down without a
        matching up corrupts OS state."""
        sanitized = text.replace('\n', ' ').replace('\r', ' ')
        with self._paste_lock:
            _release_held_modifiers()
            try:
                if self._last_pasted:
                    pyautogui.hotkey('ctrl', 'z')
                    time.sleep(UNDO_SETTLE_S)
                self.app._paste_preserving_clipboard(sanitized)
                self._last_pasted = False
                if PASTE_SETTLE_S:
                    time.sleep(PASTE_SETTLE_S)
                print(f"[STREAM] Direct paste (final): "
                      f"{len(sanitized.split())} words")
            except Exception as e:
                print(f"[STREAM] direct final paste failed: {e}")
                # Fallback: leave the cleaned text on the clipboard so
                # the user can paste it manually.
                if pyperclip is not None:
                    try:
                        pyperclip.copy(sanitized)
                        print("[STREAM] Text copied to clipboard "
                              "-- paste manually")
                    except Exception:
                        pass

    def _undo_direct_paste(self):
        """Cancel: undo the last partial paste so only our text is
        removed and pre-existing content stays intact."""
        with self._paste_lock:
            if not self._last_pasted:
                return
            _release_held_modifiers()
            try:
                pyautogui.hotkey('ctrl', 'z')
                self._last_pasted = False
                print("[STREAM] Direct paste (undo): cleared")
            except Exception as e:
                print(f"[STREAM] direct paste undo failed: {e}")

    def _focus_unchanged(self):
        """True if the focused window matches the one captured at
        session start. Always True on non-Windows or if we couldn't
        capture the handle -- focus tracking is best-effort."""
        if sys.platform != "win32" or self._target_hwnd is None:
            return True
        try:
            return _user32.GetForegroundWindow() == self._target_hwnd
        except Exception:
            return True
