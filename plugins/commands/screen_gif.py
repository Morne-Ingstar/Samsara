"""Screen-to-GIF recording plugin.

Say "Samsara, record my screen" to start capturing. Say "Samsara, stop
recording" to stop and save the GIF. Optionally specify a duration:
"Samsara, record a gif for 5 seconds".

Captures the full screen at ~10fps, assembles into an animated GIF,
saves to ~/Downloads/ with a timestamp filename.

Trigger phrases:
  "record my screen"  / "start recording"  / "record a gif"
  "make a gif"        / "capture my screen" / "screen record"
  "stop recording"    / "done recording"    / "save the gif"

Dependencies: Pillow (PIL) — already installed via Whisper/faster-whisper.
"""

import threading
import time
import os
from pathlib import Path
from datetime import datetime

from samsara.plugin_commands import command


# Recording state
_recording = False
_frames = []
_record_thread = None
_lock = threading.Lock()

# Settings
_FPS = 10          # capture rate
_MAX_DURATION = 30  # seconds, safety cap
_SCALE = 0.5       # downscale factor (1.0 = full res, 0.5 = half)
_OUTPUT_DIR = Path.home() / "Downloads"


def _capture_loop(duration=None):
    """Background thread: capture screenshots until stopped or duration expires."""
    global _recording, _frames
    
    try:
        from PIL import ImageGrab
    except ImportError:
        print("[GIF] Pillow not installed — cannot capture screen")
        _recording = False
        return
    
    start_time = time.time()
    interval = 1.0 / _FPS
    
    print(f"[GIF] Recording at {_FPS}fps (scale: {_SCALE}x)...")
    
    while _recording:
        # Check duration cap
        elapsed = time.time() - start_time
        if duration and elapsed >= duration:
            print(f"[GIF] Duration reached ({duration}s)")
            break
        if elapsed >= _MAX_DURATION:
            print(f"[GIF] Max duration reached ({_MAX_DURATION}s)")
            break
        
        # Capture screenshot
        try:
            frame = ImageGrab.grab()
            
            # Downscale for smaller file size
            if _SCALE < 1.0:
                new_size = (int(frame.width * _SCALE), int(frame.height * _SCALE))
                frame = frame.resize(new_size, resample=1)  # LANCZOS
            
            with _lock:
                _frames.append(frame)
        except Exception as e:
            print(f"[GIF] Capture error: {e}")
        
        # Sleep to maintain target FPS
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
        # Convert to palette mode for smaller GIF
        converted = []
        for f in frames:
            converted.append(f.convert('P', palette=0, colors=256))
        
        converted[0].save(
            str(output_path),
            save_all=True,
            append_images=converted[1:],
            duration=int(1000 / _FPS),  # ms per frame
            loop=0,  # infinite loop
            optimize=True,
        )
        
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"[GIF] Saved: {output_path} ({size_mb:.1f}MB, {len(frames)} frames)")
        
        # Open the file
        if os.name == 'nt':
            os.startfile(str(output_path))
        
    except Exception as e:
        print(f"[GIF] Save failed: {e}")
    finally:
        with _lock:
            _frames = []


def _parse_duration_from_remainder(text):
    """Extract duration in seconds from 'for 5 seconds' / 'for 10 sec'."""
    import re
    if not text:
        return None
    match = re.search(r'(\d+)\s*(?:seconds?|secs?|s)\b', text.lower())
    if match:
        return int(match.group(1))
    match = re.search(r'(\d+)\s*(?:minutes?|mins?|m)\b', text.lower())
    if match:
        return int(match.group(1)) * 60
    # Bare number = seconds
    match = re.search(r'for\s+(\d+)', text.lower())
    if match:
        return int(match.group(1))
    return None


@command("record my screen", aliases=[
    "start recording", "record a gif", "make a gif",
    "capture my screen", "screen record",
    "record this", "make a gif of this",
    "start screen recording", "record screen"
])
def handle_start_recording(app, remainder):
    """Start screen recording. Say 'stop recording' when done."""
    global _recording, _frames, _record_thread
    
    if _recording:
        print("[GIF] Already recording — say 'stop recording' to finish")
        return True
    
    duration = _parse_duration_from_remainder(remainder)
    
    with _lock:
        _frames = []
    _recording = True
    
    _record_thread = threading.Thread(
        target=_capture_loop,
        args=(duration,),
        daemon=True,
        name="gif-recorder"
    )
    _record_thread.start()
    
    if duration:
        print(f"[GIF] Recording for {duration}s — will auto-save when done")
    else:
        print("[GIF] Recording started — say 'Samsara, stop recording' when done")
    
    # Update indicator if available
    if hasattr(app, 'listening_indicator'):
        try:
            label = f"Recording GIF{f' ({duration}s)' if duration else ''}..."
            app._schedule_ui(app.listening_indicator.set_mode, label)
        except Exception:
            pass
    
    # Play start sound
    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("start")
        except Exception:
            pass
    
    return True


@command("stop recording", aliases=[
    "done recording", "save the gif", "finish recording",
    "end recording", "stop screen recording",
    "stop the recording", "that's enough"
])
def handle_stop_recording(app, remainder):
    """Stop screen recording and save the GIF."""
    global _recording
    
    if not _recording:
        print("[GIF] Not currently recording")
        return True
    
    _recording = False
    print("[GIF] Stopping...")
    
    # Play stop sound
    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("stop")
        except Exception:
            pass
    
    # Reset indicator
    if hasattr(app, '_indicator_reset'):
        try:
            app._indicator_reset()
        except Exception:
            pass
    
    return True
