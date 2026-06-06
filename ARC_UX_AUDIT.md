# Samsara — Top-Level UX Audit

**ARC Review | Scope: First contact → initial settings configuration**
**Focus: Ease of use · Accessibility · Retention drivers**

---

## 1. Discovery

**Primary path — website executable**
The intended funnel. Landing page covers philosophy, feature count, and stats. This is doing useful work: it sets an expectation before the user runs anything. The philosophy framing ("local, private, voice control") is a stronger retention hook than a feature list because it signals *why* the app exists, not just what it does. The docs page is detailed and available from day one.

**Secondary path — GitHub**
Accessible to technical users but creates a steeper setup cost for general users (Python environment, dependencies, no packaged experience). For the accessibility-primary user (chronic pain, limited mobility) this path is effectively closed. The website executable should be the only path that matters for UX audit purposes.

**Gaps at discovery:**
- Screenshot count on the landing page is reportedly low. First impressions for a voice-control app depend heavily on seeing it in action — a short screen-recorded demo (30 seconds showing hold-mode dictation, show numbers, one command) would do more work than any feature list.
- No clear "who is this for" signal at the top of the landing page. The use-case split (chronic pain / privacy / power user / just dictation) is defined inside the wizard but not surfaced on the website, so users arrive without a mental model for which version of Samsara they're about to get.

---

## 2. Installation

Single executable download. User double-clicks. No complex setup chain. This is correct — installation friction is effectively zero for the target audience.

**What happens on first run:**
Splash screen → first-run wizard (no config file detected) → main app state. The splash screen bridges the launch delay while the model and audio devices initialize. Appropriate use of that time.

**One concern:** if the splash and wizard are the first things a user sees, the visual quality of both sets the tone for the whole product. Worth auditing their polish independently.

---

## 3. First-Run Wizard

**6 steps: Welcome → Use Case → Microphone → Model → Shortcuts → Complete**

The wizard's job is configuration, and it does that. The user leaves it with:
- A microphone selected and optionally tested
- A model size chosen (tiny/base/small)
- Hotkeys set
- A use-case profile applied (which adjusts some defaults silently)

**What the wizard does well:**
- Use case selection (chronic pain / privacy / power user / just dictation) is the right framing. It signals the app is aware of different audiences.
- Microphone test is in-wizard — users can verify audio is working before they're in the app proper.
- Hotkey configuration up front eliminates the "nothing works and I don't know why" failure state.

**What the wizard does not do:**
- It does not demonstrate anything. The user completes 6 screens and has still never made the app do something. No dictation sample, no command example, no visual of what "success" looks like. This is the single biggest gap in the onboarding chain.
- The use-case selection changes config defaults but does not change the subsequent wizard content or the post-wizard experience. A "chronic pain" user and a "power user" see identical screens after step 2. The personalisation is invisible.
- No explanation of what the tray icon is or will do. The user is about to live in a tray-icon-first app and the wizard never mentions it.

---

## 4. Tutorial (Post-Wizard, Auto-Launch)

**Steps: Welcome → Dictation → Command → Show Numbers → Ava (if configured) → Done**

This addresses the wizard's core gap: the user now performs each core action once before they're on their own. Each step requires the actual gesture (hold key, speak, release) and returns real feedback.

**What the tutorial does well:**
- Hands-on. The user proves to themselves that each thing works before being left alone.
- Each interactive step has a 20-second timeout → hint → skip path. Nobody gets trapped.
- Done screen surfaces the three advanced guides (Mic Setup, Ava Guide, Voice Training) as clickable cards. This is the only point in the onboarding chain where the user is shown "here is what you could do next."
- `tutorial_complete` flag prevents auto-relaunch. "Replay Tutorial" is in Settings and the tray Tools submenu; voice commands "show tutorial" / "run tutorial" also work.

**Gaps:**
- The tutorial launches immediately after the wizard. The user has never used the app organically. For some users the correct moment for a hands-on tutorial is *after* they've fumbled around for a few minutes and already want to know how things work. Auto-launch is correct, but the "skip tutorial" path needs to be obvious and consequence-free.
- Ava step only appears if Ava is already configured. For new users who haven't set up Ollama, this step is silently absent. They never learn Ava exists during onboarding. The Done screen's Ava guide card partially covers this, but it's passive.
- The Command step (say "scroll down") is narrow. One successful command doesn't convey the breadth of the command library. The follow-on copy ("there are 400+ commands") is doing a lot of lifting.

---

## 5. Post-Setup: Landing State

After the tutorial closes (or is skipped), the user is in the app proper for the first time.

### Tray icon

The primary persistent UI. Sits in the Windows system tray (potentially in the overflow area, hidden by default on Windows 11 until pinned). Animates/spins in wake-word mode and during recording. The animation is the main "I'm alive and listening" signal.

> **Critical accessibility note:** For users with limited mobility who need the app most urgently, a tray icon buried in the overflow area is a weak anchor. They may not realise the app is running. There is no persistent window they can keep open as a reference point.

### History window

Opens on launch. Shows a table of dictation events (Time, Type, Mode, Text). Four action buttons: Copy, Copy All, Delete, Clear All.

This is the only persistent visible UI other than the tray, and it is entirely retrospective. On first launch the table is empty. A new user opens the app and sees a blank history log and four buttons that don't apply to anything yet. There is no "here's what to do first" prompt, no feature hint, no call to action.

### Listening indicator

Off by default. The user has no visual feedback that the app is monitoring for their hotkey unless they enable this manually in settings.

### Net landing state for a new user

Tray icon (possibly hidden) + blank history window. The app looks inactive and gives no indication of what to do next.

---

