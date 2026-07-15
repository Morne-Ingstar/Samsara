# Samsara Show Numbers (Brave/Chromium extension)

Labels visible interactive elements on the active webpage with numbers so
Samsara can select them by voice ("show numbers", then "click 7"). Falls back
to Samsara's native UI Automation overlay automatically when this extension
isn't installed, isn't connected, or the page is restricted (e.g. `brave://`
pages, the extension gallery).

This extension only ever talks to a Samsara process running on the same
machine, over a loopback WebSocket (`ws://127.0.0.1:47831`). Both sides prove
knowledge of a per-install pairing secret before the extension accepts any
click, focus, or selection request. It makes no other network requests.

## Install (one-time)

1. Start Samsara once. Its log prints the exact **Authenticated extension
   ready at ...** directory. Normally this is
   `C:\Users\<you>\.samsara\browser_bridge\extension`.
2. Open `brave://extensions`.
3. Enable **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select the authenticated extension directory
   printed by Samsara, not the source repository's template directory.

Existing pre-pairing installations must be removed/reloaded from that
profile-specific directory. They intentionally fail closed and show a red
`!` badge; there is no insecure compatibility fallback.

## After editing `manifest.json`, `background.js`, or `content*.js`

Restart Samsara so it refreshes the profile-specific installed copy, then
click the reload icon for that copy on `brave://extensions`.

## Limitations (first milestone)

- Top-frame only -- controls inside `<iframe>`s are not labeled.
- Numbering may go stale if the page replaces the labeled elements with new
  DOM nodes while the overlay is visible (common in single-page apps) --
  re-run "show numbers" if that happens.
- If Samsara isn't running, or this extension's background service worker was
  suspended by the browser while idle, reconnection happens on the next
  periodic wake (roughly every 24 seconds), not instantly.
