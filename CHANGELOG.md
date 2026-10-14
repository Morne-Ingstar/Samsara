# Changelog

All notable changes to Samsara are documented here.

## [0.30.0-beta.5] - Unreleased

Fixes for everything the beta.4 check found, clearer Ava and OpenRouter messages, and AI apps that can work with Samsara.

<!-- Stand-in release wording for the owner's review. -->

### Fixes from the beta.4 audit

- New portable copies understand the release folder layout when preparing future updates. Portable beta.4 users must use the installer once: [back up and upgrade safely](Docs/UPGRADING_TO_BETA5.md).
- Headset recovery handles stalled sounds and failed audio starts more reliably. A stalled Windows voice may end its current sentence.
- Music resumes when Windows reports Samsara's own pause late. One broken media session no longer prevents the others from pausing.
- In-app installer updates preserve command edits, archive old plugins, and retain recovery copies if installation fails. A manual installer does not make that backup for you.

### Ava & OpenRouter

- Ava answers queued questions once, shows when it is loading, and responds to stop.
- Cloud consent takes effect, and each provider keeps its own key.
- OpenRouter has a searchable model list with prices and filters. Auto follows the default model, and Refresh recovers after a picker error.
- Key and model checks distinguish setup from a successful reply. Ava explains credit, key, model and provider failures, with bounded retry delays.

### AI apps

- An optional agent server lets AI apps on this PC check Samsara's status and ask it to speak. The connection is off by default.
- AI typing has its own switch, off by default, and asks for spoken approval for each paste. Choose ordinary yes or hold the Command key while saying yes. Old answers, cancelled captures and repeated key presses cannot approve a later request.
- Connect an AI app copies setup for Claude Code and Claude Desktop. Desktop uses a bundled local relay; Samsara does not edit another app's settings.
- Agent instructions are included beside Samsara.exe in `Docs/agents/samsara/SKILL.md` and `Docs/hermes/samsara-voice/SKILL.md`.

### Commands

- The command catalogue matches the shipped commands, and missing descriptions have been filled in.

### Daily-use polish

- Drag the Compact display at any time.
- Quitting or updating asks about unsent words without stacking questions. Kept and live drafts are sent together.

## [0.30.0-beta.4] - 2026-10-12

Fixes from a second computer: a microphone that arrives late, Pause, setup, and updates that respect how Samsara was installed.

### New

- **Beta updates are your choice** - Tick "Include beta updates" in setup or in the Updates window and Samsara also offers tester builds. It is off until you turn it on, and turning it on does not start automatic checks.
- **Starting Samsara twice shows the one that is running** - A second start brings Samsara's window forward instead of ending without a word.

### Changed

- **Installed copies update through the installer** - A copy installed with the installer no longer swaps its folder. When a release carries a verified installer, the Updates window installs it for you; until then it gives you a link to it.
- **Setup's choices explain one thing at a time** - Each choice shows only the explanation of the option you picked, and its tick sits inside it.

### Fixed

- **A microphone plugged in later just works** - Start Samsara without your microphone, or plug it in afterwards, and dictation works without a restart. With no microphone, Samsara says so instead of failing silently.
- **A finished install stays finished** - Samsara no longer returns to setup because its microphone is missing.
- **Settings keeps a microphone that is not connected** - It is listed as not connected and stays your choice.
- **Pause for other audio works in the installed app** - Choosing Pause stops other audio while you record and resumes it afterwards; if other audio cannot be controlled, Samsara tells you once.
- **The Compact placement picture draws again** - The picture on the placement page had stopped drawing.
- **Updating no longer removes the uninstaller** - On an installed copy the update button could remove Samsara's uninstaller. If that already happened to you, Samsara tells you and links to the installer.
- **Samsara notes how it ended** - If Samsara closes unexpectedly, its next start records how the last session ended, to help find the cause.

## [0.30.0-beta.3] - 2026-10-12

Expanded media integrations, Ava intelligence, a calmer Settings and a new setup walkthrough.

### New

- **Several commands in one sentence** — Say “open notepad and volume thirty” to run up to four supported direct commands in order; a command that needs confirmation or is not allowed prevents the combination from starting. <!-- e578786 -->
- **Tasks with a dedicated key** — Hold Ctrl+Alt+T, say the task, and release to add it to your local list. <!-- 5642c0b -->
- **Undo pasted dictation** — Say “scratch that” or use the undo key to take back the last dictation paste in the same editor; this is excluded in recognized terminal apps. <!-- b6fba88 -->
- **Spotify by name** — Say “pause Spotify”, “play Spotify”, or “Spotify volume up” and “Spotify volume down” to control Spotify even while another app is playing. <!-- 0eef376 -->
- **Import Spotify playlists** — Bring named playlists into Samsara from your Spotify account data export. <!-- a2a9ee1 -->
- **Microsoft Word commands** — Format text, change selections and ask for a word count in the focused Word window, without launching Word. <!-- 9f8d7a9 -->
- **Todoist tasks are optional** — Turn on Todoist and supply your own token to send tasks you add by voice to Todoist; your local list remains the default, and this connection is off by default. <!-- fbe5c13 -->
- **Ava memory controls** — Save facts explicitly, ask what Ava remembers, and forget or erase them; optional saved-session recall is off by default, and saved conversation records stay on this PC. <!-- 195d5d8, 03e3c28, b358d58, 954fc80 -->
- **Ask Ava about commands** — Ask which commands you have for an app or task; Ava uses the available command list and can suggest a real command after a refusal. Ava’s AI commands are off by default. <!-- c4ecc32 -->
- **Ask GPT or Claude by voice** — Use installed, signed-in command-line tools to ask questions and continue a conversation; “ask Claude” uses the Claude desktop app when it is available. <!-- 096906e -->
- **Read Claude replies aloud** — Read back desktop replies, ask for the rest, repeat them or stop the reading; reply reading is enabled by default when Samsara sends to Claude, subject to your voice feedback level. <!-- 5180ffa -->
- **Video memos** — Record a video memo with sound, then finish or cancel it by voice; this needs the optional camera components and the Utilities commands, which are off by default. <!-- 1f6a745 -->
- **Images by voice** — Create, revise, open or cancel images using a configured local ComfyUI server; these AI commands are off by default. <!-- 0d99c44, 0e08afa -->
- **Start at sign-in or in the tray** — Choose automatic start when you sign in to Windows, or start with the main window hidden; both choices are off by default. <!-- 2d47987 -->
- **Optional processing-time diagnostics** — Turn on local timing records and diagnostic event logs in Help’s Advanced settings to investigate where a recording spends time; both are off by default. <!-- aebdfda, d5a0b73, 122a7c5, 63195c7, 9e13379 -->
- **Ava understands plainly worded requests** - With Ava on, say what you want in your own words, up to three steps in one sentence; Ava chooses only from the commands you have.
- **Ava knows the date and time** - Ask her what day it is; every answer is given with the current local date and time in view.
- **"Ways to talk" in the tutorial** - Every way of talking to Samsara on one page, with details on demand.
- **"Scratch all that"** - Clears the whole waiting draft at once, with no question.
- **Settings asks before the network voice is used** - Choosing it shows what is sent and lets you say "Not now".
- **Result messages in plain words** - A finished chain reads "Done: opened Notepad · Windows volume 30%", and messages stay on screen long enough to read.
- **Put the listening display where you want it** - Place the Compact or Full display during setup or in the tutorial, and either display stays where you drop it and snaps to screen edges and corners.
- **A new first-run walkthrough** - Ten short pages, from the microphone check to a recognition model picked for your hardware.
- **Try your voice settings in the tutorial** - Hear the voice, its speed and its volume, and see each feedback level before you keep them; nothing changes until you press "Keep this".
- **Choose when hands-free types** - Settings > Dictation > "Hands-free sends": "When I say send" keeps your words in the display until you send them, as before; "As I speak" types each sentence when you pause.
- **Click a command to see what it does** - In the Command Reference a click opens a short card that explains the command; "Run it" runs it.
- **Try your settings, one step at a time** - The tutorial now walks you through it: how much Samsara talks, which voice, and your own pace. Each step plays at once, and nothing is saved until you choose "Keep this".
- **Ask your Hermes agent by voice** - If you use Hermes Agent, turn on the Hermes pack in Settings > Command library and say "ask Hermes ..." or "tell Hermes ..."; the answer comes back on screen and by voice. "New session with Hermes" and "stop Hermes" work too. Off by default, and nothing is sent until you accept.

### Changed

