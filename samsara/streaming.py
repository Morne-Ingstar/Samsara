"""Streaming dictation: live partial transcription with overlay.

Architecture (ACE-04B):

  - Audio capture: DictationSessionConsumer drain thread fills _streaming_frames
    from the ACE engine ring. activate_streaming() starts the drain thread;
    stop_streaming() stops it and returns all accumulated audio.
  - StreamingSession owns the lifecycle: spawn worker, manage overlay,
    finalize on hotkey release.
  - StreamingWorker (daemon thread) calls snapshot_streaming_audio() every
    ~1.5s for non-destructive partial snapshots, runs Whisper at beam_size=1,
    posts overlay updates via Qt signals.
  - On stop_event the worker runs a final beam_size=5 pass with full
    Grammar-Lite cleanup, then posts paste + overlay close to the Qt thread.

Latency budget (for 'hold' streaming):

  pre-buffer skipped + 1.0s first chunk + ~0.3s transcribe = ~1.3s
  to first partial. Subsequent partials at ~1.5s cadence.

Locks:

  - app.model_lock (existing): serializes Whisper calls. Acquired
    non-blocking for partials -- if held, skip and try next interval.
  - _streaming_lock in DictationSessionConsumer: protects _streaming_frames
    for concurrent access between the drain thread and snapshot_streaming_audio().
"""

import ctypes
import sys
import threading
import time

import numpy as np
import pyautogui

try:
    import pyperclip
except ImportError:
    pyperclip = None

from samsara.cleanup import clean_text
from samsara.log import get_logger
from samsara.runtime import thread_registry
from samsara.smart_corrections import smart_correct
from samsara import diagnostics
from samsara import languages as _languages

logger = get_logger(__name__)


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
            logger.debug(f"[STREAM] SendInput vk={vk:#x} failed: {e}")

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
            logger.debug(f"[STREAM] release_held_modifiers failed: {e}")
        return held

    def _press_modifiers(vks):
        if not vks:
            return
        try:
            for vk in vks:
                _send_key_event(vk, key_up=False)
        except Exception as e:
            logger.debug(f"[STREAM] press_modifiers failed: {e}")

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


# ---------------------------------------------------------------------------
# Qt overlay
# ---------------------------------------------------------------------------

class _StreamingWidget:
    """Internal Qt widget — created on the samsara-qt thread."""

    def __init__(self, dim: bool):
        from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QApplication
        from PySide6.QtCore import Qt, QTimer, Signal, Slot
        from PySide6.QtGui import QFont

        # Inline QWidget subclass so we can define Signals
        class _W(QWidget):
            _update_sig = Signal(str, str)
            _flash_sig  = Signal(object)
            _close_sig  = Signal()

            def __init__(self, dim, parent=None):
                super().__init__(
                    parent,
                    Qt.WindowType.FramelessWindowHint |
                    Qt.WindowType.WindowStaysOnTopHint |
                    Qt.WindowType.Tool,
                )
                self._dim = dim
                self._fade_alpha = DIM_ALPHA if dim else ALPHA
                self._on_complete = None

                self._fade_timer = QTimer(self)
                self._fade_timer.setInterval(40)
                self._fade_timer.timeout.connect(self._fade_step)

                lay = QHBoxLayout(self)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(0)

                self._border = QWidget()
                self._border.setFixedWidth(4)
                self._border.setStyleSheet(f"background:{LISTENING_BORDER};")
                lay.addWidget(self._border)

                content = QWidget()
                content.setStyleSheet(f"background:{BG_COLOR};")
                cLay = QVBoxLayout(content)
                cLay.setContentsMargins(8, 10, 12, 10)
                fs = DIM_FONT_SIZE if dim else FONT_SIZE
                init = "Listening (direct paste)..." if dim else "Listening..."
                self._label = QLabel(init)
                self._label.setWordWrap(True)
                self._label.setStyleSheet(
                    f"color:{TEXT_COLOR};font-size:{fs}px;"
                    f"font-family:'{FONT_FAMILY}';background:transparent;"
                )
                self._label.setMinimumWidth(OVERLAY_W - 40)
                self._label.setMaximumWidth(OVERLAY_W - 40)
                cLay.addWidget(self._label)
                lay.addWidget(content, stretch=1)

                self.setFixedWidth(OVERLAY_W)
                self.setWindowOpacity(DIM_ALPHA if dim else ALPHA)

                self._update_sig.connect(self._on_update)
                self._flash_sig.connect(self._on_flash)
                self._close_sig.connect(self._on_close)

            def show_overlay(self):
                self._border.setStyleSheet(f"background:{LISTENING_BORDER};")
                self._fade_timer.stop()
                self.setWindowOpacity(DIM_ALPHA if self._dim else ALPHA)
                self._fade_alpha = DIM_ALPHA if self._dim else ALPHA
                self._position()
                self.show()
                self.raise_()

            def _position(self):
                scr = QApplication.primaryScreen().availableGeometry()
                hint_h = self._label.heightForWidth(OVERLAY_W - 40)
                req_h  = max(OVERLAY_MIN_H,
                             min(OVERLAY_MAX_H, hint_h + 20))
                self.setFixedHeight(req_h)
                x = scr.left() + (scr.width() - OVERLAY_W) // 2
                y = scr.bottom() - TASKBAR_RESERVE - OVERLAY_GAP_ABOVE_TASKBAR - req_h
                self.move(x, y)

            def _on_update(self, text, state):
                self._label.setText(text)
                if state == "done":
                    self._border.setStyleSheet(f"background:{DONE_BORDER};")
                elif state:
                    self._border.setStyleSheet(f"background:{LISTENING_BORDER};")
                self._position()

            def _on_flash(self, on_complete):
                self._on_complete = on_complete
                self._border.setStyleSheet(f"background:{DONE_BORDER};")
                self._fade_alpha = DIM_ALPHA if self._dim else ALPHA
                self._fade_timer.start()

            def _on_close(self):
                self._fade_timer.stop()
                self.hide()
                cb, self._on_complete = self._on_complete, None
                if cb:
                    cb()

            def _fade_step(self):
                self._fade_alpha -= 0.08
                if self._fade_alpha <= 0.05:
                    self._fade_timer.stop()
                    self.hide()
                    cb, self._on_complete = self._on_complete, None
                    if cb:
                        cb()
                    return
                self.setWindowOpacity(max(0.0, self._fade_alpha))

        self._w = _W(dim)

    def show_overlay(self):      self._w.show_overlay()
    def update(self, text, st):  self._w._update_sig.emit(text, st or "")
    def flash(self, cb):         self._w._flash_sig.emit(cb)
    def close(self):             self._w._close_sig.emit()


