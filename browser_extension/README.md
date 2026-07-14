# Samsara Show Numbers (Brave/Chromium extension)

Labels visible interactive elements on the active webpage with numbers so
Samsara can select them by voice ("show numbers", then "click 7"). Falls back
to Samsara's native UI Automation overlay automatically when this extension
isn't installed, isn't connected, or the page is restricted (e.g. `brave://`
pages, the extension gallery).

This extension only ever talks to a Samsara process running on the same
machine, over a loopback WebSocket (`ws://127.0.0.1:47831`). It makes no other
network requests.

## Install (one-time)

1. Open `brave://extensions`.
2. Enable **Developer mode** (top-right toggle).
3. Click **Load unpacked** and select this `browser_extension` folder.

## After editing `manifest.json`, `background.js`, or `content*.js`

Click the reload icon for this extension on `brave://extensions`.

## Limitations (first milestone)

- Top-frame only -- controls inside `<iframe>`s are not labeled.
- Numbering may go stale if the page replaces the labeled elements with new
  DOM nodes while the overlay is visible (common in single-page apps) --
  re-run "show numbers" if that happens.
- If Samsara isn't running, or this extension's background service worker was
  suspended by the browser while idle, reconnection happens on the next
  periodic wake (roughly every 24 seconds), not instantly.
