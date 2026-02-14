"""Persistent alarm/reminder system with sound notifications and gamification.

Unlike the NotificationManager which uses toast notifications,
this AlarmManager plays sounds repeatedly until dismissed via hotkey.

Features:
- Interval-based alarms (e.g., every 60 minutes)
- Sound playback with repeat/nag until dismissed
- Two dismiss options: Complete (gets credit) or Dismiss (no credit)
- Streak tracking and gamification stats per alarm
- Configurable hotkeys for complete/dismiss
- Integration with main keyboard listener
"""
import threading
import time
import json
import wave
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional, Callable, Dict, List, Any

# Windows sound playback
import sys
if sys.platform == 'win32':
    try:
        import winsound
        HAS_WINSOUND = True
    except ImportError:
        HAS_WINSOUND = False
else:
    HAS_WINSOUND = False

try:
    import sounddevice as sd
    import numpy as np
    HAS_SOUNDDEVICE = True
except ImportError:
    HAS_SOUNDDEVICE = False


def get_default_alarm_config() -> Dict[str, Any]:
    """Return the default alarms configuration."""
    return {
        "enabled": True,
        "complete_hotkey": "f7",    # User completed the task, gets streak credit
        "dismiss_hotkey": "f8",     # Just silence it, no credit, breaks streak
        "nag_interval_seconds": 60,
        "items": [
            {
                "id": "hydration_default",
                "name": "Hydration",
                "enabled": False,  # Disabled by default
                "interval_minutes": 60,
                "sound": "alarm"  # Built-in sound name
            }
        ]
    }


