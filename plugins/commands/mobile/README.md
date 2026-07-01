# Samsara Mobile Companion

Phone remote control for Samsara. Big touch-friendly buttons for media playback and volume. No cloud, no app store, no remote desktop.

## How to use

1. Start Samsara normally (`python dictation.py`)
2. The companion server starts automatically on port 8742
3. On your phone, open the URL printed in Samsara's console (e.g. `http://192.168.1.50:8742`)
4. **Add to Home Screen** — tap Chrome's menu → "Add to Home Screen"
5. It now works like a native app

## What it controls

| Button | What it does |
|--------|-------------|
| ▶/⏸ | Play/pause current media (works on Spotify, browser, VLC, Stremio, anything) |
| ⏮ | Previous track |
| ⏭ | Next track |
| 🔊/🔉 | Volume up/down (system volume, ~4% steps) |
| Slider | Fine-grained volume control |
| 🔇 | Mute toggle (system mute) |

The transport controls use SMTC (System Media Transport Controls) — same mechanism as Bluetooth earbud buttons. Whatever's currently playing responds.

## Troubleshooting

**Can't connect from phone:**
- Phone and PC must be on the same WiFi network
- Windows Firewall may block the first connection — allow "Python" when prompted
- If your PC IP changes, open the Settings panel on the phone and update the IP

**Controls don't respond:**
- Nothing playing? Play/pause/next only work when a media app is active
- Volume and mute always work regardless

**Server port conflict:**
- Edit `PORT` in `mobile_companion.py` to change from 8742

## Files

```
plugins/commands/
├── mobile_companion.py    # Plugin: HTTP server + API endpoints
└── mobile/
    ├── index.html          # PWA frontend
    ├── manifest.json       # PWA manifest for "Add to Home Screen"
    └── icon.svg            # App icon
```
