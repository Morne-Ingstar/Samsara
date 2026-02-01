# Toast Notification System (Planned Feature)

## Overview

Windows toast notifications for reminders — medication, breaks, hydration, stretches, and custom alerts. More reliable than relying on Discord or external apps.

## Why This Feature?

For Samsara's audience (accessibility, chronic pain, hands-free users):

1. **Medication timing** — Gabapentin 3x/day, morning meds, Adderall at specific times
2. **RSI/strain prevention** — Break reminders every 45-60 minutes during long sessions
3. **Hydration** — Easy to forget when focused; periodic water reminders
4. **Stretch prompts** — "Time to stretch your hands/neck"
5. **Posture checks** — Quick "how's your posture?" nudge
6. **20-20-20 rule** — Eye strain prevention (every 20 min, look 20 ft away for 20 sec)
7. **Custom reminders** — User-defined, voice-triggered ("remind me in 30 minutes to check the laundry")

## Why Build This Into Samsara?

- **Already running in background** — Samsara has system tray presence, no extra apps needed
- **Voice control users aren't always watching the screen** — Toast notifications grab attention
- **Chronic pain users need reliable reminders** — Pain is distracting, meds need precise timing, self-care gets forgotten
- **Natural extension of the accessibility mission** — Helping people take care of themselves while they work

## Planned Features

### Reminder Types

| Type | Example | Use Case |
|------|---------|----------|
| **Interval** | Every 60 minutes | Hydration, breaks |
| **Scheduled times** | 9:00 AM, 2:00 PM, 9:00 PM | Medication |
| **One-shot** | In 30 minutes | Quick custom reminders |

### Voice Command Support

Say things like:
- "Remind me in 30 minutes to check the laundry"
- "Remind me in 2 hours to take a break"
- "Set a hydration reminder"

### Settings UI

A new "Reminders" tab in Settings:
- Enable/disable notifications globally
- List of configured reminders with on/off toggles
- Add, edit, delete reminders
- Quick-add presets: Hydration, Break, Stretch

### Preset Reminders

One-click setup for common needs:
- **Hydration** — Every 60 minutes: "Drink some water! 💧"
- **Break** — Every 45 minutes: "Take a short break, stretch your legs"
- **Stretch** — Every 2 hours: "Time to stretch your hands and neck"
- **Posture** — Every 30 minutes: "Quick posture check!"

## Technical Notes

- Uses Windows 10/11 toast notification API (`win10toast` or similar)
- Runs in background thread, checks reminders every 30 seconds
- Reminder state persisted to `reminders.json`
- Integrates with existing Samsara system tray

## Status

🚧 **Planned** — Not yet implemented. See `SATURN_HANDOFF.md` for implementation details.
