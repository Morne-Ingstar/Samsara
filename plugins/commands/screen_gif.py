"""Screen-to-GIF recording plugin.

Say "Samsara, record my screen" for full screen capture.
Say "Samsara, record this window" for active window only.
Say "Samsara, stop recording" to save.

Changes from v1:
- Uses mss (fast DXGI capture) instead of PIL.ImageGrab (slow)
- Active window capture via win32gui
- Persistent red "REC" indicator while recording (privacy safety)
- Distinct triggers from gif_search.py (no "make a gif" overlap)
"""

import threading
import time
import os
import sys
from pathlib import Path
from datetime import datetime

from samsara.plugin_commands import command


# Recording state
_recording = False
_frames = []
_record_thread = None
_lock = threading.Lock()
_indicator_window = None

# Settings
_FPS = 10
_MAX_DURATION = 30
_SCALE = 0.5
_OUTPUT_DIR = Path.home() / "Downloads"


def _get_capture_engine():
    """Return the best available capture engine.
    
    Priority: mss (fast DXGI) > PIL.ImageGrab (slow fallback).
    """
    try:
        import mss
        return 'mss'
    except ImportError:
        pass
    try:
        from PIL import ImageGrab
        return 'pil'
    except ImportError:
        pass
    return None


def _get_active_window_bounds():
    """Get the bounding box of the active (foreground) window.
    
    Returns dict with top/left/width/height for mss, or None.
    """
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        
        if width > 0 and height > 0:
            return {
                "top": rect.top, "left": rect.left,
                "width": width, "height": height
            }
    except Exception:
        pass
    return None


def _show_recording_indicator():
    """Show a persistent always-on-top red REC indicator.
    
    Privacy safety: user always knows recording is active.
    Runs on a daemon thread with its own Tk root.
    """
    global _indicator_window
    try:
        import tkinter as tk
        
        indicator = tk.Tk()
        indicator.overrideredirect(True)
        indicator.attributes('-topmost', True)
        indicator.attributes('-alpha', 0.85)
        indicator.geometry('80x30+10+10')
        indicator.configure(bg='#cc0000')
        
        label = tk.Label(indicator, text='\u25CF REC', fg='white',
                         bg='#cc0000', font=('Arial', 12, 'bold'))
        label.pack(expand=True)
        
        _indicator_window = indicator
        
        def _pulse():
            if not _recording:
                indicator.destroy()
                return
            # Pulse opacity
            current = indicator.attributes('-alpha')
            new_alpha = 0.5 if current > 0.7 else 0.85
            indicator.attributes('-alpha', new_alpha)
            indicator.after(500, _pulse)
        
        indicator.after(500, _pulse)
        indicator.mainloop()
    except Exception as e:
        print(f"[GIF] Indicator error: {e}")


def _capture_loop(duration=None, window_only=False):
    """Background thread: capture screen until stopped."""
    global _recording, _frames
    from PIL import Image
    
    engine = _get_capture_engine()
    if not engine:
        print("[GIF] No capture engine available (install mss or Pillow)")
        _recording = False
        return
    
    # Get window bounds if window-only mode
    bounds = None
    if window_only:
        bounds = _get_active_window_bounds()
        if bounds:
            print(f"[GIF] Capturing window: {bounds['width']}x{bounds['height']}")
        else:
            print("[GIF] Could not detect active window — recording full screen")
    
    start_time = time.time()
    interval = 1.0 / _FPS
    mode_str = "window" if bounds else "full screen"
    print(f"[GIF] Recording {mode_str} at {_FPS}fps (scale: {_SCALE}x)...")
    
    # Start recording indicator on separate thread
    indicator_thread = threading.Thread(
        target=_show_recording_indicator, daemon=True, name="rec-indicator")
    indicator_thread.start()
    
    if engine == 'mss':
        import mss
        with mss.mss() as sct:
            while _recording:
                elapsed = time.time() - start_time
                if duration and elapsed >= duration:
                    print(f"[GIF] Duration reached ({duration}s)")
                    break
                if elapsed >= _MAX_DURATION:
                    print(f"[GIF] Max duration reached ({_MAX_DURATION}s)")
                    break
                
                try:
                    region = bounds or sct.monitors[1]  # primary monitor
                    shot = sct.grab(region)
                    frame = Image.frombytes('RGB', shot.size,
                                            shot.bgra, 'raw', 'BGRX')
                    if _SCALE < 1.0:
                        new_size = (int(frame.width * _SCALE),
                                    int(frame.height * _SCALE))
                        frame = frame.resize(new_size, resample=1)
                    with _lock:
                        _frames.append(frame)
                except Exception as e:
                    print(f"[GIF] Capture error: {e}")
                
                time.sleep(max(0, interval - (time.time() - start_time) % interval))
    
    else:  # PIL fallback
        from PIL import ImageGrab
        while _recording:
            elapsed = time.time() - start_time
            if duration and elapsed >= duration:
                break
            if elapsed >= _MAX_DURATION:
                break
            try:
                if bounds:
                    bbox = (bounds['left'], bounds['top'],
                            bounds['left'] + bounds['width'],
                            bounds['top'] + bounds['height'])
                    frame = ImageGrab.grab(bbox=bbox)
                else:
                    frame = ImageGrab.grab()
                if _SCALE < 1.0:
                    new_size = (int(frame.width * _SCALE),
                                int(frame.height * _SCALE))
                    frame = frame.resize(new_size, resample=1)
                with _lock:
                    _frames.append(frame)
            except Exception as e:
                print(f"[GIF] Capture error: {e}")
            time.sleep(max(0, interval - (time.time() - start_time) % interval))
    
    _recording = False
    frame_count = len(_frames)
    print(f"[GIF] Stopped. {frame_count} frames captured.")
    if frame_count > 0:
        _save_gif()


