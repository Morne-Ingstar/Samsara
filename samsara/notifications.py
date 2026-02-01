"""Windows toast notification system for reminders.

Supports:
- Interval reminders (every N minutes)
- Scheduled times (e.g., 09:00, 14:00, 21:00)
- One-time reminders (at specific datetime)

Designed for accessibility use cases:
- Medication timing
- RSI/strain prevention breaks
- Hydration reminders
- Stretch prompts
- Custom voice-triggered reminders
"""
import threading
import time
import re
from datetime import datetime, timedelta
from pathlib import Path
import json

# Windows toast notifications
try:
    from win10toast_click import ToastNotifier
    HAS_TOAST = True
    TOAST_TYPE = 'click'
except ImportError:
    try:
        from win10toast import ToastNotifier
        HAS_TOAST = True
        TOAST_TYPE = 'basic'
    except ImportError:
        HAS_TOAST = False
        TOAST_TYPE = None


class NotificationManager:
    """Manages scheduled notifications and reminders."""

    def __init__(self, config_dir):
        """
        Initialize the notification manager.

        Args:
            config_dir: Path to config directory for storing reminders.json
        """
        self.config_dir = Path(config_dir)
        self.config_file = self.config_dir / 'reminders.json'
        self.reminders = []
        self.toaster = ToastNotifier() if HAS_TOAST else None
        self.running = False
        self.thread = None
        self.on_notification = None  # Callback for notification events
        self.load_reminders()

    def load_reminders(self):
        """Load reminders from config file."""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    self.reminders = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"[NOTIFY] Error loading reminders: {e}")
                self.reminders = []
        else:
            self.reminders = []

    def save_reminders(self):
        """Save reminders to config file."""
        try:
            # Ensure config directory exists
            self.config_dir.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.reminders, f, indent=2)
        except IOError as e:
            print(f"[NOTIFY] Error saving reminders: {e}")

    def add_reminder(self, name, schedule, message, sound=True, enabled=True):
        """
        Add a new reminder.

        Args:
            name: Display name for the reminder
            schedule: Dict with schedule type and parameters:
                - {"type": "interval", "minutes": 60}
                - {"type": "times", "times": ["09:00", "12:00", "18:00"]}
                - {"type": "once", "at": "2026-01-29T14:30:00"}
            message: Notification message to display
            sound: Whether to play notification sound
            enabled: Whether reminder is active

        Returns:
            The created reminder dict
        """
        reminder = {
            "id": str(int(time.time() * 1000)),
            "name": name,
            "schedule": schedule,
            "message": message,
            "sound": sound,
            "enabled": enabled,
            "last_fired": None,
            "created": datetime.now().isoformat()
        }
        self.reminders.append(reminder)
        self.save_reminders()
        print(f"[NOTIFY] Added reminder: {name}")
        return reminder

    def remove_reminder(self, reminder_id):
        """Remove a reminder by ID."""
        original_count = len(self.reminders)
        self.reminders = [r for r in self.reminders if r['id'] != reminder_id]
        if len(self.reminders) < original_count:
            self.save_reminders()
            print(f"[NOTIFY] Removed reminder: {reminder_id}")
            return True
        return False

    def update_reminder(self, reminder_id, **kwargs):
        """Update a reminder's properties."""
        for reminder in self.reminders:
            if reminder['id'] == reminder_id:
                for key, value in kwargs.items():
                    if key in reminder:
                        reminder[key] = value
                self.save_reminders()
                return True
        return False

    def get_reminder(self, reminder_id):
        """Get a reminder by ID."""
        for reminder in self.reminders:
            if reminder['id'] == reminder_id:
                return reminder
        return None

    def get_all_reminders(self):
        """Get all reminders."""
        return list(self.reminders)

    def toggle_reminder(self, reminder_id):
        """Toggle a reminder's enabled state."""
        for reminder in self.reminders:
            if reminder['id'] == reminder_id:
                reminder['enabled'] = not reminder.get('enabled', True)
                self.save_reminders()
                return reminder['enabled']
        return None

    def show_notification(self, title, message, duration=5):
        """
        Show a Windows toast notification.

        Args:
            title: Notification title
            message: Notification body text
            duration: How long to show (seconds)
        """
        if not self.toaster:
            print(f"[NOTIFY] {title}: {message}")
            return False

        try:
            # Run in thread to avoid blocking
            def show():
                try:
                    self.toaster.show_toast(
                        title,
                        message,
                        duration=duration,
                        threaded=False  # We're already in a thread
                    )
                except Exception as e:
                    print(f"[NOTIFY ERROR] {e}")

            toast_thread = threading.Thread(target=show, daemon=True)
            toast_thread.start()

            if self.on_notification:
                self.on_notification(title, message)

            return True
        except Exception as e:
            print(f"[NOTIFY ERROR] {e}")
            return False

    def start(self):
        """Start the reminder check loop."""
        if self.running:
            return

        if not HAS_TOAST:
            print("[NOTIFY] Toast notifications not available (install win10toast or win10toast-click)")

        self.running = True
        self.thread = threading.Thread(target=self._check_loop, daemon=True)
        self.thread.start()
        print("[NOTIFY] Notification manager started")

    def stop(self):
        """Stop the reminder check loop."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None
        print("[NOTIFY] Notification manager stopped")

    def _check_loop(self):
        """Background loop checking for due reminders."""
        while self.running:
            try:
                now = datetime.now()

                for reminder in self.reminders:
                    if not reminder.get('enabled', True):
                        continue

                    if self._is_due(reminder, now):
                        self.show_notification(
                            reminder.get('name', 'Reminder'),
                            reminder['message']
                        )
                        reminder['last_fired'] = now.isoformat()

                        # Remove one-time reminders after firing
                        schedule = reminder.get('schedule', {})
                        if schedule.get('type') == 'once':
                            reminder['enabled'] = False

                        self.save_reminders()

            except Exception as e:
                print(f"[NOTIFY] Check loop error: {e}")

            # Check every 30 seconds
            for _ in range(30):
                if not self.running:
                    break
                time.sleep(1)

    def _is_due(self, reminder, now):
        """
        Check if a reminder is due.

        Args:
            reminder: Reminder dict
            now: Current datetime

        Returns:
            True if reminder should fire
        """
        schedule = reminder.get('schedule', {})
        last_fired = reminder.get('last_fired')

        if last_fired:
            try:
                last_fired = datetime.fromisoformat(last_fired)
            except (ValueError, TypeError):
                last_fired = None

        stype = schedule.get('type')

        if stype == 'interval':
            minutes = schedule.get('minutes', 60)
            if not last_fired:
                return True
            return (now - last_fired) >= timedelta(minutes=minutes)

        elif stype == 'times':
            times = schedule.get('times', [])
            current_time = now.strftime('%H:%M')

            if current_time in times:
                # Only fire once per scheduled time per day
                if last_fired:
                    if (last_fired.strftime('%H:%M') == current_time and
                            last_fired.date() == now.date()):
                        return False
                return True

        elif stype == 'once':
            try:
                target = datetime.fromisoformat(schedule.get('at', ''))
                if now >= target and not last_fired:
                    return True
            except (ValueError, TypeError):
                pass

        return False

    def add_quick_reminder(self, minutes, message=None):
        """
        Add a one-time reminder for N minutes from now.

        Args:
            minutes: Minutes from now
            message: Optional message (defaults to "Time's up!")

        Returns:
            The created reminder
        """
        if message is None:
            message = "Time's up!"

        target_time = datetime.now() + timedelta(minutes=minutes)

        return self.add_reminder(
            name=f"Reminder ({minutes} min)",
            schedule={"type": "once", "at": target_time.isoformat()},
            message=message
        )

    def parse_remind_command(self, text):
        """
        Parse a voice command like "remind me in 30 minutes to take a break".

        Args:
            text: Voice command text

        Returns:
            (minutes, task) tuple if parsed, None otherwise
        """
        # Pattern: "remind me in X minutes [to Y]"
        patterns = [
            r"remind me in (\d+) minutes?(?: to (.+))?",
            r"set (?:a )?reminder (?:for )?(\d+) minutes?(?: to (.+))?",
            r"(\d+) minute reminder(?: to (.+))?",
        ]

        text_lower = text.lower().strip()

        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                minutes = int(match.group(1))
                task = match.group(2) if match.lastindex >= 2 else None
                return (minutes, task)

        return None


def get_default_notification_config():
    """Return the default notifications configuration."""
    return {
        "enabled": True,
        "presets": [
            {
                "name": "Hydration",
                "schedule": {"type": "interval", "minutes": 60},
                "message": "Time to drink some water!"
            },
            {
                "name": "Break",
                "schedule": {"type": "interval", "minutes": 45},
                "message": "Take a short break - stretch and rest your eyes."
            },
            {
                "name": "Stretch",
                "schedule": {"type": "interval", "minutes": 120},
                "message": "Time to stretch your hands, wrists, and neck."
            },
            {
                "name": "20-20-20 Rule",
                "schedule": {"type": "interval", "minutes": 20},
                "message": "Look at something 20 feet away for 20 seconds."
            }
        ]
    }
