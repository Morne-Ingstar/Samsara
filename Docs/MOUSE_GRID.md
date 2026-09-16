# Mouse grid — the universal last resort (queue 71)

Say **"grid"**. Nine numbered cells cover the screen. Say a number and that cell
becomes the new grid. Two or three numbers put the pointer on anything.

```
grid                      the monitor the focused window is on
grid window  /  grid here  just the focused window
grid monitor two          a named monitor, numbered left to right
five                      refine (any bare number, while the grid is up)
grid five three eight     three refinements in one breath
grid 5 3 8 click          ... and click at the end
click / right click / double click     act at the current cell
move here                 park the pointer there, click nothing
grid back                 undo one number
hide grid                 close it
```

## Why it exists when `show numbers` and `click <text>` already do

| Method | Needs | Reaches |
|---|---|---|
| `show numbers` (queue 72) | UI Automation elements | apps that expose a control tree |
| `click <text>` (queue 70) | readable text on screen | anything with words, via on-device OCR |
| **mouse grid** | **nothing at all** | **everything else** |

Icon-only buttons have no text and often no UIA element. Canvases, games, video
players and custom-drawn UIs expose neither. Queue 72 measured Obsidian at 6
role-bearing controls out of 892 nodes and Warp at 7 nodes (the window frame);
queue 70 measured OCR misreading 10–22% of small words. The grid does not care:
it divides pixels.

## Why not just use Windows Voice Access's `show grid`

Voice Access is always listening on the same microphone. Two always-listening
voice systems both act on the same speech, so every Samsara phrase would also be
heard by Voice Access and vice versa — a user who needs a grid would be running
two recognisers that fight over one utterance. A grid has to live inside the
tool you are already talking to.

## The subdivision: 3×3, reading order

```
1 2 3
4 5 6
7 8 9
```

* One spoken digit per step, and those nine words already exist in the app's
  vocabulary.
* Each step divides width and height by three. On a 2560×1440 monitor: 853×480
  after one, 285×160 after two, **95×53 after three** — smaller than any
  toolbar icon. A fourth step reaches 32×18.
* Dragon's MouseGrid is 3×3 in reading order for the same reasons, so muscle
  memory transfers.
* Refinement stops subdividing below 6 px; the centre is already inside.

## Chaining

`grid five three eight click` is one utterance, not four round trips — the same
path as saying the three numbers separately (a test asserts the two produce
identical geometry). Chaining works wherever a command takes a remainder:
command mode and the command hotkey.

**Limit, hands-free:** in the hands-free dictation lane only whole-utterance
commands and a curated list of prefixes (`click `, `focus `, `switch to `, …)
are candidates, so the one-breath chain needs `"grid "` added to
`_HANDS_FREE_COMMIT_PREFIXES` in `dictation.py`. That file is dispatch policy
and outside queue 71's fence. Until then, hands-free use is `"grid"`, then bare
numbers, then `"click"` — each of which is already a whole utterance.

## The pointer moves at every step

Every refinement moves the pointer to the centre of the new cell, like Dragon.
Two consequences:

* **Move without clicking is free** — refine and stop, or say `move here`. This
  is how the grid composes with a spoken verb afterwards ("the pointer names the
  target, the voice supplies the verb").
* The app's existing global `left click` / `right click` / `double click` act at
  that point, so the grid does not register competing click phrases.

## The numbers cannot leak into dictation

Bare `one`…`nine` are live registry phrases and a documented source of false
execution ("To, um…" → `window_cube.two`). The grid does **not** register them.

* Its own phrases (`hide grid`, `grid back`, `move here`) are scoped with queue
  68's mechanism to the tag **`mouse_grid.visible`**, published by
  `show_numbers.grid_active()`. While the grid is hidden they are not
  candidates at all.
* Refinement numbers reuse the path the numbered overlay already uses: a sole
  spoken number becomes `click N`, and `click` routes it to the grid while the
  grid is on screen. Nothing new is registered, so the window cube keeps its own
  tag-scoped numbers.

## Coordinates

Cells are computed in **physical pixels** — the coordinate system `SetCursorPos`
and UI Automation use — and converted to Qt logical DIPs only for drawing, with
queue 56's `phys_to_logical`. The pointer is placed inside an explicit
per-monitor-V2 thread context, so it is correct even though the process itself is
only system-DPI-aware (queue 70: `SetProcessDpiAwarenessContext() failed: Access
is denied.`, because `pyautogui` is imported before Qt's request).