class AlarmManager:
    """Manages interval-based alarms with sound notifications and gamification."""
    
    # Built-in alarm sounds (relative to sounds directory)
    BUILTIN_SOUNDS = {
        'alarm': 'alarm.wav',
        'chime': 'chime.wav',
        'bell': 'bell.wav',
        'gentle': 'gentle.wav',
        'success': 'success.wav',  # For completion feedback
    }
    
    def __init__(self, config_dir: Path, sounds_dir: Path, get_config: Callable, save_config: Callable):
        """
        Initialize the alarm manager.
        
        Args:
            config_dir: Path to config directory
            sounds_dir: Path to sounds directory (for built-in sounds)
            get_config: Callable that returns current app config dict
            save_config: Callable to save config changes
        """
        self.config_dir = Path(config_dir)
        self.sounds_dir = Path(sounds_dir)
        self.get_config = get_config
        self.save_config = save_config
        
        self.running = False
        self.thread = None
        
        # Track active alarms (id -> last_triggered timestamp)
        self.last_triggered: Dict[str, float] = {}
        
        # Currently nagging alarm (only one at a time)
        self.nagging_alarm_id: Optional[str] = None
        self.nag_thread: Optional[threading.Thread] = None
        self.nag_stop_event = threading.Event()
        
        # Sound cache for low-latency playback
        self._sound_cache: Dict[str, np.ndarray] = {}
        self._sound_sample_rate = 44100
        
        # Callbacks
        self.on_alarm_triggered: Optional[Callable[[dict], None]] = None
        self.on_alarm_dismissed: Optional[Callable[[dict], None]] = None
        self.on_alarm_completed: Optional[Callable[[dict, dict], None]] = None  # (alarm, stats)
        
        # Stats file for gamification/streak tracking
        self.stats_path = self.config_dir / 'alarm_stats.json'
        self.stats: Dict[str, Dict[str, Any]] = self._load_stats()
        
        # Ensure alarm sounds exist
        self._ensure_alarm_sounds()
        self._load_sound_cache()
    
    def _load_stats(self) -> Dict[str, Dict[str, Any]]:
        """Load alarm stats from file."""
        try:
            if self.stats_path.exists():
                with open(self.stats_path, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
                    # Reset completions_today if it's a new day
                    self._check_daily_reset(stats)
                    return stats
        except Exception as e:
            print(f"[ALARM] Failed to load stats: {e}")
        return {}
    
    def _save_stats(self):
        """Save alarm stats to file."""
        try:
            with open(self.stats_path, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=2)
        except Exception as e:
            print(f"[ALARM] Failed to save stats: {e}")
    
    def _check_daily_reset(self, stats: Dict[str, Dict[str, Any]]):
        """Reset completions_today if it's a new day."""
        today = date.today().isoformat()
        for alarm_id, alarm_stats in stats.items():
            last_date = alarm_stats.get('last_date', '')
            if last_date != today:
                alarm_stats['completions_today'] = 0
                alarm_stats['last_date'] = today
    
    def _get_alarm_stats(self, alarm_id: str) -> Dict[str, Any]:
        """Get or create stats for an alarm."""
        if alarm_id not in self.stats:
            self.stats[alarm_id] = {
                'completions_today': 0,
                'total_completions': 0,
                'current_streak': 0,
                'best_streak': 0,
                'last_completed': None,
                'last_date': date.today().isoformat(),
            }
        # Check for daily reset
        today = date.today().isoformat()
        if self.stats[alarm_id].get('last_date', '') != today:
            self.stats[alarm_id]['completions_today'] = 0
            self.stats[alarm_id]['last_date'] = today
        return self.stats[alarm_id]
    
    def get_stats(self, alarm_id: str) -> Dict[str, Any]:
        """Get stats for an alarm (public method)."""
        return self._get_alarm_stats(alarm_id).copy()
    
    def reset_stats(self, alarm_id: str):
        """Reset all stats for an alarm."""
        if alarm_id in self.stats:
            self.stats[alarm_id] = {
                'completions_today': 0,
                'total_completions': 0,
                'current_streak': 0,
                'best_streak': 0,
                'last_completed': None,
                'last_date': date.today().isoformat(),
            }
            self._save_stats()
            print(f"[ALARM] Reset stats for: {alarm_id}")
    
    @property
    def alarms_config(self) -> dict:
        """Get the alarms config section."""
        config = self.get_config()
        if 'alarms' not in config:
            config['alarms'] = get_default_alarm_config()
            self.save_config()
        return config['alarms']
    
    @property
    def enabled(self) -> bool:
        """Check if alarms are globally enabled."""
        return self.alarms_config.get('enabled', True)
    
    @property
    def complete_hotkey(self) -> str:
        """Get the complete hotkey (user did the task, gets credit)."""
        return self.alarms_config.get('complete_hotkey', 'f7')
    
    @property
    def dismiss_hotkey(self) -> str:
        """Get the dismiss hotkey (just silence, no credit)."""
        return self.alarms_config.get('dismiss_hotkey', 'f8')
    
    @property
    def nag_interval(self) -> int:
        """Get nag interval in seconds."""
        return self.alarms_config.get('nag_interval_seconds', 60)
    
    @property
    def items(self) -> List[dict]:
        """Get alarm items list."""
        return self.alarms_config.get('items', [])
    
    def _ensure_alarm_sounds(self):
        """Create default alarm sounds if they don't exist."""
        if not HAS_SOUNDDEVICE:
            return
            
        for sound_name, filename in self.BUILTIN_SOUNDS.items():
            sound_path = self.sounds_dir / filename
            if not sound_path.exists():
                self._generate_alarm_sound(sound_path, sound_name)
    
    def _generate_alarm_sound(self, filepath: Path, sound_type: str):
        """Generate a default alarm sound."""
        sample_rate = 44100
        
        def generate_tone(freq, duration, volume=0.5):
            t = np.linspace(0, duration, int(sample_rate * duration), False)
            tone = np.sin(2 * np.pi * freq * t) * volume
            # Fade in/out
            fade_len = min(int(sample_rate * 0.02), len(tone) // 4)
            if fade_len > 0:
                tone[:fade_len] *= np.linspace(0, 1, fade_len)
                tone[-fade_len:] *= np.linspace(1, 0, fade_len)
            return tone
        
        if sound_type == 'alarm':
            # Classic alarm: two-tone repeating
            t1 = generate_tone(880, 0.2, 0.6)
            gap = np.zeros(int(sample_rate * 0.1))
            t2 = generate_tone(660, 0.2, 0.6)
            # Repeat pattern 3 times
            pattern = np.concatenate([t1, gap, t2, gap])
            audio = np.tile(pattern, 3)
        elif sound_type == 'chime':
            # Pleasant chime: ascending notes
            notes = [523, 659, 784, 1047]  # C5, E5, G5, C6
            segments = []
            for note in notes:
                segments.append(generate_tone(note, 0.25, 0.5))
                segments.append(np.zeros(int(sample_rate * 0.05)))
            audio = np.concatenate(segments)
        elif sound_type == 'bell':
            # Bell-like with decay
            t = np.linspace(0, 1.5, int(sample_rate * 1.5), False)
            audio = np.sin(2 * np.pi * 440 * t) * np.exp(-2 * t) * 0.7
            # Add harmonics
            audio += np.sin(2 * np.pi * 880 * t) * np.exp(-3 * t) * 0.3
            audio += np.sin(2 * np.pi * 1320 * t) * np.exp(-4 * t) * 0.15
        elif sound_type == 'gentle':
            # Soft notification
            t1 = generate_tone(392, 0.3, 0.4)  # G4
            gap = np.zeros(int(sample_rate * 0.1))
            t2 = generate_tone(523, 0.4, 0.4)  # C5
            audio = np.concatenate([t1, gap, t2])
        else:
            # Default: simple beep
            audio = generate_tone(440, 0.5, 0.5)
        
        # Save as WAV
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            audio_int = (audio * 32767).astype(np.int16)
            with wave.open(str(filepath), 'w') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(audio_int.tobytes())
            print(f"[ALARM] Generated default sound: {filepath.name}")
        except Exception as e:
            print(f"[ALARM] Failed to generate sound {filepath}: {e}")
    
    def _load_sound_cache(self):
        """Load alarm sounds into memory for quick playback."""
        if not HAS_SOUNDDEVICE:
            return
            
        self._sound_cache = {}
        
        # Load built-in sounds
        for sound_name, filename in self.BUILTIN_SOUNDS.items():
            sound_path = self.sounds_dir / filename
            if sound_path.exists():
                try:
                    audio = self._load_wav(sound_path)
                    if audio is not None:
                        self._sound_cache[sound_name] = audio
                except Exception as e:
                    print(f"[ALARM] Failed to load {sound_path}: {e}")
    
    def _load_wav(self, filepath: Path) -> Optional[np.ndarray]:
        """Load a WAV file into a numpy array."""
        try:
            with wave.open(str(filepath), 'rb') as wf:
                sample_rate = wf.getframerate()
                n_channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                audio_data = wf.readframes(wf.getnframes())
            
            if sample_width == 1:
                dtype = np.uint8
            elif sample_width == 2:
                dtype = np.int16
            else:
                dtype = np.int32
            
            audio = np.frombuffer(audio_data, dtype=dtype).astype(np.float32)
            
            if sample_width == 1:
                audio = (audio - 128) / 128.0
            else:
                audio = audio / (2 ** (sample_width * 8 - 1))
            
            # Mix stereo to mono
            if n_channels == 2:
                audio = audio.reshape(-1, 2).mean(axis=1)
            
            # Resample if needed
            if sample_rate != self._sound_sample_rate:
                duration = len(audio) / sample_rate
                new_length = int(duration * self._sound_sample_rate)
                indices = np.linspace(0, len(audio) - 1, new_length)
                audio = np.interp(indices, np.arange(len(audio)), audio)
            
            return audio.astype(np.float32)
        except Exception as e:
            print(f"[ALARM] Error loading WAV {filepath}: {e}")
            return None
    
    def get_sound_for_alarm(self, alarm: dict) -> Optional[np.ndarray]:
        """Get the sound array for an alarm."""
        sound = alarm.get('sound', 'alarm')
        
        # Check if it's a built-in sound name
        if sound in self._sound_cache:
            return self._sound_cache[sound]
        
        # Check if it's a file path
        sound_path = Path(sound)
        if sound_path.exists():
            return self._load_wav(sound_path)
        
        # Try relative to sounds directory
        rel_path = self.sounds_dir / sound
        if rel_path.exists():
            return self._load_wav(rel_path)
        
        # Fallback to default alarm
        return self._sound_cache.get('alarm')
    
    def play_sound(self, alarm: dict, volume: float = 0.7):
        """Play the sound for an alarm."""
        audio = self.get_sound_for_alarm(alarm)
        
        if audio is not None and HAS_SOUNDDEVICE:
            try:
                scaled = (audio * volume).flatten()
                sd.play(scaled, self._sound_sample_rate)
                sd.wait()
            except Exception as e:
                print(f"[ALARM] Sound playback error: {e}")
                self._fallback_play(alarm)
        else:
            self._fallback_play(alarm)
    
    def _fallback_play(self, alarm: dict):
        """Fallback sound playback using winsound or system beep."""
        sound = alarm.get('sound', 'alarm')
        
        # Try winsound with WAV file
        if HAS_WINSOUND:
            # Try built-in sound file
            if sound in self.BUILTIN_SOUNDS:
                sound_path = self.sounds_dir / self.BUILTIN_SOUNDS[sound]
            else:
                sound_path = Path(sound)
            
            if sound_path.exists():
                try:
                    winsound.PlaySound(str(sound_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                    return
                except Exception:
                    pass
            
            # System beep as last resort
            try:
                winsound.Beep(880, 500)
            except Exception:
                pass
    
    def play_sound_file(self, sound_path: str, volume: float = 0.7):
        """Play a specific sound file (for preview in settings)."""
        path = Path(sound_path)
        
        # Handle built-in sound names
        if sound_path in self.BUILTIN_SOUNDS:
            path = self.sounds_dir / self.BUILTIN_SOUNDS[sound_path]
        elif not path.exists():
            path = self.sounds_dir / sound_path
        
        if path.exists() and HAS_SOUNDDEVICE:
            audio = self._load_wav(path)
            if audio is not None:
                try:
                    sd.play((audio * volume).flatten(), self._sound_sample_rate)
                    sd.wait()
                except Exception as e:
                    print(f"[ALARM] Preview playback error: {e}")
        elif HAS_WINSOUND and path.exists():
            try:
                winsound.PlaySound(str(path), winsound.SND_FILENAME)
            except Exception:
                pass
    
    def start(self):
        """Start the alarm check loop."""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._check_loop, daemon=True)
        self.thread.start()
        print("[ALARM] Alarm manager started")
    
    def stop(self):
        """Stop the alarm manager."""
        self.running = False
        self.stop_nagging()
        
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None
        
        print("[ALARM] Alarm manager stopped")
    
    def _check_loop(self):
        """Background loop checking for due alarms."""
        while self.running:
            try:
                if self.enabled:
                    self._check_alarms()
            except Exception as e:
                print(f"[ALARM] Check loop error: {e}")
            
            # Check every 10 seconds
            for _ in range(10):
                if not self.running:
                    break
                time.sleep(1)
    
    def _check_alarms(self):
        """Check if any alarms are due."""
        now = time.time()
        
        for alarm in self.items:
            if not alarm.get('enabled', False):
                continue
            
            alarm_id = alarm.get('id', alarm.get('name', 'unknown'))
            interval_minutes = alarm.get('interval_minutes', 60)
            interval_seconds = interval_minutes * 60
            
            last_trigger = self.last_triggered.get(alarm_id, 0)
            
            # Check if interval has elapsed
            if now - last_trigger >= interval_seconds:
                self._trigger_alarm(alarm)
                self.last_triggered[alarm_id] = now
    
    def _trigger_alarm(self, alarm: dict):
        """Trigger an alarm - start playing and nagging."""
        alarm_id = alarm.get('id', alarm.get('name', 'unknown'))
        print(f"[ALARM] Triggered: {alarm.get('name', 'Unnamed')}")
        
        # Stop any existing nag
        self.stop_nagging()
        
        # Start nagging
        self.nagging_alarm_id = alarm_id
        self.nag_stop_event.clear()
        self.nag_thread = threading.Thread(
            target=self._nag_loop, 
            args=(alarm,), 
            daemon=True
        )
        self.nag_thread.start()
        
        if self.on_alarm_triggered:
            self.on_alarm_triggered(alarm)
    
    def _nag_loop(self, alarm: dict):
        """Continuously nag until dismissed."""
        alarm_id = alarm.get('id', alarm.get('name', 'unknown'))
        nag_interval = self.nag_interval
        
        while not self.nag_stop_event.is_set() and self.nagging_alarm_id == alarm_id:
            # Play the alarm sound
            self.play_sound(alarm)
            
            # Wait for nag interval or stop event
            if self.nag_stop_event.wait(timeout=nag_interval):
                break  # Stop event was set
        
        print(f"[ALARM] Stopped nagging: {alarm.get('name', 'Unnamed')}")
    
    def stop_nagging(self):
        """Stop the current nagging alarm."""
        if self.nagging_alarm_id:
            print(f"[ALARM] Stopping nag for alarm")
            self.nag_stop_event.set()
            self.nagging_alarm_id = None
            
            if self.nag_thread and self.nag_thread.is_alive():
                self.nag_thread.join(timeout=1)
            self.nag_thread = None
    
    def complete(self) -> bool:
        """Complete the currently nagging alarm (user did the task, gets credit).
        
        Returns True if an alarm was completed.
        """
        if self.nagging_alarm_id:
            alarm_id = self.nagging_alarm_id
            alarm = self.get_alarm(alarm_id)
            alarm_name = alarm.get('name', alarm_id) if alarm else alarm_id
            
            self.stop_nagging()
            
            # Update stats - credit the completion
            stats = self._get_alarm_stats(alarm_id)
            stats['completions_today'] += 1
            stats['total_completions'] += 1
            stats['current_streak'] += 1
            if stats['current_streak'] > stats['best_streak']:
                stats['best_streak'] = stats['current_streak']
            stats['last_completed'] = datetime.now().isoformat()
            self._save_stats()
            
            # Log the streak
            streak = stats['current_streak']
            best = stats['best_streak']
            if streak == best and streak > 1:
                print(f"[STREAK] {alarm_name}: {streak} in a row! 🎉 NEW BEST!")
            else:
                print(f"[STREAK] {alarm_name}: {streak} in a row! (Best: {best})")
            
            if self.on_alarm_completed and alarm:
                self.on_alarm_completed(alarm, stats)
            
            return True
        return False
    
    def dismiss(self) -> bool:
        """Dismiss the currently nagging alarm (just silence, no credit, breaks streak).
        
        Returns True if an alarm was dismissed.
        """
        if self.nagging_alarm_id:
            alarm_id = self.nagging_alarm_id
            alarm = self.get_alarm(alarm_id)
            alarm_name = alarm.get('name', alarm_id) if alarm else alarm_id
            
            self.stop_nagging()
            
            # Update stats - break the streak (no credit)
            stats = self._get_alarm_stats(alarm_id)
            old_streak = stats['current_streak']
            stats['current_streak'] = 0
            self._save_stats()
            
            if old_streak > 0:
                print(f"[ALARM] Dismissed {alarm_name} - streak reset (was {old_streak})")
            else:
                print(f"[ALARM] Dismissed {alarm_name}")
            
            if self.on_alarm_dismissed and alarm:
                self.on_alarm_dismissed(alarm)
            
            return True
        return False
    
    def is_nagging(self) -> bool:
        """Check if an alarm is currently nagging."""
        return self.nagging_alarm_id is not None
    
    def get_nagging_alarm(self) -> Optional[dict]:
        """Get the currently nagging alarm, if any."""
        if self.nagging_alarm_id:
            return self.get_alarm(self.nagging_alarm_id)
        return None
    
    # === Alarm CRUD operations ===
    
    def get_alarm(self, alarm_id: str) -> Optional[dict]:
        """Get an alarm by ID."""
        for alarm in self.items:
            if alarm.get('id') == alarm_id or alarm.get('name') == alarm_id:
                return alarm
        return None
    
    def add_alarm(self, name: str, interval_minutes: int = 60, 
                  sound: str = 'alarm', enabled: bool = True) -> dict:
        """Add a new alarm."""
        alarm_id = f"alarm_{int(time.time() * 1000)}"
        alarm = {
            'id': alarm_id,
            'name': name,
            'interval_minutes': interval_minutes,
            'sound': sound,
            'enabled': enabled
        }
        
        config = self.get_config()
        if 'alarms' not in config:
            config['alarms'] = get_default_alarm_config()
        config['alarms']['items'].append(alarm)
        self.save_config()
        
        print(f"[ALARM] Added alarm: {name}")
        return alarm
    
    def update_alarm(self, alarm_id: str, **kwargs) -> bool:
        """Update an alarm's properties."""
        config = self.get_config()
        alarms = config.get('alarms', {}).get('items', [])
        
        for alarm in alarms:
            if alarm.get('id') == alarm_id or alarm.get('name') == alarm_id:
                for key, value in kwargs.items():
                    alarm[key] = value
                self.save_config()
                return True
        return False
    
    def remove_alarm(self, alarm_id: str) -> bool:
        """Remove an alarm."""
        config = self.get_config()
        alarms = config.get('alarms', {}).get('items', [])
        original_count = len(alarms)
        
        config['alarms']['items'] = [
            a for a in alarms 
            if a.get('id') != alarm_id and a.get('name') != alarm_id
        ]
        
        if len(config['alarms']['items']) < original_count:
            self.save_config()
            # Clean up tracking
            if alarm_id in self.last_triggered:
                del self.last_triggered[alarm_id]
            print(f"[ALARM] Removed alarm: {alarm_id}")
            return True
        return False
    
    def toggle_alarm(self, alarm_id: str) -> Optional[bool]:
        """Toggle an alarm's enabled state. Returns new state or None if not found."""
        config = self.get_config()
        alarms = config.get('alarms', {}).get('items', [])
        
        for alarm in alarms:
            if alarm.get('id') == alarm_id or alarm.get('name') == alarm_id:
                alarm['enabled'] = not alarm.get('enabled', False)
                self.save_config()
                return alarm['enabled']
        return None
    
    def set_global_enabled(self, enabled: bool):
        """Set global alarms enabled state."""
        config = self.get_config()
        if 'alarms' not in config:
            config['alarms'] = get_default_alarm_config()
        config['alarms']['enabled'] = enabled
        self.save_config()
    
    def set_complete_hotkey(self, hotkey: str):
        """Set the complete hotkey."""
        config = self.get_config()
        if 'alarms' not in config:
            config['alarms'] = get_default_alarm_config()
        config['alarms']['complete_hotkey'] = hotkey.lower()
        self.save_config()
    
    def set_dismiss_hotkey(self, hotkey: str):
        """Set the dismiss hotkey."""
        config = self.get_config()
        if 'alarms' not in config:
            config['alarms'] = get_default_alarm_config()
        config['alarms']['dismiss_hotkey'] = hotkey.lower()
        self.save_config()
    
    def set_nag_interval(self, seconds: int):
        """Set the nag interval in seconds."""
        config = self.get_config()
        if 'alarms' not in config:
            config['alarms'] = get_default_alarm_config()
        config['alarms']['nag_interval_seconds'] = seconds
        self.save_config()
    
    def get_available_sounds(self) -> List[dict]:
        """Get list of available sounds (built-in + custom)."""
        sounds = []
        
        # Built-in sounds
        for name in self.BUILTIN_SOUNDS.keys():
            sounds.append({
                'name': name.title(),
                'value': name,
                'builtin': True
            })
        
        # Custom sounds in sounds directory
        if self.sounds_dir.exists():
            for f in self.sounds_dir.glob('*.wav'):
                if f.name not in self.BUILTIN_SOUNDS.values():
                    sounds.append({
                        'name': f.stem.replace('_', ' ').title(),
                        'value': str(f),
                        'builtin': False
                    })
        
        return sounds
    
    def reset_alarm_timer(self, alarm_id: str):
        """Reset an alarm's timer (e.g., after manual trigger or edit)."""
        if alarm_id in self.last_triggered:
            del self.last_triggered[alarm_id]