- **Choose how much Samsara speaks** — Silent, Brief and Full replace the speech switches; new installs use Brief and a local voice, and “talk less” or “talk more” changes the level by voice. <!-- 8f74fca, 0e873f7 -->
- **Settings simplified** — Six focused pages, a sidebar search and contextual Advanced controls make settings easier to find; Tools stay in the same window, and help buttons provide an explanation and a documentation link. <!-- c2c5bf3, 96d1539, 0fddd24, 87a1ca1, ae0c2fd -->
- **Ava can interpret missed commands** — With Ava on, she can choose one of your commands after a miss; turn this off in Ava settings. The separate Ava command-session key is retired.
- **Guides and controls are more consistent** — Settings and guides use consistent text sizes, spacing, larger controls and step indicators. <!-- 6fe2585, b5c68aa -->
- **Updated app mark** — The app uses the approved articulated snake mark, including as the tray changes listening state. <!-- 3740466, 215b017 -->
- **A calmer Settings** - Rows grouped under plain headings, On/Off switches, everyday choices moved out of Advanced, and a search box in the sidebar.
- **A command with no argument must be the whole sentence** - "close" in the middle of a sentence no longer closes a window.
- **Voice Training has left the menus** - It was not ready; nothing else changes.
- **The old echo-cancellation filter is retired** - A saved "on" no longer starts it.
- **A compact loading screen and a smaller "What's new" card.**
- **App commands find your apps** - "Open Spotify", "open Discord", "open Obsidian", "open Warp" and "run Claude Code" look for the app on your PC instead of a fixed folder, and say so when it is not installed.
- **Setup's questions sit in clear rows** - Each question has its own line, and every choice can be reached from the keyboard.
- **Your words stay out of the log file** - Samsara's log now records what happened and how long your text was, not what you said, wrote or had read aloud. Logs written before this update are left as they are.
- **Ava's conversation log is off unless you turn it on** - Settings > Ava > Advanced > "Keep a conversation log".
- **A quieter listening display** - Short messages appear as a small chip in the display's header, and the display no longer changes size when they come and go.
- **"Where it sits" shows one desktop** - The position you choose carries the animation.
- **The CapsLock streaming mode is retired** - Its setting and its tray entry are gone; a saved "on" no longer starts it.
- **A shorter Tools menu** - "Move listening indicator..." appears only with the Compact display, which needs it; the Full display can be dragged at any time.
- **Settings scrolls softly** - Thin lines between rows, and a fade at the edge instead of text cut off mid-line.
- **One clean keyboard outline** - When you move with the keyboard, each control shows one outline that fits its shape; the second, square outline inside it is gone.

### Fixed

- **A silent hold pastes nothing** — Releasing the key without speech shows “No speech” instead of invented text, without locking the key for five seconds; weak-audio short sign-offs can also be discarded. <!-- 5866c5a, e3b7912, 5fe2fb3 -->
- **Hold recordings keep the end of your speech** — With the live preview enabled, recording waits for speech to end after release and still finishes if that waiting step fails. <!-- 9babad3, d5e5e46 -->
- **Failed pastes keep the draft** — A blocked paste keeps your words, does not press Enter, and allows you to speak the retained text again as a retry; the notice explains clipboard blockage. <!-- d5a0b73, 890e8d4 -->
- **Office clipboard protection markers no longer block a paste** — Dictation can paste past Office’s empty protection markers while preserving readable clipboard contents; if those contents cannot be preserved, it refuses the paste. <!-- b1f4dbf -->
- **Commands-only key works on a default install** — Commands are matched when you hold the commands-only key; misses show what was heard and may suggest a supported command. <!-- b0e4b69, c149d9a -->
- **Cancelling a task or memo stays cancelled** — Escape no longer carries a cancelled recording into the next take. <!-- 5642c0b -->
- **Control words must stand alone** — Finish, cancel, pause and resume words inside ordinary sentences no longer act as whole-recording controls; existing custom cancel words are preserved. <!-- 890e8d4, 5180ffa -->
- **Hands-free music controls report what happened** — Pause and resume music while dictating hands-free; Spotify search says that it opened a search, and failed music controls no longer claim success. <!-- 197b7f1, 5180ffa, 6bf1965 -->
- **False wake detections leave music alone** — An unconfirmed wake detection no longer lowers your music volume; profiles also respect the required wake detection before activating. <!-- b67db62, 940a7f6 -->
- **Ava does not treat its own reply as your recording** — Command and hold recordings entirely within Ava’s spoken-reply window are discarded. <!-- c78125d -->
- **Ava settings and status agree** — One selector chooses Off, this PC or cloud, with consent when required; Home and Voice help follow Ava’s actual readiness and Home links reach the correct settings pages. <!-- c0f3341, f9c570a, 24e1645, 60b9491, a0ac09e -->
- **Local AI requests stay on this PC** — Local AI requests cannot follow redirects to another host, and cloud requests are limited to their provider’s allowed addresses. <!-- 02a8195 -->
- **Speech settings apply consistently** — Choosing Silent cancels queued speech, changing voices applies together, and saving another page keeps Ava captions and mute preferences. <!-- f499de1, 8f74fca -->
- **Settings keep your choices** — Saving Advanced keeps your selected theme; searches do not leave blank Advanced sections, and commands needing a spoken name accept a valid supplied value. Moving a window to another monitor requires its monitor number. <!-- 55940fc, 797798c, 1f7c394 -->
- **Setup and guidance reflect your settings** — Wake and finish hints use your chosen words; setup explains downloads and recovers after a failed model installation, without automatically checking for updates. <!-- 1f55039, cf29c71, 957cb75, 17881f8, 46c4d1b -->
- **The dictation preview remains readable** — Corrections and retained drafts stay visible, long incoming text scrolls, Clear works during a hold, and wake settings do not mislabel its activity; high contrast and reduced motion are respected. <!-- 91ee14c, 0ad7c6e, 6528d86, e3b7912 -->
- **Requested windows come forward** — Opening a Samsara window brings it forward while background updates leave focus alone; tray Exit remains available after an optional menu error, and restarting resolves a retained draft before the handoff. <!-- 02d868b, e6fd2c7, 6d59d24 -->
- **GPU availability is checked** — Samsara checks for an available GPU rather than relying on whether a GPU support package is installed. <!-- 890e8d4 -->
- **Release notes reach packaged builds** — The packaging recipe now includes the release text needed for Home’s “What’s new” note. <!-- be15e29 -->
- **Ava's status tells the truth** - Home, the setup guide and Settings agree on whether Ava is ready, and the OK for Ava on this PC can be given in Settings.
- **A quick tap of the Ava or Command-only key leaves nothing on screen.**
- **Samsara closes cleanly** - No crash record after Exit.
- **"Sounds quieter" and "sounds louder" work, and a command that fails says so** instead of doing nothing.
- **The command reference opens reliably from Home.**
- **No console windows flash when the network voice speaks.**
- **A voice command no longer pastes the waiting draft as a side effect.**
- **Apply in Settings no longer overwrites a setting that changed elsewhere while the window was open.**
- **"Send chrome to left screen", "tab next" and "tab close" work again.**
- **The first word of a hold is whole** when nothing else is playing.
- **Screen readers can name the controls** in Settings tools, the command reference, the editors and the pop-up messages.
- **Settings fields** - values sit centred, and the search box keeps its magnifier clear of the text.
- **A draft left after hands-free stays yours** - The display names the kept draft and how to send it, switching to command mode and back keeps it, and "bring back my draft" no longer doubles it.
- **The tutorial takes only its own dictation** - Its typing step receives the words meant for it and lets you continue, words meant for another app stay with that app, and cancelling a dictation on the Compact display keeps it from being typed.
- **Your words stay with the window you chose** - Samsara refuses a paste if the window you were dictating into has changed, and a quick "Thanks" spoken while holding the dictation key is kept.
- **Setup and the tutorial fit smaller screens** - Pages scroll while the steps and navigation buttons remain visible at 150% scaling and on a 1366 × 768 laptop.
- **Windows and text are easier to read** - Mic Setup, Wake profiles, Workflow, Updates and Profiles fit their content, the Full display's long draft has a readable scrollbar, Home's links and the Tasks line fit, and the quit question names what each button does.
- **Holding the key always types** - Also during a hands-free session: your held words go to the window, and the waiting draft is left alone.
- **Quitting with a kept draft asks first** - In the Compact display too: send it, discard it, or stay.
- **No stray blue outline on Home** - The focus outline appears when you use the keyboard, not after a mouse click.
- **Profiles and setup look right** - Profiles' labels lose their boxes, the placement explanation appears once, and setup's window grows before it scrolls.
- **An empty recording stays empty** - Before your words go to the speech model, Samsara checks that the recording contains a voice.
- **Samsara keeps talking when the Network voice is down** - If Microsoft's online voice does not answer, Samsara says it with the Windows voice instead and tells you once.
- **A setting you change back is changed back** - Changing a setting and later changing it back while Samsara is running now takes effect, and the voice list shows the voices of the engine you picked.

## [0.30.0-beta.2] - 2026-09-19

A beta for testers. The public download remains v0.22.1 while this build is
tested on more machines.

### Added

- **Focused browser controls** — browser commands now stay in the focused
  browser, including Find, numbered tabs, and page zoom.
- **Ava adapts when asked** — Ava can remember a preferred answer length,
  pace, repetition tolerance, name, and familiar interests, on this device.
- **Report a problem** — create a shareable, redacted support bundle.

### Fixed

- **Ava consent now turns Ava on** — accepting the chosen provider enables
  Ava while preserving the current Settings state.
- **Live surface feedback is reliable** — it stays on top, drag keeps its size
  and position, words appear while holding the key, mode and command-result
  feedback return, Pause parks a draft, notices expire, and the Insert button
  is gone.
- **Command Reference is easier to use** — its placement recovers when it is
  off-screen and its search, resize, close target, and most-used section are
  easier to read.
- **Live surface placement is safer** — it can dock and avoids the text caret.

### Changed

- **Live surface review is quieter** — it uses a compact text-first card,
  plain settled text, and correction controls only while correcting.

## [0.30.0-beta.1] - 2026-09-17

A beta for testers. The public download remains v0.22.1 while this build is
tested on more machines.

### Added

- **Dictionary corrections that apply everywhere** — add a correction once in
  Dictionary and it is applied to dictated text, rather than only appearing in
  the panel.