def _save_gif():
    """Assemble captured frames into an animated GIF."""
    global _frames
    
    with _lock:
        frames = list(_frames)
    
    if not frames:
        print("[GIF] No frames to save")
        return
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"samsara_recording_{timestamp}.gif"
    output_path = _OUTPUT_DIR / filename
    
    print(f"[GIF] Saving {len(frames)} frames to {output_path}...")
    
    try:
        converted = []
        for f in frames:
            converted.append(f.convert('P', palette=0, colors=256))
        
        converted[0].save(
            str(output_path), save_all=True,
            append_images=converted[1:],
            duration=int(1000 / _FPS), loop=0, optimize=True,
        )
        
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"[GIF] Saved: {output_path} ({size_mb:.1f}MB, {len(frames)} frames)")
        if os.name == 'nt':
            os.startfile(str(output_path))
    except Exception as e:
        print(f"[GIF] Save failed: {e}")
    finally:
        with _lock:
            _frames = []


def _parse_duration(text):
    """Extract seconds from 'for 5 seconds' / 'for 10 sec'."""
    import re
    if not text:
        return None
    match = re.search(r'(\d+)\s*(?:seconds?|secs?|s)\b', text.lower())
    if match:
        return int(match.group(1))
    match = re.search(r'(\d+)\s*(?:minutes?|mins?|m)\b', text.lower())
    if match:
        return int(match.group(1)) * 60
    match = re.search(r'for\s+(\d+)', text.lower())
    if match:
        return int(match.group(1))
    return None


def _start_recording(app, remainder, window_only=False):
    """Shared logic for starting a screen recording."""
    global _recording, _frames, _record_thread
    
    if _recording:
        print("[GIF] Already recording — say 'stop recording' to finish")
        return True
    
    duration = _parse_duration(remainder)
    
    with _lock:
        _frames.clear()
    _recording = True
    
    _record_thread = threading.Thread(
        target=_capture_loop,
        args=(duration, window_only),
        daemon=True, name="gif-recorder"
    )
    _record_thread.start()
    
    mode = "window" if window_only else "screen"
    dur_str = f" for {duration}s" if duration else ""
    print(f"[GIF] Recording {mode}{dur_str}")
    
    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("start")
        except Exception:
            pass
    return True


# --- Full screen recording ---
# Start triggers are deliberately limited to record/capture verbs so they
# can't overlap with gif_search.py's search/find verbs.
@command("record my screen", aliases=[
    "start recording", "record a gif",
    "capture my screen", "screen record",
    "record screen",
])
def handle_record_screen(app, remainder):
    """Record full screen as GIF. Usage: 'Samsara, record my screen'"""
    return _start_recording(app, remainder, window_only=False)


# --- Active window recording ---
@command("record this window", aliases=[
    "capture this window", "record window",
    "record this", "capture this",
])
def handle_record_window(app, remainder):
    """Record active window as GIF. Usage: 'Samsara, record this window'"""
    return _start_recording(app, remainder, window_only=True)


# --- Stop recording ---
@command("stop recording", aliases=[
    "done recording", "save the gif", "finish recording",
    "end recording", "stop screen recording",
    "stop the recording"
])
def handle_stop_recording(app, remainder):
    """Stop screen recording and save the GIF."""
    global _recording
    
    if not _recording:
        print("[GIF] Not currently recording")
        return True
    
    _recording = False
    print("[GIF] Stopping...")
    
    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("stop")
        except Exception:
            pass
    
    if hasattr(app, '_indicator_reset'):
        try:
            app._indicator_reset()
        except Exception:
            pass
    
    return True