class StreamingOverlayQt:
    """Thread-safe Qt drop-in for StreamingOverlay.

    Public API is identical.  Widget is created lazily on the samsara-qt
    thread on first show() so it never touches Qt from the Tk main thread.
    """

    STATE_LISTENING  = "listening"
    STATE_PROCESSING = "processing"
    STATE_DONE       = "done"

    def __init__(self, dim: bool = False):
        self._dim    = dim
        self._widget: "_StreamingWidget | None" = None

    def _ensure(self):
        """Create the widget on the Qt thread if not already done."""
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        qt_app = QApplication.instance()
        if qt_app is None or self._widget is not None:
            return
        def _make():
            self._widget = _StreamingWidget(self._dim)
        QTimer.singleShot(0, qt_app, _make)

    def show(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        qt_app = QApplication.instance()
        if qt_app is None:
            return
        def _show():
            if self._widget is None:
                self._widget = _StreamingWidget(self._dim)
            self._widget.show_overlay()
        QTimer.singleShot(0, qt_app, _show)

    def update_text(self, text, state=None):
        if self._widget is not None:
            self._widget.update(text or "", state or "")

    def flash_done_and_fade(self, on_complete):
        if self._widget is not None:
            self._widget.flash(on_complete)
        elif on_complete:
            on_complete()

    def close(self):
        if self._widget is not None:
            self._widget.close()


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
            logger.exception(f"[STREAM] Worker partial loop crashed: {e}")
        if self._cancel_event.is_set():
            self._session.on_cancelled()
            return
        try:
            self._final_pass()
        except Exception as e:
            logger.exception(f"[STREAM] Final pass crashed: {e}")
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
            # Cancellation can arrive while Whisper owns model_lock. Never
            # publish that stale partial after cancel() has hidden the overlay
            # and released the accumulator for another recording.
            if self._stop_event.is_set() or self._cancel_event.is_set():
                return
            if text:
                logger.debug(f"[STREAM] Partial: {text}")
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
            logger.exception(f"[STREAM] Partial transcribe failed: {e}")
            return None
        finally:
            lock.release()

    def _final_pass(self):
        app = self._session.app
        try:
            # Acquire final audio by stopping the accumulator.  stop_streaming()
            # stops the drain thread and returns all frames atomically -- no race
            # with dictation.py's stop_recording(), which no longer calls it.
            # Done OUTSIDE model_lock: join can take up to 2s on slow threads.
            consumer = getattr(app, '_dictation_consumer', None)
            if consumer is not None and hasattr(consumer, 'stop_streaming'):
                audio = self._session._stop_capture()
            else:
                audio = self._snapshot_audio()   # non-ACE fallback

            if audio is None or audio.size == 0:
                self._session.on_final(None)
                return

            with app.model_lock:
                params = self._final_params()
                t0 = time.time()
                segments, info = app.model.transcribe(audio, **params)
                detected_lang = getattr(info, 'language', None)
                seg_list = list(segments)
                text = "".join(seg.text for seg in seg_list).strip()
                duration_s = len(audio) / app.model_rate
                elapsed_ms = int((time.time() - t0) * 1000)

            try:
                diag_sig = diagnostics.segment_signals(seg_list)
            except Exception as e:
                logger.debug(f"[STREAM] segment signal extraction failed: {e}")
                diag_sig = {}

            if not text:
                self._session.on_final(None, duration_s=duration_s,
                                       elapsed_ms=elapsed_ms)
                return

            diag_corr_start = time.perf_counter()

            try:
                text = app.voice_training_window.apply_corrections(text)
            except Exception as e:
                logger.debug(f"[STREAM] apply_corrections failed: {e}")

            try:
                text = app.process_transcription(text)
            except Exception as e:
                logger.debug(f"[STREAM] process_transcription failed: {e}")

            raw = text
            _cmode = 'verbatim' if getattr(app, '_skip_cleanup', False) else app.config.get('cleanup_mode', 'clean')
            cleaned = clean_text(text, mode=_cmode)
            t_corrections_ms = int((time.perf_counter() - diag_corr_start) * 1000)

            # Smart Corrections (optional LLM cleanup pass) -- streaming
            # gate (off by default; latency-sensitive path). Never blocks
            # output on failure (see smart_correct docs).
            t_smart_ms = -1
            smart_changed = False
            if app.config.get('smart_corrections', {}).get('modes', {}).get('streaming', False):
                diag_smart_start = time.perf_counter()
                text_before_smart = cleaned
                try:
                    cleaned = smart_correct(cleaned, app)
                except Exception as e:
                    logger.debug(f"[STREAM] smart_correct failed: {e}")
                t_smart_ms = int((time.perf_counter() - diag_smart_start) * 1000)
                smart_changed = (cleaned != text_before_smart)

            if app.config.get('add_trailing_space', True):
                cleaned = cleaned + " "

            # Inline formatting tokens ("new line" -> \n, etc.) -- after
            # smart_correct, before delivery/history. Applies to the FINAL
            # streamed text only (see DictationApp._apply_formatting_tokens)
            # -- partials (on_partial/_direct_paste_partial, above) never
            # pass through this method and are deliberately left as raw
            # transcription text.
            cleaned = app._apply_formatting_tokens(cleaned)

            # Diagnostics record -- total measured from transcribe start to
            # just before handoff to on_final (the streaming equivalent of
            # "just before paste").
            try:
                diagnostics.record(diagnostics.DiagRecord(
                    mode="streaming",
                    audio_s=duration_s,
                    model_name=app.config.get('model_size', ''),
                    device=getattr(app, 'device_type', 'unknown'),
                    compute_type=app.config.get('compute_type', ''),
                    t_transcribe_ms=elapsed_ms,
                    t_corrections_ms=t_corrections_ms,
                    t_smart_ms=t_smart_ms,
                    t_total_ms=int((time.time() - t0) * 1000),
                    text=cleaned,
                    smart_changed=smart_changed,
                    language=_languages.describe_diagnostics_language(
                        app.config.get('language', 'en'), detected_lang,
                    ),
                    **diag_sig,
                ), app=app)
            except Exception as e:
                logger.debug(f"[STREAM] diagnostics record failed: {e}")

            self._session.on_final(cleaned, raw_text=raw,
                                   duration_s=duration_s,
                                   elapsed_ms=elapsed_ms)
        except Exception as e:
            logger.exception(f"[STREAM] Final pass error: {e}")
            self._session.on_final(None)

    def _snapshot_audio(self):
        app = self._session.app

        # ACE-04B: use the DictationSessionConsumer's streaming accumulator.
        consumer = getattr(app, '_dictation_consumer', None)
        if consumer is None or not hasattr(consumer, 'snapshot_streaming_audio'):
            return None
        audio = consumer.snapshot_streaming_audio()
        if audio is None or audio.size == 0:
            return None
        if audio.size / app.model_rate < MIN_PARTIAL_AUDIO_S:
            return None
        return audio  # already at model_rate (16kHz) — no resample needed

    def _partial_params(self):
        app = self._session.app
        try:
            prompt = app.voice_training_window.get_initial_prompt()
        except Exception as e:
            logger.debug(f"[STREAM] get_initial_prompt failed: {e}")
            prompt = None
        return {
            'language': _languages.resolve_transcribe_language(app),
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
        except Exception as e:
            logger.debug(f"[STREAM] get_transcription_params failed: {e}")
            params = {
                'language': _languages.resolve_transcribe_language(app),
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
            except Exception as e:
                logger.debug(f"[STREAM] GetForegroundWindow failed: {e}")
                self._target_hwnd = None
        # Serializes select+paste between the worker thread (partials),
        # the main Qt thread (final), and the cancel undo thread.
        self._paste_lock = threading.Lock()
        self._overlay = StreamingOverlayQt(dim=self._direct_paste)
        self._worker = StreamingWorker(self)
        self._last_partial = ""
        self._capture_cleanup_lock = threading.Lock()
        self._capture_cleaned = False
        self._finished_notified = False

    # ---- Public lifecycle (call from main thread) -----------------------

    def start(self):
        """Begin partial loop. Audio stream must already be running."""
        self._overlay.show()
        self._worker.start()
        thread_registry.register(self._worker, "streaming-worker")
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
        # A cancelled worker skips its final pass, so it would otherwise
        # never stop DictationSessionConsumer's streaming accumulator.
        self._discard_capture()
        if self._direct_paste and self._last_pasted:
            thread_registry.spawn("streaming-cancel-undo", self._undo_direct_paste,
                             daemon=True)
        self._overlay.close()
        self._notify_finished()

    # ---- Worker -> session callbacks (called from worker thread) --------

    def on_partial(self, text):
        self._last_partial = text
        self._overlay.update_text(text, StreamingOverlayQt.STATE_PROCESSING)
        if self._direct_paste and text:
            # Runs on the worker thread -- pyautogui + clipboard ops are
            # blocking and must not run on the Tk main thread.
            self._direct_paste_partial(text)

    def on_timeout(self):
        logger.info("[STREAM] Max duration reached -- auto-finalizing")
        self.app._schedule_ui(self._on_timeout_main)

    def on_cancelled(self):
        self._discard_capture()
        self._overlay.close()
        self._notify_finished()

    def on_final(self, final_text, raw_text=None, duration_s=0.0,
                 elapsed_ms=0):
        if self.cancel_event.is_set():
            self.on_cancelled()
            return
        self.app._schedule_ui(self._deliver_final, final_text, raw_text,
                              duration_s, elapsed_ms)

    # ---- Main-thread handlers -------------------------------------------

    def _on_timeout_main(self):
        try:
            if self.app.recording:
                self.app.stop_recording()
        except Exception as e:
            logger.exception(f"[STREAM] Auto-stop after timeout failed: {e}")

    def _deliver_final(self, text, raw_text, duration_s, elapsed_ms):
        with self._state_lock:
            self._state = self.STATE_PASTING
        if not text or not text.strip():
            logger.info("[STREAM] No speech detected")
            if duration_s > MIN_PARTIAL_AUDIO_S:
                try:
                    self.app._log_history(
                        raw_text="",
                        display_text="(no speech detected)",
                        duration_ms=int(duration_s * 1000),
                        mode="streaming",
                        status="empty",
                    )
                except Exception as e:
                    logger.debug(f"[STREAM] _log_history (empty) failed: {e}")
            # In direct-paste mode, the user has partial text typed into
            # the target app -- erase it on no-speech so we leave a clean
            # slate. Off-thread to avoid blocking Tk.
            if self._direct_paste and self._last_pasted:
                thread_registry.spawn("streaming-empty-undo", self._undo_direct_paste,
                                 daemon=True)
            self._overlay.update_text("(no speech)",
                                      StreamingOverlayQt.STATE_DONE)
            self._overlay.flash_done_and_fade(self._mark_done)
            return

        # Update overlay to show final text.
        self._overlay.update_text(text.rstrip(),
                                  StreamingOverlayQt.STATE_DONE)
        logger.info(f"[OK] {text}")
        try:
            self.app.play_sound("success")
        except Exception as e:
            logger.debug(f"[STREAM] play_sound('success') failed: {e}")
        try:
            self.app.add_to_history(text.strip(), is_command=False)
        except Exception as e:
            logger.exception(f"[STREAM] add_to_history failed: {e}")
        try:
            self.app._log_history(
                raw_text=raw_text if raw_text is not None else text,
                display_text=text.strip(),
                duration_ms=int(duration_s * 1000),
                mode="streaming",
                status="success",
            )
        except Exception as e:
            logger.debug(f"[STREAM] _log_history (success) failed: {e}")
        try:
            self.app._notify_main_window(text.strip())
        except Exception as e:
            logger.debug(f"[STREAM] _notify_main_window failed: {e}")

        if self.app.config.get('auto_paste', True):
            if self._direct_paste:
                thread_registry.spawn("streaming-final-paste", self._direct_paste_final,
                                 args=(text,),
                                 daemon=True)
            else:
                self._paste_with_retry(text)

        self._overlay.flash_done_and_fade(self._mark_done)

    def _paste_with_retry(self, text):
        try:
            self.app._paste_preserving_clipboard(text)
            return
        except Exception as e:
            logger.exception(f"[STREAM] Paste failed once: {e} -- retrying in 100ms")
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(100, QApplication.instance(),
                          lambda: self._paste_retry_then_clipboard(text))

    def _paste_retry_then_clipboard(self, text):
        try:
            self.app._paste_preserving_clipboard(text)
        except Exception as e:
            logger.exception(f"[STREAM] Paste retry failed: {e}")
            if pyperclip is not None:
                try:
                    pyperclip.copy(text)
                    logger.info("[STREAM] Text copied to clipboard -- paste manually")
                except Exception as e:
                    logger.exception(f"[STREAM] Clipboard copy fallback failed: {e}")

    def _mark_done(self):
        with self._state_lock:
            self._state = self.STATE_DONE
        self._notify_finished()

    def _discard_capture(self):
        """Stop and discard the shared streaming accumulator exactly once."""
        self._stop_capture()

    def _stop_capture(self):
        """Stop the accumulator once and return its audio to the final pass."""
        with self._capture_cleanup_lock:
            if self._capture_cleaned:
                return None
            self._capture_cleaned = True
        consumer = getattr(self.app, '_dictation_consumer', None)
        if consumer is not None and hasattr(consumer, 'stop_streaming'):
            try:
                return consumer.stop_streaming()
            except Exception as e:
                logger.exception(f"[STREAM] Capture cleanup failed: {e}")
        return None

    def _notify_finished(self):
        """Release app-level ownership once across repeated close paths."""
        with self._capture_cleanup_lock:
            if self._finished_notified:
                return
            self._finished_notified = True
        callback = getattr(self.app, '_on_streaming_session_finished', None)
        if callback is not None:
            callback(self)

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
                logger.warning("[STREAM] Focus changed -- aborting direct paste")
                return
            try:
                if self._last_pasted:
                    pyautogui.hotkey('ctrl', 'z')
                    time.sleep(UNDO_SETTLE_S)
                self.app._paste_preserving_clipboard(text)
                self._last_pasted = True
                if PASTE_SETTLE_S:
                    time.sleep(PASTE_SETTLE_S)
                logger.debug(f"[STREAM] Direct paste: {len(text.split())} words")
            except Exception as e:
                logger.exception(f"[STREAM] direct partial paste failed: {e}")

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
                logger.debug(f"[STREAM] Direct paste (final): "
                             f"{len(sanitized.split())} words")
            except Exception as e:
                logger.exception(f"[STREAM] direct final paste failed: {e}")
                # Fallback: leave the cleaned text on the clipboard so
                # the user can paste it manually.
                if pyperclip is not None:
                    try:
                        pyperclip.copy(sanitized)
                        logger.info("[STREAM] Text copied to clipboard "
                                    "-- paste manually")
                    except Exception as e:
                        logger.exception(f"[STREAM] Clipboard copy fallback failed: {e}")

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
                logger.info("[STREAM] Direct paste (undo): cleared")
            except Exception as e:
                logger.exception(f"[STREAM] direct paste undo failed: {e}")

    def _focus_unchanged(self):
        """True if the focused window matches the one captured at
        session start. Always True on non-Windows or if we couldn't
        capture the handle -- focus tracking is best-effort."""
        if sys.platform != "win32" or self._target_hwnd is None:
            return True
        try:
            return _user32.GetForegroundWindow() == self._target_hwnd
        except Exception as e:
            logger.debug(f"[STREAM] GetForegroundWindow check failed: {e}")
            return True