- **Ava start-up readiness** — Ava can warm a configured provider so the first
  reply is faster. Local providers warm by default; cloud warming is off until
  you opt in because it sends a small billable request.
- **Ava consent, captions and mute** — Ava asks before it is enabled, can show
  its replies in a high-contrast caption panel, and can mute only Ava's speech
  while leaving command sounds on.
- **Named Spotify items** — save playlists, albums, artists, or tracks once in
  Settings, then say “playlist Morning” or “album Blue Train.”
- **Focused voice help** — “what can I say” opens the command sheet for the
  app you are using, alongside commands that work everywhere.

### Fixed

- **Copied files stay safe** — clipboard recovery now refuses to destroy a
  copied file list when a paste cannot be proved to have landed.
- **Safer updates** — a failed update rolls back without leaving the installed
  app unusable, and updates refuse a package missing its bundled wake-word
  models.
- **First-run is honest** — setup no longer hangs on background work and it
  cannot complete until a usable microphone has been chosen.
- **Ava actions stay bounded** — model-proposed actions are limited to the
  menu Samsara actually offered; stale or denied actions cannot run later.

### Changed

- **Live theme switching** — changing light/dark mode updates open Samsara
  windows immediately.
- **Hands-free control is clearer** — the commit word is configurable, with
  “finish” as the default, and Settings exposes the hands-free ducking
  controls.
- **Home and History tell the truth** — Home's example strip only shows
  universal command packs, and History labels command rows with their
  canonical command name.

### Under the hood

- Qt import discipline now catches Qt objects constructed at module import,
  preventing a startup-thread crash class before it reaches a build.
- Ava warm-up results are generation-bound, require a usable completion, and
  cannot overwrite fresher evidence from a real turn.

## [0.23.0-beta.1] - 2026-09-13

A beta for testers, not the public download (that stays v0.22.1). It is the
first build of the conversational work: hands-free that stays open until you
send it to sleep, commands that tell you what actually happened, and a first
set of "do the real thing" commands for windows, music and the Claude app.

### Added

- **Sleep phrases** — "go to sleep", "samsara sleep" or "sleep now", said on
  their own, end the hands-free session from any lane. Anything you had
  dictated but not yet sent with "end" is kept and comes back the next time
  hands-free opens. A sentence that merely contains the words is dictated.
- **Wake word can open hands-free** — with `wake_word_config.opens_session`
  (config file, off by default) the wake phrase opens the same latched
  hands-free session the toggle key does, instead of a one-command window.
- **Screen destinations by position** — "put warp on the left screen",
  "... right / middle / top / bottom screen", "the other screen", and two
  windows in one sentence: "put warp on the left screen and claude on the
  right". If the second move fails, Samsara says so instead of claiming success.
- **Play a playlist by name** — "play my alternative rock playlist" /
  "play something from ..." goes to Spotify and only reports "playing" once
  the Spotify media session is actually playing.
- **"Tell Claude ..."** — prepares a message for the Claude desktop app and
  asks "Send to Claude: ...?". Only after your "yes" is it pasted and sent,
  and Samsara reports Sent, Not sent, or Outcome unknown from what it sees in
  the window; an unknown outcome is never resent.
- **Outcome chip** — the listening indicator now shows what just happened:
  a tick for a command that ran, MISS, refused, staged, typed, stopped,
  asleep. The earcon Test button respects the volume slider.
- **Window cube** — a pinned, numbered, always-on-top list of open windows;
  say a number to switch.
- **Verbatim dictation profile** for terminals and URL bars, plus a spoken
  toggle.
- **Quick memo** hotkey and voice command appending to your memos file, and
  voice memo capture to an Obsidian vault.
- **Live preview box** for hands-free dictation while you speak.
- **"What can I say"** — a hands-free section in Quick Reference.

### Fixed

- **Switching into Ava mode** — "ava mode" is a real switch word, and a failed
  switch is announced instead of silently leaving you where you were.
- **Microphone loss recovery** — devices are found by name and audio API, so
  unplugging and replugging (or a Windows reordering) recovers the same mic.
- **A command that fails is no longer typed as text** or sent to Ava as a new
  request; a command that was only queued is no longer reported as done.
- **Your exact words reach the command** — "tell Claude: \"Don't rename
  Foo.py\"" keeps its capitals, quotes and punctuation.
- Hands-free toggle session was deaf when the wake word was disabled.
- Holding the record key during hands-free now suspends and resumes
  hands-free capture correctly.
- First-run wizard microphone detection; settings minimum widths from font
  metrics; the mic dropdown can choose "System default".
- Hands-free exit freezes (keyboard hook no longer runs session transitions
  inline); a second keyboard hook was removed.
- Bundled wake-word models are now in the build (v0.22.1 lacked one).
- Rich-editor double paste and first-character doubling in Chromium.
- Config backups before migration and a guard against overwriting config
  with defaults; invalid config structure is detected before migrating.

### Changed

- **Commands that can't be undone now ask first; spoken commands don't.**
  A command you say yourself runs straight away unless its effect is
  irreversible (e.g. permanent delete). Anything proposed by Ava asks "yes?"
  first. The question is Samsara's own wording, "yes" must be said on its
  own, a pending question expires after 30 seconds ("wait" extends it once),
  and "stop" cancels anything still in flight.
- **Faster start** — warm boot to "Ready" 6.5 s -> 3.9 s: wake-word models are
  not loaded when the wake word is off, speech output starts in the
  background, and the microphone calibration is remembered.
- Clipboard paste is used everywhere by default; typed injection is opt-in
  per app.
- Audio ducking runs in a separate helper process and ducks all active
  output devices; a persistent idle duck and a deeper duck while capturing.
- The old AI command mode is replaced by the command-first Ava session.

### Under the hood

- Dispatch contract: `DispatchResult` states (miss / matched / queued /
  completed / failed / rejected / cancelled); `succeeded` means COMPLETED.
- Plugin identity: each plugin loads once under its canonical package name;
  background services start from an explicit app call, not on import.
- Argument spans: matching runs on a normalised view with offsets; commands
  receive the original-text argument. Command metadata is preserved verbatim
  (`tools/dump_command_metadata.py`), undeclared fields are `unknown`.
- Execution policy (`samsara/execution_policy.py`): one choke point with a
  capture-time request generation, local confirmation templates, strict
  argument schemas on model routes, a single-use target-bound pending
  confirmation, and a stop path that bumps the generation first.
- One-shot config migrations, per-thread boot timers, cached polyphase
  resampling filter.
- Flight recorder and incident bundler; zero thread-discipline violations;
  the full test suite green; release CI workflow and a SignPath-ready draft
  build workflow; five-minute-take rehearsal tool (`tools/demo_rehearsal.py`).

### Known issues

From the 2026-09-13 hot-path reliability review; none are fixed in this beta.

- A microphone recovery attempt that races with a stop or restart can bring a
  stopped audio stream back to life.
- The wake-word utterance queue has no session identity or size limit: after a
  slow decode, an old utterance can still run after you exit and re-enter.
- Two ducking volume changes can finish out of order, leaving media quieter
  (or louder) than intended.
- If the ducking helper restarts mid-duck, the original volume can be lost, so
  audio may stay turned down.
- If console output breaks, a copied image or file list on the clipboard can be
  lost when Samsara pastes dictation.
- Packaged beta builds cannot check for updates: the updater accepts only
  stable version numbers and reports a metadata error instead.

## [0.22.1] - 2026-07-18

### Added

- **Privacy-explicit one-click updates for packaged Windows builds** — users
  can check GitHub Releases manually or explicitly enable a once-daily check;
  automatic checks are off by default and source launches never check. The
  request goes directly to GitHub and contains no audio, dictation, history,
  configuration, keys, or device names, although GitHub receives ordinary
  connection metadata such as the IP address, time, and user agent.
- **Verified portable update swap** — Samsara verifies the release ZIP against
  its published SHA-256 before replacing application files. Profile data and
  configuration remain outside the application folder, and the ten allowlisted
  CUDA runtime DLLs are carried into the new CPU build. Custom commands and
  drop-in command plugins are backed up and migrated. This is an integrity
  check, not a claim that the application is code-signed.
- **Lotus-wheel app icon** on the window, taskbar, and EXE, plus a taskbar
  AUMID for correct taskbar grouping. The tray icon keeps its animated status
  wheel by design — it is a live status indicator, not the static app icon.
- **App lifecycle voice commands** — 9 new commands to start, stop, and
  restart Strata, Ariadne, and the Sigil server by voice.
- **In-app feedback workflow** — structured bug and beta-feedback reporting
  from Settings and always-on voice commands, with user-triggered safe
  diagnostics and Live Log access; logs, secrets, paths, devices, and
  dictated text are kept out of the copied summary. Paired with more focused
  beta issue/feedback forms on GitHub.

### Fixed

- **Long free-form dictations no longer lose middle content** — the
  vocabulary prompt was removed from all free-form decode paths, a
  sanity-triggered auto-retry now acts as a safety net, and new
  param-contract tests lock the decode call shape in place. Long recordings
  also now decode as a single whole with per-segment quality gating, and a
  plausible result is preserved instead of being discarded outright after a
  quality fallback.
- **Hands-free toggle command-mode utterances are now gated against
  hallucination** — a spuriously recognized command can no longer hijack a
  hands-free session.