## 6. Hints System

Five hints exist, displayed as bottom-right toasts (dark background, teal border, 8-second auto-dismiss, non-blocking).

| Hint | Trigger | Message |
|---|---|---|
| `first_dictation_undo` | After first successful dictation | Say 'undo' to remove last paste |
| `wake_word_suggestion` | After 3 hold-mode sessions | Try wake word mode |
| `show_numbers_intro` | After 10 commands, if show numbers never used | Say 'show numbers' to click by voice |
| `wake_mode_activated` | When wake word mode starts | "Jarvis" activates voice commands |
| `streaming_mode` | First streaming session | Text appears live as you speak |

**What the hints do well:**
- Non-blocking and dismissible. Correct choice.
- "Don't show hints" checkbox in the toast itself — no hunting through settings.
- Resettable in settings so users who dismissed early can get them back.

**Structural gaps in hint coverage:**
- `show_numbers_intro` requires 10 commands to fire. Most new users won't reach 10 commands in the first session, and certainly won't if they're in hold-mode-only usage. The most visually impressive feature in the app — the one most likely to create a "wow" moment — is gated behind a threshold the majority of new users never reach.
- No hint for Ava. If a user configures Ollama after initial setup, nothing tells them Ava is ready to use.
- No hint for command mode (the dedicated command-only hotkey). A distinct and useful mode that most users don't discover.
- No hint fires from the history window, which is where the user is sitting when they have the most questions.

---

## 7. Settings — Access & Navigation

**Current paths to settings:**
1. Tray icon → right-click → Settings
2. History window → (needs verification — may not have a settings link in current build)
3. Voice command: "open settings" *(added)*

**Previously missing:** no voice command to open settings. For a voice-control app whose target audience includes users with limited hand mobility, this was an obvious gap. Now resolved.

### Settings window: 9 tabs

For initial UX audit, the relevant tabs are:

#### General tab

Contains: microphone selection, model size, and a cluster of guide/tutorial entry points:
- Run Mic Setup Guide
- Replay Tutorial
- Ava Setup Guide
- Voice Training

These are now grouped together. This is the right location — first tab a new user opens. However, they currently sit below the AI Model section with no visual section header to distinguish them from configuration controls. A new user scanning the tab sees "Replay Tutorial" between model settings and more toggles, and may not register it as a learning entry point.

> **Recommendation:** Add a "Getting Started" section header above the tutorial/guide buttons.

#### Hotkeys tab

Shows the 4 configurable hotkeys with capture buttons. Critical for new users whose chosen shortcuts conflict with other apps. Clean and functional.

#### Ava / Cloud tab

Relevant if the user saw the Ava step in the tutorial and got nothing (because Ollama wasn't set up). The discovery chain is: tutorial Done screen → Ava Setup Guide card → guide walks through Ollama install. The chain exists but is 3 steps long.

#### Commands tab

Outside the scope of this audit, but worth noting: new users who want to know what commands exist have no in-app discovery path from the General tab. The Command Reference (tray menu or voice "open command reference") is the right tool but is not surfaced anywhere in settings.

---

## 8. Retention Risk Assessment

### The hold-mode trap

The core problem: **hold-mode dictation is self-contained, immediately functional, and satisfying enough that it creates a closed loop.** Users who want to type with their voice get that in the first minute and never need to go further. The entire rest of the feature set — commands, show numbers, Ava, wake word, streaming, command mode — requires an active decision to learn something new.

**How the trap forms:**
1. User installs. Runs wizard. Does tutorial (maybe). Holds Ctrl+Shift, speaks, releases, text appears.
2. That works. They close the tutorial and go back to their day.
3. Every hint that could pull them deeper requires them to stay in the app long enough to hit a threshold. If they're using the app in short bursts for dictation only, those thresholds take days or weeks.
4. Nothing in the landing state says "here are three other things you can do right now."

### The features that create retention if discovered

- **Show numbers** — most likely to create an "I didn't know software could do this" moment. Closest thing to a product-defining demo for new users.
- **Commands** — once a user has 5 memorised they become dependent on them. Utility compounds.
- **Wake word mode** — transforms the app from "a better microphone shortcut" to "ambient voice control." Qualitatively different product experience.

None of these have a reliable passive discovery path from the current landing state.

### Accessibility angle

The chronic pain / limited mobility user is the audience for whom the full feature set matters most. Hold-mode requires sustained key pressure, which is exactly what these users are trying to avoid. Wake word mode and show numbers are the features that make the app genuinely hands-free — but they're also the features least likely to be discovered without guidance.

---

## 9. Summary Table

| Stage | Works | Gap |
|---|---|---|
| Website | Philosophy framing, docs exist | Screenshot count low; no "who is this for" up front |
| Installation | Near-zero friction | Splash/wizard polish sets first impression |
| Wizard | Config complete, mic tested | No demonstration; personalisation invisible; tray never explained |
| Tutorial | Hands-on, skippable, guides on Done screen | Ava hidden if not configured; command breadth understated |
| Landing state | Tray animates; history window opens | History empty and passive; listening indicator off; tray may be buried |
| Hints | Non-blocking, dismissible, resettable | `show_numbers` threshold too high; no Ava hint; no command-mode hint |
| Settings access | Tray right-click + "open settings" voice command | History window settings path needs verification |
| Settings (General) | All guides now present | No "Getting Started" section header |

---

## 10. One-Sentence Summary for ARC

The app acquires users effectively and gets them to first success (hold-mode dictation) quickly, but the onboarding chain has no reliable mechanism to pull a satisfied hold-mode user toward the features — commands, show numbers, wake word — that would make them dependent on the app, and those features are what justify its existence.
