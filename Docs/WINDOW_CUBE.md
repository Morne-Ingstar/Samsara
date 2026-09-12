# Window Cube

A pinned, numbered list of your open windows that stays on screen. Keep your apps
full-screen and say **one number** to switch.

## The three phrases that matter

| Say this | What happens |
|---|---|
| **"show cube"** | Pins the numbered list in the bottom-right corner. It stays until you hide it. |
| **"three"** | Switches to window 3. |
| **"hide cube"** | Puts it away. |

That is the whole feature. Everything below is detail you can ignore until you need it.

## Saying a bare number

`"one"` … `"nine"` (and `"1"` … `"9"`) only switch windows when **both** are true:

- the cube is pinned, **and**
- you are in COMMAND mode or using hold-to-command.

In DICTATE or Ava mode a bare number is left alone, so dictating *"buy three apples"*
does not switch windows.

If you want a number to work **in any mode**, say **"window three"** instead. That form
is unambiguous, so it is always active while the cube is pinned.

> **Known limitation.** Samsara's command registry matches purely on phrase — it has no
> way to make a command conditionally *matchable*. While the cube is pinned, the bare
> number phrases are therefore still *recognised* in DICTATE mode even though they refuse
> to act, which means the number may be swallowed instead of typed. Until that is fixed
> in the dispatcher, prefer `"window three"` if you dictate numbers often — or turn the
> whole set off (see Turning the bare numbers off).

## Numbers never move

While the cube is pinned, a number always means the same window:

- Open a new window → it is **appended** with the next free number.
- Close a window → its row goes **grey** and keeps its number, so nothing below it shifts.
- **"refresh cube"** picks up new and closed windows *without* renumbering anything.

Numbers are only reassigned when you hide the cube and show it again. Your muscle memory
is the point of the feature, so a renumber never happens to you silently.

## More than nine windows

The cube shows 9 rows by default. Say **"cube page two"** for the next block.

## Other phrases

| Say this | What happens |
|---|---|
| **"refresh cube"** | Re-scan for new/closed windows, keeping all existing numbers. |
| **"cube page two"** | Show the next block of windows. |
| **"cube copy 2 into 4"** | Copy the selection from window 2 into window 4. |
| **"cube tile 2 and 4"** | Tile windows 2 and 4 side by side. |
| **click a row** | Same as saying its number. |

The cube also plays the usual success earcon when it switches.

## Settings

Config keys live under `window_cube` in your config:

| Key | Default | Meaning |
|---|---|---|
| `window_cube.max_rows` | `9` | Rows per page. |
| `window_cube.opacity` | `0.6` | Panel opacity, 0–1. |
| `window_cube.position` | *(remembered)* | `[x, y]` of the panel. Set automatically when you hide the cube, so it reopens where you left it. Delete the key to go back to the bottom-right of the active monitor. |

### Turning the bare numbers off

The bare `"one"`…`"nine"` commands live in their own command pack,
**`window-cube-numbers`**. Disable that pack to keep `"window three"` and everything else
while removing the bare numbers entirely.

## How it relates to "show windows"

The older **"show windows"** labels each window with a letter drawn *on top of* the
window, and dismisses itself after 30 seconds — so the labels disappear when an app is
full-screen. The cube is one small panel instead, it stays pinned, and it uses numbers.
Both features share the same window enumeration and focus code, so they always agree
about what a window is.