- **Audio ducking now affects all output devices, not just the default one.**
- **Settings window layout overhaul** — card/row redesign, wordwrap clipping
  at live widths, opaque background bars painting over checkboxes and labels,
  button truncation, and the settings search bar.
- **Tray context menu no longer anchors to a stale screen position.**
- **Silent mid-decode data loss on long hotkey dictations now fails loud**
  instead of quietly returning a truncated result, via new suspected_loss
  diagnostics.
- **Correction-store hardening** — empty-overwrite guards and corrupt-file
  quarantine protect the correction store from silent data loss.

### Improved

- **Splash screen overhaul** — richer progress and status detail with
  coordinated startup completion.
- **Updater hardening** — reparse-point checks across the full path chain and
  a closed rollback-directory leak path, with accompanying README/RELEASING
  doc updates for the manual-download bootstrap this release requires.

v0.22.1 is the first build containing the updater, so users on v0.22.0 must
download and extract v0.22.1 manually. Subsequent compatible releases can be
installed from inside the packaged app.

## [0.22.0] - 2026-07-15

The hands-free release. Toggle voice control is now one persistent lane where
ordinary speech is buffered as dictation and exact whole-utterance commands
remain available without changing modes. Show Numbers also gains a
browser DOM path alongside its Windows UI Automation fallback.

### Added

- **Persistent HANDS FREE dictation and commands** — natural pauses no longer
  paste partial fragments. Samsara buffers the complete thought until the sole
  utterance "end", pastes it once, and stays hands-free for the next text box.
  Any enabled command or user macro can execute as an exact whole utterance;
  scrolling, Show Numbers, focus, click, submit, field/window movement, and
  app launches coexist with dictation. `literal ...` remains the escape for
  dictating a reserved command phrase.
- **Speech-aware hold-to-record release tail** — capture continues only until
  the user's final speech ends (with a bounded fail-safe), preventing the last
  one or two words from being stranded in the next recording's pre-buffer.
- **DOM-aware Show Numbers for Chromium browsers** — a bundled Manifest V3
  extension and mutually authenticated loopback bridge expose visible page controls,
  including ARIA and contenteditable targets, while UI Automation remains the
  fallback for browser chrome and non-browser applications. Overlay rendering
  now accounts for Windows/Qt DPI conversion and multi-monitor coordinates.
- **Voice-managed vocabulary and corrections** — add, list, remove, and teach
  recognition vocabulary without opening Settings.
- **Themed reminder toasts** — reminders use the shared Qt runtime, batch
  same-tick notifications, avoid stealing focus, and wait until Show Numbers
  closes before appearing.
- **OpenRouter cloud provider** — Ava's bring-your-own-key cloud mode now offers
  OpenRouter alongside DeepSeek, OpenAI, and Anthropic.
- **Configuration backup and restore** — Settings can export and import a
  complete configuration snapshot. Because backups include private values such
  as API keys and webhook URLs, Samsara warns before writing or importing one.

### Fixed

- **Confirmed wake words are no longer rejected by the later RMS gate** — a
  strong OpenWakeWord detection now reaches the wake action even when adaptive
  energy gating would otherwise classify the same quiet phrase as too soft.
- **Show Numbers is reachable from command mode** — "show numbers" is present
  in the command path instead of transcribing as an unmatched utterance.
- **Dense web apps keep their primary targets reachable** — DOM Show Numbers
  ranks navigation links ahead of repeated utility controls, distinguishes
  checkbox/radio inputs from text fields, and supports up to 99 visible hints;
  Gmail message rows no longer disappear behind a screenful of star buttons.
- **Buffered dictation is retained on focus or paste failure** — delivery is
  locked to both the original process and window handle; failures remain
  visible and retryable rather than silently losing the thought.
- **Clipboard changes made during paste are preserved** — DictationApp now
  uses the central sequence-number guard instead of an unconditional stale
  clipboard restore.
- **Reminder delivery is recorded only after display** — failed or suppressed
  notifications retry within a bounded window instead of being marked sent.
- **Profile switching isolates profile-scoped state** — cached settings no
  longer leak across profile changes.
- **History date navigation** can query the selected day directly instead of
  silently returning only the current window.
- **Cloud configuration imports cannot redirect built-in providers** through
  hidden endpoint overrides or silently carry an existing API key to a newly
  selected provider.
- **Incomplete local CUDA packs fail safely** — Samsara now verifies all ten
  required runtime DLLs before selecting GPU inference, reports the precise
  missing files, and falls back to CPU instead of crashing during model load.

### Changed

- **Task List remains strictly local** — v0.22 includes the v0.21.1 privacy
  hotfix and removes the residual Arcana configuration/settings scaffolding;
  focused regressions forbid network imports and requests from the plugin.
- **Pull-request CI runs on Windows** to match Samsara's supported platform and
  Windows-only dependencies.
- **The v0.22 application archive remains a reproducible CPU build** — the
  existing optional CUDA runtime pack was hash-verified against the v0.22 test
  environment and documented for packaged users instead of duplicating 1.13 GB
  of identical binaries.

## [0.21.1] - 2026-07-13

### Fixed

- **Removed: undisclosed "Arcana sync" network call from the Task List
  plugin** — the Tasks plugin silently POSTed the text of every voice-added
  task to `morneis.com` by default, with no Settings control ever exposed to
  see or change this. On investigation, the destination endpoint accepts no
  authentication and no user or device identifier, so this could never have
  synced to an individual user's account in the first place — the "sync to
  my Arcana account" premise was invalid regardless of the default. Rather
  than ship a consent toggle for a feature that can't do what its label
  claims, the network call has been removed outright. The Task List plugin
  now makes no network requests under any configuration; local add / show /
  complete / remove / clear / read task-list behavior is unchanged.

## [0.21.0] - 2026-07-10

The trust release. The theme is dictation you can rely on: an adversarial
review series audited the transcription pipeline end to end (six audits,
independent AI reviewers, findings verified against the code), and the fixes
shipped here. Also: spoken formatting, a live Quick Reference, a plain-English
health readout, and CI-built releases as the first step toward signed
downloads.

### Fixed

- **"You know" no longer vanishes from dictation** — an overzealous filler
  cleanup rule deleted the phrase "you know" from every dictation, anywhere
  in the sentence, whether it was filler or not. It's now comma-anchored
  like the other filler rules: "It's, you know, complicated" still cleans
  up; "you know what I mean" survives intact.
- **A voice-only escape that cannot be blocked** — abort words now use
  word-boundary matching ("report" can no longer accidentally trigger
  "abort"), work from every session lane, and are structurally guaranteed
  to bypass all quality gates: a user with degraded audio can always end a
  stuck session by voice.
- **Wake listener goes fully deaf during hotkey dictation** — previously it
  kept processing your speech in parallel (its suppression only applied
  before speech began), corrupting the shared voice-detection state and
  double-transcribing. One recording, one listener, always.
- **Clipboard preservation hardened** — copied *files* (Explorer) and
  transparent images (CF_DIBV5) now survive a dictation paste; restore is
  atomic (a failure can no longer leave your clipboard empty); a copy you
  make mid-paste is never clobbered (sequence-number guard); a hung
  clipboard owner can't freeze Samsara.
- **Scratch-that can't delete text in the wrong app** — destructive undo
  keystrokes now refuse to fire if the focused window changed since the
  text was dictated, instead of deleting content in whatever app happens
  to be focused.
- **Prefix lane switches are transactional** — "dictate: hello world"
  reverts cleanly (audibly) if delivery fails instead of stranding you in
  the new mode with nothing delivered, and payload spacing survives
  verbatim.
- **Audio-engine correctness pass** — COM handle leak in the output-device
  watcher fixed (was leaking every 2 seconds for the app's lifetime); the
  device-death sound no longer plays from inside the dying audio callback
  (a deadlock risk exactly when a Bluetooth mic drops); shutdown gives every
  thread a fair share of the timeout.
- **Smart Corrections hardening (8 fixes)** — tighter change budget (a
  correction can alter at most 15% of your words), structural artifacts
  (think-tags, code fences) cause the correction to be rejected rather than
  "cleaned," language handling no longer force-translates, cloud fallback
  reports outages accurately, and timeouts are signaled structurally instead
  of by string-matching.
- **Multilingual correctness** — hallucination blacklist now fires only when
  a junk phrase dominates the transcript (mentioning "amara.org" in a real
  sentence no longer deletes your sentence), corrections handle mixed-script
  vocabulary correctly, and Unicode normalization makes visually identical
  text compare equal.
- **Stress Test Wizard actually captures dictation** — it previously watched
  the clipboard and failed every step; it now routes through the real
  dictation pipeline, reads your actual hotkey from config for its
  instructions, and scores long utterances on the complete result.

### Added

- **Spoken formatting tokens** — say "new line", "new paragraph", "insert tab", or
  "bullet" while dictating and get real formatting, applied after
  corrections so nothing mangles it. The ordinary word "tab" stays literal;
  say "press tab" or "next field" as a standalone utterance for the key
  action. Toggleable in config.
- **Quick Reference window** — a tray-menu cheat sheet of your hotkeys, wake
  phrases, send word, lane-switch phrases, and formatting tokens — every
  value read live from your current settings, never hardcoded.
- **Plain-English dictation health verdict** — the Diagnostics panel now
  opens with a sentence a human can act on ("Audio is clear but the model is
  struggling — try a larger model or add problem words to your vocabulary")
  computed from your recent dictations, instead of only a wall of numbers.
- **Tray menu reorganized** — daily actions stay one click away; occasional
  tools grouped under Tools; debug surfaces under Developer. Nothing removed.
- **CI-built releases** — every release is now built by a public GitHub
  Actions workflow from tagged source. This is the prerequisite for signed
  releases (SignPath), coming next.
- **WER benchmark** — opt-in collection of your own dictation samples plus an
  offline accuracy sweep across Whisper models, so "which model hears *me*
  best" is a measurement, not a guess.

### Changed

- **Homegrown echo cancellation disabled by default** — it converged to only
  3–8% cancellation and review concluded it likely added artifacts. It will
  be replaced by an industry implementation (WebRTC AEC3 / OS-level) in a
  future release; the code remains for opt-in.
- **Relicensed to AGPL-3.0** (from BSL) — qualifies Samsara for free
  open-source code signing and grant eligibility.

## [0.20.0] - 2026-07-02

A polish-and-hardening release. No single headline feature — this is weeks of
fixing things that were quietly wrong: dictation hallucinating during silence,
Samsara going deaf when a mic disconnected, onboarding screens with invisible
buttons and clipped text, and a startup path that could lock you out with a
stale lock file. The version jump from 0.12.x to 0.20.0 reflects the scope of
this cleanup, not one single new headline feature.

### Fixed

- **Dictation hallucination during silence** — Whisper no longer fabricates
  text during near-silent holds or short pauses. Replaced leaky heuristics
  with causal fixes (native no-speech/confidence thresholds, per-press state
  reset so one hallucination couldn't poison the next, a speech-presence
  gate on short buffers, and a brief fade on the recording buffer to kill
  mechanical hotkey-click artifacts), and locked the tuned parameters in
  with a regression test suite.
- **Microphone disconnect/reconnect no longer breaks dictation** — unplugging
  a mic or a Bluetooth headset dropping out used to leave Samsara deaf until
  restart. Audio input now survives a device disconnect and resumes
  automatically on reconnect.
- **Invisible onboarding buttons** — the first-run wizard's and tutorial's
  primary buttons ("Next", "Let's go", "Start using Samsara") could render
  as dark text on a dark background, effectively invisible, on a fresh
  install.
- **Clipped onboarding text and buttons** — several wizard/tutorial pages
  had fixed-size boxes sized for an older, smaller font; labels and button
  text could get cut off. Sizing now follows actual text metrics instead of
  hardcoded pixel widths.
- **Stray borders around plain text** — some onboarding and tutorial text
  was rendering with a faint input-box-like outline around it, a Qt
  stylesheet quirk fixed at the source.
- **Startup could get stuck behind a stale lock file** — if Samsara was
  force-killed (crash, Task Manager, a bad update), the next launch could
  refuse to start and report "already running" even though nothing was.
  Samsara now checks whether the process that actually owns the lock is
  still alive and still Samsara before refusing to start, and cleans up
  after a dead one automatically.
- **Duplicate log lines on startup** — some boots logged every line twice
  because of a root-logger setup race; logging now initializes exactly
  once.
- **First-run wizard could leave the app stuck** on a failed launch instead
  of continuing with defaults, and could write its config somewhere that
  vanished on the next update. Both fixed.
- Windowed build no longer crashes on launch when Windows redirects
  stdout/stderr to nothing.
- Config migration no longer breaks on older configs missing the `modes`
  key.

### Changed

- **History window overhaul** — right-click a row for Copy/Delete, or
  double-click to copy instantly. Timestamps now show full date and time
  instead of just time, and the window's look was brought in line with the
  rest of the app.
- **Onboarding redesigned** — the first-run wizard and tutorial now share
  one consistent visual design across every screen. The tutorial's
  interactive Ava step was removed to keep the walkthrough focused on the
  core loop (Ava and "show numbers" are pointed to from the wrap-up
  instead), and wake-word instructions only appear when wake word is
  actually turned on.
- **Splash screen** — the old progress bar is now a spinning, brand-red
  segmented wheel matching Samsara's own icon.
- **Ava and session modes reworked** — Command, Dictate, and Ava now run
  through one unified state machine instead of scattered mode flags, with
  a dedicated Ava lane and a "substance gate" that skips agent calls for
  coughs and stray syllables that aren't real requests.
- **BYOK cloud AI is free** — the license gate on bring-your-own-key cloud
  AI has been removed.
- **Settings reorganized** — mode activation (Command/Dictate/Ava) now
  lives in one consolidated Modes tab instead of being spread across
  several.
- **Earbud-style media controls** — play/pause/next/mute now route through
  Windows' native media transport controls, working reliably across
  Spotify, browser tabs, and other media apps, with per-app Spotify muting.

### Under the hood

- **Thread registry** — every background thread and timer now spawns
  through a single tracked registry instead of bare
  `threading.Thread`/`Timer` calls, so shutdown can account for everything
  instead of leaking daemon threads.
- **Fail-loud pass** — swallowed exceptions across the codebase now log
  instead of silently vanishing; stray debug `print()`s route through the
  real logger.
- **Centralized per-user data directory** — config, logs, and every
  per-user file now resolve through one function, with an environment
  override for isolated testing.
- **Frozen-build smoke harness** — one command builds the release EXE and
  runs it through boot, first-run-wizard, and clean-shutdown checks against
  an isolated profile, catching the class of bug that only shows up in a
  frozen build.

---

## [0.12.0] - 2026-06-30

Hands-free multi-wakeword dictation and microphone-agnostic wake detection.

### Added

- **Multi-wakeword hands-free dictation** — say a named wake phrase ("Hey Claude", "Activate Hermes") to focus that app's window and start dictating into it, fully hands-free. Wake phrases are configurable per target (process-name targeting, restores minimized windows). Custom OpenWakeWord models with Whisper-transcript fallback.
- **Open-ended wake sessions** — a wake-triggered session survives silence (does not end on a short pause), appending each spoken utterance into the target. Ends on inactivity timeout or a spoken send word.
- **Send terminator** — end an utterance with "over"/"send" to submit; only the final spoken word is checked, so the word mid-sentence is typed normally. Per-target send policy: Claude submits on send; agentic targets (Hermes) leave text staged.
- **Earcons** — session-start and sent audio cues for hands-free feedback.
- **Adaptive microphone gate** — wake detection now measures speech relative to a rolling ambient noise floor instead of a fixed energy threshold, so low-output mics (headsets, USB mics with AGC) work without manual tuning. Includes a mic-calibration helper.

### Fixed

- Wake-session inactivity timer no longer races in-flight transcription (last utterance could be dropped).
- Wake corrections no longer self-corrupt on repeated canonical phrases.

---

## [0.9.9] - 2026-05-15

Focused on accessibility, voice-control surface area, and reliability. Adds a floating command cheat sheet, monitor-to-monitor window moves, a phonetic-collision audit, a voice-driven semantic clicking overlay, and an echo-cancellation calibration tool. Also fixes several silent failures around config persistence, CUDA fallback, and Whisper model selection that were degrading user experience without obvious symptoms.

### Added

- **Floating command cheat sheet** — semi-transparent always-on-top window listing your voice commands. Resizable, draggable, click-through-friendly. Real-time filter bar, manual pinning (★/☆) so you can keep your most-used commands at the top, and click-to-execute fires the same command path as voice. 300ms teal flash before execution gives visual feedback. Position, size, opacity, and pinned commands persist to `command_palette.json`. Trigger via tray menu "Command Reference" or voice: "show commands" / "hide commands".
- **Show numbers overlay (experimental)** — voice-driven semantic clicking. "Jarvis, show numbers" labels every clickable element in the foreground window with a number; "Jarvis, click 7" performs a left-click on element 7. Supports "click thirty seven", "click 7 twice" (double), "click 7 right". Uses Windows UI Automation tree, foreground-window scope only. Auto-dismisses on click, hide command, foreground change, or 30s inactivity.
- **AEC calibration harness** — "Calibrate Echo Cancellation" in the tray menu plays a 50ms log-chirp through speakers, records the mic, and cross-correlates to measure speaker-to-mic latency. Reports a confidence-scored lag in milliseconds for tuning `latency_ms` in the echo cancellation config. Replaces guessing with measurement.
- **Phonetic collision audit tool** (`tools/phonetic_audit.py`) — uses CMU pronouncing dict to find command pairs that sound similar. One-shot report identifies homophones, near-collisions, and overlaps with common English speech. Run before any command-naming changes.
- **Command routing test harness** (`tools/test_commands.py`) — validates every registered command (builtins + plugins) can resolve to a handler. Catches broken hotkey keys, missing methods, dead plugin references, and stale paths before users hit them. Run before each release.
- **AI command pack** — open Claude / ChatGPT / Gemini / Perplexity / Midjourney / Anthropic / Hugging Face / GitHub, plus shortcuts to Claude desktop, projects, settings, and new chat. 18 commands total.
- **Accessibility command pack** — Magnifier (zoom in/out), Narrator (start/stop), high contrast, color filters, live captions, mono audio, on-screen keyboard, sticky keys, mouse keys, eye control, big cursor, accessibility settings, focus mode / do not disturb, immersive reader, night light, dark mode, and more. 44 commands total.
- **Window manager voice commands** — "send to far right", "send to far left", "send to middle monitor", "send to main monitor" for multi-monitor setups. "send maximized right" / "send maximized left" handle maximized windows automatically. Standard "move to next monitor" / "move to right monitor" for single-step moves.
- **Steam navigation commands** — "steam library", "steam friends", "steam store", "steam downloads" via `steam://` protocol URIs.
- **Twitch commands** — "open twitch", "open twitch directory", "open twitch following".
- **Obsidian command pack** — 27 commands for new note, quick switcher, palette, search, replace, graph view, daily note, indent/unindent, and more.
- **Per-monitor DPI awareness** — Samsara now calls `SetProcessDpiAwareness(2)` at startup. Fixes click-coordinate offset issues on 150% scaling and HiDPI displays.
- **Bluetooth mic refresh** — mic list refreshes when the tray menu opens. New mics (especially BT earbuds) appear without restarting Samsara. Name-based reconciliation handles unstable PortAudio device indices across reconnects. Skips refresh during active capture.
- **Whisper `.en` model variants in Settings** — `tiny.en`, `base.en`, `small.en`, `medium.en` now appear in the model dropdown. `small.en` is the recommended sweet spot for English speakers on GPU.
- **Continuous mode** has its own speech threshold (`continuous_speech_threshold`, default 0.03), independent of wake word threshold which is intentionally higher to avoid false activations.

### Changed

- **Splash screen stays visible until the model has actually loaded.** Previously closed after 3 seconds even though Whisper could still be loading for another 20-60s, leaving users wondering why dictation wasn't working. Now reflects real readiness.
- **Settings window opens at 920×700** (was 700×700). With the additional packs and tabs added this release, the old size squished tabs together.
- **Media commands route through SMTC plugin** — "pause music", "play music", "next track", "previous track" now use Windows SMTC (System Media Transport Controls) via the `media_keys` plugin. Replaces broken pynput `playpause` / `nexttrack` / `prevtrack` virtual-key strings that never actually worked. Reliable across Spotify, browser tabs, Stremio, and any other media app.
- **Phonetic-aware command aliases** — `remove file` added as the preferred trigger for file deletion (was `delete file`, phonetically too close to `delete line`). Old phrase kept as alias.
- **Streaming dictation direct-paste** uses validated Ctrl+Z + Ctrl+V cycle (ARC-reviewed, empirically tested). Word-boundary selection replaced.
- **Tray menu "Command Reference"** toggle opens / hides the cheat sheet.

### Fixed

- **Config save no longer wipes external edits.** `save_config()` now deep-merges in-memory config over the on-disk file before writing. Previously, editing `config.json` externally while Samsara was running would silently lose those changes when Samsara exited and overwrote the file with its in-memory state. This was the root cause of the recurring "command packs disappear after restart" pattern.
- **CUDA detection works correctly.** The Settings dropdown previously hid the CUDA option when `cublas64_12.dll` wasn't findable, which made it look like CUDA support was missing. Added README documentation and detection guidance. Samsara also no longer silently falls back to CPU at model load when the user explicitly selected CUDA — a clear log line surfaces what happened.
- **Command mode (Right Ctrl)** now actually executes commands. A wrong-object `hasattr` guard in `CommandExecutor.process_text` was blocking every execution path. ARC tribunal review identified the root cause.
- **Click-to-execute in cheat sheet** uses the same code path as wake-word command mode (`force_commands=True`). Previously called a non-existent keyword argument and silently no-op'd.
- **VAD/Silero error spam** — TorchScript exceptions from Silero VAD are now rate-limited (one log per 30s), the model state is reset on error to attempt recovery, and after 50 consecutive failures Samsara disables VAD for the session and runs on RMS-only speech detection. Previously dumped a 30-line traceback on every audio chunk during a state-corruption episode.
- **Continuous mode** now reads from `continuous_speech_threshold` instead of `wake_word_config.audio.speech_threshold`. The wake threshold (typically 0.15) was too high for normal-volume speech, so continuous mode captured nothing.
- **Settings model dropdown** displays the currently-selected `.en` variant correctly. Previously fell back to showing `base (~150 MB)` for any `.en` model because the display map didn't include them.
- **Splash screen** is now closed by the model-load worker thread on completion, not pre-emptively closed by the main thread before the worker even starts.
- **Continuous mode toggle wiring fix** — Right Ctrl + voice commands now properly enter command mode regardless of starting state.

### Known issues

- Some commands may still trigger Whisper's `no_speech_threshold` filter (default 0.6) and be silently discarded on borderline audio. Working as designed, but worth knowing if a transcribed phrase mysteriously fails to appear.
- The show numbers overlay currently uses Tkinter with `-transparentcolor` which can darken the entire screen on some Windows configurations instead of showing crisp labels. A Win32 layered-window rewrite is planned for the next release.
- VAD/Silero state corruption is suppressed in the logs but the underlying torch issue remains. Doesn't affect functionality (RMS fallback works) but if you see persistent `[VAD] inference error` lines, the dictation pipeline is still operating correctly.

---

## [0.9.8] - 2026-05-01

This release bundles all work since v0.9.5: the new main hub window, dictation history, streaming dictation, and four new plugins (Hyperion lights, FlashForge 3D printer, Spotify music, screen recording / GIF search). Also includes substantial reliability fixes for hold-to-dictate, wake word recognition, and audio recovery after sleep/wake.

### Added

- **Streaming dictation** — real-time voice-to-text with live overlay. Hold the streaming hotkey (CapsLock) and watch text appear and update as you speak, with a polished final paste on release. Two modes: overlay-only (safe) and direct-paste (text flows into the focused app in real time). Configurable via `streaming_mode` and `streaming_direct_paste` in config.
- **CapsLock streaming hotkey** — suppressed at the OS level so it never toggles caps while Samsara is running. atexit handler guarantees the hook is released even on crash, so users never end up with a stuck CapsLock key.
- **Main hub window** — opens on launch with sidebar navigation (History, Dictionary, Settings). Closing minimizes to tray. Double-click tray icon to reopen.
- **Dictation history** — SQLite database logging every transcription with timestamp, source app, raw text, cleaned text, duration, mode, and success/fail status. Searchable, with copy/retry/delete.
- **Unified dictionary UI** — three-tab corrections manager (Vocabulary, Corrections, Wake Words) in the main window. Add, edit, delete corrections from the UI. User corrections stored in `~/.samsara/` as JSON, hot-reloaded without restart. Hardcoded defaults are read-only; user overrides merge on top.
- **Grammar-Lite cleanup** — post-Whisper processing removes filler words (um, uh, like), fixes capitalization, adds missing punctuation. Two modes: Clean (default) and Verbatim. Raw transcript always preserved in history.
- **Hyperion LED strip control** — voice-controlled ambient lighting via Hyperion JSON API. "Jarvis, lights red", "lights effect rainbow", "lights off". Supports hostname, IPv4, and IPv6. 11 preset colors, 14 effect aliases with fuzzy matching.
- **FlashForge 3D printer control** — voice commands for printer status, pause/resume/cancel, chamber light toggle, file listing. TCP M-code protocol on port 8899. Tested on AD5X firmware v1.2.3.
- **Spotify music playback** — "Jarvis, play me some music" opens tracks in the Spotify desktop app. Pre-configured library, user-configurable music_library, volume control, Spotify search fallback.
- **Timer plugin** — "set a timer for 5 minutes" with natural language parsing, background thread, Windows notification on completion.
- **GIF search plugin** — "search for a gif of dancing cat" opens Giphy.
- **Screen recording plugin** — "record my screen" / "record this window" captures screen to GIF using mss (DXGI). Persistent red REC indicator, 30-second safety cap, active window capture via ctypes.
- **Audio auto-reconnect** — stream health monitor detects dead PortAudio streams after sleep/wake, auto-reconnects with exponential backoff (max 5 retries). Windows toast notification on reconnect.
- **Wake word corrections** — harvest, charge us, charge, driver's, drivers added to Jarvis recognition.
- **Music command mishearing aliases** — Whisper sometimes hears "place of music" or "plays some music" instead of "play some music". All common variants now route to the same handler.

### Changed

- Streaming first-chunk latency reduced from 1.3s to 1.0s (0.7s first chunk + 0.3s transcription).
- Streaming update interval tightened from 1.5s to 1.0s between partials.
- Partial transcriptions use beam_size=1 (greedy, fast), final pass uses beam_size=5 (full beam search).
- Pre-buffer skipped in streaming mode to eliminate 1.5s of unnecessary initial latency.
- Direct-paste cycle uses Ctrl+Z + Ctrl+V instead of select-and-replace by word boundary. Works reliably across Notepad, browser fields, and Obsidian. Tunable via `UNDO_SETTLE_S` and `PASTE_SETTLE_S` constants.
- History tab performance: callback-based updates replace 5-second polling, pagination limits initial load to 50 entries.
- Main window UI restyled with blue-teal tinted dark theme.
- Voice Training window refactored into reusable CTkFrame components (HistoryFrame, DictionaryFrame) shared between main window and standalone windows.

### Fixed

- **Hold-to-dictate VAD bypass** — faster-whisper's internal VAD was stripping 80% of speech during explicit hotkey recordings. Now disabled for hold-to-dictate and long dictation modes.
- **Min audio guard** — Whisper hallucinations ("Thank you", "Subtitles by Amara") on sub-0.5s clips. Audio shorter than 0.51s is now silently discarded.
- **No ghost typing** — unrecognized wake word commands no longer paste garbage into focused apps. No-match goes silently back to sleep.
- **History tab close** no longer spams TclError from stale widget redraws.
- **Tk shutdown-race filter** installed on the root window. Suppresses benign "invalid command name" / "application has been destroyed" TclErrors that fire from CTk widget `<Configure>` handlers during teardown. Real exceptions still surface.
- **Quiet third-party loggers** — torio, torchaudio, urllib3, huggingface_hub pinned to WARNING. voice_training.py raises root logger to DEBUG at import, which was producing FFmpeg traceback spam on every probe.
- **Streaming module imports** — pyautogui and pyperclip moved to top-level imports. Inline imports in the hot path were an anti-pattern.

## \[0.9.5\] - 2026-04-24

### Added

- **BSL-1.1 license** — replaces MIT. Free for non-commercial use, converts to MIT April 2030.
- **Silero VAD v5 fixed** — now receives correct 512-sample chunks at 16kHz (was getting 1600 samples). Returns True if ANY 512-sample window contains speech.
- **Raw mic pipeline** — VAD runs on raw microphone signal, not AEC output. AEC was amplifying noise 10,000x instead of cancelling. Speech buffer stores raw mic audio for Whisper.
- **Wake word corrections** — "charvus", "jervice", "jervis", "service" → "jarvis".
- **\[HEAR\] debug trace** — every transcription logs what Whisper heard or why it returned empty.
- **Buffer cap** — 7-second speech buffer cap with stuck buffer detection. Prevents infinite accumulation from ambient noise fooling VAD.

### Changed

- Old MIT releases (v0.9.0–v0.9.4) deleted from GitHub.
- README badge updated to BSL-1.1.

## \[0.9.4\] - 2026-04-21

### Added

- **Phonetic wash** — post-transcription correction layer. Fixes common Whisper misrecognitions ("open crow" → "open chrome", "you two" → "YouTube").
- **Command registry** — token-based longest-match resolver with frozen phrase table. Handles prefix overlaps deterministically.
- **6 plugins shipped** — macros, audio switching, tab finder, web shortcuts, quick ask (ARC IPC), example greeting.
- **Ko-fi integration** — support link in README and license section.
- **Test suite** — 347/347 tests passing across 8 test files.

### Changed

- Command matching upgraded from substring to token-aware longest-match.
- README rewritten with architecture diagram and feature matrix.

## \[0.9.3\] - 2026-04-21

### Added

- **Wake word debug window** — structured trace events, evaluation panel, decision timeline, token-aware wake phrase matcher.
- **Snooze from tray** — pause all listening for 5/15/30/60 minutes or until manually resumed.
- **Listening indicator** — borderless always-on-top pill showing current mode, pulses teal during capture.

### Fixed

- Wake word substring match bug — "samsara-like" no longer falsely triggers on wake phrase "samsara". Now uses token-bounded matching.

## \[0.9.2\] - 2026-04-18

### Added

- **4-state dictation model** — Replaces fragmented dictate/short/long modes with clean state machine: Asleep → Command Window → Quick Dictation → Long Dictation. Designed through ARC (multi-AI review process).
- **Auto-calibrate speech threshold** — Measures ambient noise on startup (1.5s), sets threshold using IQR-based outlier rejection. Floor of 0.0005 (not 0.01). Re-calibrates on mic switch. Configurable via Settings (Auto/Manual toggle).
- **Frequency-domain echo cancellation** — Replaced sample-by-sample NLMS with block FFT-based adaptive filter. 4096 taps at 16kHz = 256ms echo path. Fully vectorized (no Python for-loops in signal path). Diagnostic logging.
- **Command parser module** — `samsara/command_parser.py` extracts wake word command parsing into pure, testable functions. `parse_wake_command()` returns structured intent dicts (type/name/content/raw). Handles dictation keywords, filler stripping, Whisper punctuation, colon/dash separators, and joined tokens. 32 tests.
- **Wake word observability** -- Debug window upgraded to structured trace pipeline
  - Wake Word Evaluation panel shows match decision (type, index, YES/NO) per utterance
  - Decision Timeline groups all pipeline stages per utterance in a scrollable view
  - Export Timeline button writes trace to `docs/wake_word_trace_YYYYMMDD_HHMMSS.txt`
  - Main app pipeline events appear in debug window when open (optional trace callback)
  - New shared module: `samsara/wake_word_matcher.py` -- token-aware phrase matching
- **Snooze listening** -- Tray submenu to temporarily pause all listening for 5/15/30/60 min or indefinitely, then auto-resume; hotkeys are ignored while snoozed
  - Active streams (continuous, wake word) are stopped and restored on resume
  - Tray tooltip shows snooze state and resume time
  - Alarm hotkeys still work while snoozed
- **Listening state indicator overlay** -- Small always-on-top pill window shows current mode and pulses teal (#00CED1) while audio is actively captured
  - Flashes green on successful dictation, red on errors/cancellation (fades back smoothly)
  - Configurable position: top/bottom + left/center/right (default: bottom-center)
  - Settings in General tab: enable/disable toggle and position dropdown
  - Toggleable via tray menu "Show Listening Indicator" (persisted in config)
  - Dismissable with middle-click; positions within work area to avoid taskbar overlap
  - New file: `samsara/ui/listening_indicator.py`
- **Audio pre-buffer system** — Rolling 1.5s circular buffer captures audio before hotkey press
  - First words are never lost to startup delay (sound cue, stream initialization)
  - Pre-buffer audio prepended to recording data automatically
  - Standalone pre-buffer stream for hold/toggle modes
  - Wake word stream feeds pre-buffer in combined/wake_word modes
  - Log prefix `[PRE]` shows captured pre-buffer duration
- **Macro command example** — "jump five times" demonstrates chained/repeated actions
- **Standalone Windows EXE distribution** — No Python installation required
  - Single-folder executable built with PyInstaller
  - Reduced from 5.8 GB to 1.9 GB (1.07 GB compressed)
  - Whisper models download on first run (not bundled)
- **Tray mode switching** — Switch between all 5 modes (Hold, Toggle, Wake Word, Combined, Continuous) directly from tray right-click menu with radio checkmarks; no settings dialog needed. Central `apply_mode()` handles all state transitions.
- **Wake word correction map** — `samsara/wake_corrections.py` provides a token-level substitution map for known Whisper misrecognitions (e.g. "charvis" → "jarvis"). Applied before matching in both main app and debug window. Trace pipeline shows both RAW and CORRECTED text for easy pattern discovery.
- **Plugin command system** (scaffold) — `samsara/plugin_commands.py` with `@command` decorator, global registry, alias support, and auto-loader for `plugins/commands/*.py`. Not yet wired into `CommandExecutor.process_text`.
- **Echo cancellation module** — `samsara/echo_cancel.py` frequency-domain block NLMS with FFT overlap-save. WASAPI loopback capture for reference signal. Windows-only, disabled by default. Periodic diagnostic logging.
- **Constants module** — `samsara/constants.py` extracts magic numbers (sample rates, thresholds, timing values) from [dictation.py](http://dictation.py)
- **Pipeline tests** — `tests/test_pipeline.py` end-to-end simulation using real modules (wake_word_matcher, wake_corrections, command_parser). 21 tests.
- **Calibration tests** — `tests/test_calibration.py` validates IQR outlier rejection and threshold calculation

### Changed

- **UI extraction** — SettingsWindow, FirstRunWizard, HistoryWindow, SplashScreen extracted from [dictation.py](http://dictation.py) into `samsara/ui/`. [dictation.py](http://dictation.py) reduced from 6,555 to 3,592 lines.
- **Settings performance** — Lazy tab loading + generator-based staged building. Only builds the visible tab; others build on first click.
- **Thread-safe buffer** — `buffer_lock` added around all `speech_buffer` access to prevent race conditions between PortAudio callbacks and transcription.
- **Clipboard error logging** — Replaced 4 silent `except: pass` patterns with `_log_error()` calls for diagnosability.
- **Clipboard delay** — Reduced from `sleep(0.4)` × 3 = 1.2s per paste to configurable `sleep(0.05)` × 3 = 0.15s.
- **Stale module cleanup** — Moved deprecated [audio.py](http://audio.py), [config.py](http://config.py), [speech.py](http://speech.py)to `samsara/_stale/`.
- **Wake word is now a boolean, not a mode** -- `wake_word_enabled` config flag replaces the old `wake_word` and `combined` capture modes. Three capture modes remain (hold, toggle, continuous); wake word runs alongside any of them.
  - Tray menu shows "Wake Word" as a checkable item instead of two radio entries
  - Settings shows a checkbox beneath the capture-mode radios
  - `Ctrl+Alt+W` hotkey toggles `wake_word_enabled` on/off
  - Old configs with `mode='wake_word'` or `mode='combined'` auto-migrate to `mode='hold' + wake_word_enabled=true`
  - Snooze correctly saves/restores `wake_word_enabled` state
  - Tray tooltip shows combined state: "Hold + Wake", "Continuous", etc.
  - Listening indicator pill shows the same combined label
  - Tray icon chase animation runs while wake word listener is active
  - Icon stays animated after recording ends if wake word is still listening
  - Removed all dead 'wake_word'/'combined' mode references from code, tests, and docs
  - Updated README mode table, [ARCHITECTURE.md](http://ARCHITECTURE.md) state diagram, WAKE_WORD_GUIDE.md
- **Tray mic switching** — Now correctly stops and restarts all active audio streams (pre-buffer, wake word, continuous) on the new device. Previously only updated config without restarting streams, so the old mic kept recording. Uses closure-pattern callbacks to avoid pystray's 2-arg callback limitation.
- **Config save is now atomic** — Writes to `.json.tmp` first, rotates the existing config to `.json.bak`, then atomically promotes the temp file via `os.replace`. Prevents truncation/corruption if serialization fails mid-write.
- **Dual sample rate architecture** -- Capture at device native rate (44.1/48kHz), resample to 16kHz for Whisper. Fixes WASAPI "Invalid sample rate" errors.
  - All 5 stream sites updated to use `self.capture_rate`
  - `resample_audio()` via `np.interp` -- lightweight, no new dependencies
  - Reverted DirectSound workaround back to WASAPI (proper API, no duplicates)
  - `_detect_capture_rate()` queries device on init and mic switch
  - Wake Word Debug window also captures at native rate and resamples
- **Speech threshold default** raised from 0.01 to 0.03 RMS across all modes (config, continuous callback, wake word callback, debug window). The old default was below ambient noise floor for most environments, causing perpetual "Speaking" state that prevented silence detection from firing.
- **Toggle mode tray feedback** — `start_recording` sets tray icon to teal + tooltip to "RECORDING"; `stop_recording` restores idle state. Critical for toggle mode where there's no physical key-hold to indicate recording state.

### Fixed

- **Wake Word Debug performance** -- Reduced UI thread load by \~80%
  - Unified audio level meter + timer into single 4 Hz poll loop (was 10 Hz each)
  - Added change-detection guards to skip redundant widget reconfigs
  - Batched log textbox inserts (flush every 200ms instead of per-message)
- **Settings window performance** -- Eliminated save-time lag and faster open
  - Removed force-build of unvisited tabs on save; reads config directly instead
  - Microphone enumeration moved to background thread (window opens instantly)
- Replaced PyTorch CUDA detection with ctranslate2 native method
  - Eliminates 4.7 GB torch dependency
  - Uses `ctranslate2.get_supported_compute_types()` instead of `torch.cuda.is_available()`

### Fixed

- **Wake word substring false-positive** -- `wake_phrase in text` replaced with token-aware `match_wake_phrase()` in both [dictation.py](http://dictation.py) and wake_word_debug.py; "samsara-like" no longer triggers wake phrase "samsara"
- **Listening indicator vanishes behind taskbar** -- Periodically re-asserts topmost; positions inside the work area (excludes taskbar) instead of full screen
- **Listening indicator settings** -- Added enable/disable toggle and position dropdown to Settings &gt; General; default position changed to top-right to avoid taskbar overlap
- **Wake word + hotkey contention** — Wake word transcription now pauses during hotkey recording
  - Eliminates 200-800ms GPU contention delay when pressing hotkey in combined mode
  - Wake word audio stream continues running (feeds pre-buffer) but skips transcription
  - Processing resumes automatically when hotkey recording ends

### Removed

- PyTorch, TensorFlow, Keras (not needed for faster-whisper)
- OpenCV, numba, librosa, pandas, scipy (unused dependencies)

### Changed

- ARC refactored: registry router, dead code removal, running guard, persistent config, collapsible UI.

## \[0.9.1\] - 2026-04-18

### Added

- **ARC created** — Adversarial Reasoning Chain, multi-AI orchestration tool. Builder/Challenger/Auditor pipeline with Claude, GPT, and Gemini.
- **Tray quick-switch** — mode submenu in system tray with radio buttons.
- **Echo cancellation** — frequency-domain AEC with loopback capture.
- **Auto-calibration** — ambient noise measurement with IQR-based outlier rejection.
- **4-state dictation model** — hold, toggle, wake word, continuous modes.

### Changed

- Architecture docs created ([ARCHITECTURE.md](http://ARCHITECTURE.md)).
- Tray icon redesigned (teal theme).

## \[0.9.0\] - 2026-02-10

### Added

- Initial release. Hold-to-dictate with Whisper, system tray, basic voice commands, voice training module, configurable hotkeys, CUDA support.

---

## \[0.9.0\] - 2026-02-09

### Added

- **Alarm Reminders** — Persistent notifications with sound
  - Interval-based alarms (hydration, stretching, breaks)
  - Plays sound repeatedly until dismissed with hotkey (default: F11)
  - Configurable nag/repeat interval (default: 60 seconds)
  - Built-in sounds: alarm, chime, bell, gentle
  - Support for custom WAV/MP3 sound files
  - New "Alarms" tab in Settings

### Fixed

- Audio callback exception handling — prevents silent crashes during audio processing
- Thread safety for Whisper transcription — mutex lock prevents race conditions

---

## \[0.8.0\] - 2026-02-04

### Fixed

- **Critical: Clipboard preservation on 64-bit Windows**
  - Windows API handles were overflowing due to 32-bit integer casting
  - `save_clipboard()` now properly preserves all formats (images, files, HTML, etc.)
  - User clipboard no longer destroyed after every dictation

### Added

- Centralized `samsara/clipboard.py` module with shared lock

---

## \[0.7.0\] - 2026-01-30

### Added

- **Sound Theme System** — Four built-in themes
  - `cute` — Playful bloops (Nintendo/Duolingo style)
  - `warm` — Rich chords (OS boot sound vibes)
  - `zen` — Singing bowls and chimes
  - `chirpy` — Bright bird-like chirps
  - Theme selector in Settings → Sounds tab
  - Create custom themes by adding folders to `sounds/themes/`
- **Multi-format audio support** — MP3, OGG, FLAC, M4A (requires pydub + ffmpeg)

### Fixed

- Start sound not playing (winsound conflict with InputStream)
- Wake word response time — reduced silence detection threshold
- Speech cutoff after wake word
- Settings persistence — mode changes now take effect immediately
- Exit from system tray now works reliably

---

## \[0.6.0\] - 2026-01-23

### Added

- **Pause Word Support** — Say "hold on" during dictation to reset silence timer
  - Allows thinking mid-dictation without triggering timeout
  - Configurable phrase (pause, hold on, wait, or custom)
- **Dictation Auto-Finalization** — Text outputs after silence timeout
- Wake word debug window improvements (timer display, flow indicator, mode selector)

### Fixed

- Audio cue latency — persistent output stream eliminates device re-acquisition delay
- Clipboard preservation now handles all Windows formats (images, files, rich text)

---

## \[0.5.0\] - 2026-01-21

### Added

- **Expanded Wake Word System**
  - Configurable wake phrase (samsara, hey samsara, computer, jarvis, custom)
  - Three dictation modes: dictate (2s timeout), short dictate (1s), long dictate (waits for end word)
  - End word support (over, done, go, send, execute, etc.)
  - Cancel word support (cancel, abort, never mind, scratch that)
- Enhanced Settings UI with dropdowns for all wake word options

---

## \[0.4.0\] - 2026-01-20

### Added

- Voice Training button in Settings → General tab
- Profile Manager height increased to show all buttons

### Fixed

- App not fully closing on exit — all windows now properly destroyed
- Renamed "Dictionary Profiles" to "Voice Training Profiles" for clarity

---

## \[0.3.0\] - 2026-01-15

### Added

- Scrollable Settings tabs — content no longer cut off
- Model size dropdown shows disk space requirements
- Created Samsara-dev folder for active development

---

## \[0.2.0\] - 2026-01-11

### Added

- **Modular Package Structure** (`samsara/`)
  - `samsara.config` — Configuration management
  - `samsara.audio` — AudioCapture and AudioPlayer
  - `samsara.speech` — SpeechRecognizer and TextProcessor
  - `samsara.commands` — CommandExecutor
  - `samsara.ui` — UI components
- **Comprehensive Test Suite** — pytest-based with mocked dependencies
- **Dictation History Panel** — View, copy, clear recent transcriptions
- **20+ Punctuation Commands** — period, comma, question mark, parentheses, etc.
- **Cancel Recording Hotkey** — Escape to abort without transcribing
- **Sound Volume Slider** — 0-100% adjustment in Settings
- **Auto-Start with Windows** — Option in Settings → General
- **Auto-Capitalize Sentences** — Capitalizes after . ! ?
- **Number Formatting** — "twenty one" → "21"
- Voice Commands Manager in Settings with add/edit/delete/test
- Custom sound file support (WAV)

### Fixed

- Treeview headers invisible in dark mode
- Dictation history not persisting across restarts

---

## \[0.1.0\] - 2026-01-09

### Added

- **First Run Experience**
  - Splash screen with animated loading progress
  - Setup wizard (microphone, hotkey, model selection)
- **Launcher System**
  - `scripts/_launcher.vbs` for silent background launch
  - `install.bat` and `build.bat` scripts
- **Dictation Modes** — Hold, Toggle, Continuous, Wake Word
- **Voice Commands** — 41 predefined commands in `commands.json`
- **Voice Training Module**
  - Microphone level monitor
  - Custom vocabulary and corrections
  - Export/Import training data
- **Modern UI** — CustomTkinter dark theme with tabbed Settings

### Fixed

- Unicode emoji crashes on Windows console (replaced with ASCII)
- Settings/Voice Training buttons not visible

---

## \[0.0.1\] - 2026-01-08

### Added

- Initial release
- Python-based speech-to-text using faster-whisper
- GPU acceleration with CUDA
- Multiple Whisper model sizes (tiny to large-v3)
- System tray integration
- Global hotkey support
- JSON configuration
