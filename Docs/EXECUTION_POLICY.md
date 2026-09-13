# Execution policy (queue 02c)

`samsara/execution_policy.py` is the ONE choke point between "something
decided to act" and an actual side effect. It answers Astra review
2026-09-12 section 1 items 1 and 3 (a model could execute before any
confirmation; no owner for cancellation) and section 5 item 3 (a stop path
that does not wait on understanding).

## The decision

```python
authorize(Invocation(command_id, args, route, generation)) ->
    Allowed(reason, risk, hint)
  | NeedsConfirmation(prompt, risk)   # prompt = confirmation_prompt(): local template
  | Denied(reason, risk, detail)      # stale | unknown_command | invalid_args | unvalidated
                                      # | not_allowed_for_model | legacy_protocol
```

Inputs, in the order they are checked:

1. **generation** -- the request identity (below). Missing (`None`), not an
   integer, or not current -> `Denied(stale)`. There is no "unbound" request.
2. **command id** -- must resolve in a registry: the built-in table
   (commands.json), the plugin registry, an ACTION2 verb (`action2:close`)
   or a Smart Actions tool (`smart_action:send_email`). Otherwise
   `Denied(unknown_command)`.
3. **args** -- see "Argument schemas" below. `Denied(unvalidated)` or
   `Denied(invalid_args)`.
4. **route** -- on a model route the id must be on the model allow-list
   (below). Otherwise `Denied(not_allowed_for_model)`.
5. **confirmed** -- the user already answered a prompt for this exact
   invocation: `Allowed(confirmed)` (steps 1-4 still applied).
6. **risk x route** -- the table.

| risk | exact | grammar / macro / schedule | model / smart-action |
|------|-------|----------------------------|----------------------|
| read, ui | Allowed | Allowed | Allowed |
| write | Allowed | Allowed | NeedsConfirmation |
| destructive, reversible | Allowed (`hint="undoable"`) | NeedsConfirmation | NeedsConfirmation |
| destructive, irreversible | NeedsConfirmation | NeedsConfirmation | NeedsConfirmation |
| unknown | NeedsConfirmation* | NeedsConfirmation | NeedsConfirmation |

*Only an exact verb in `_SAFE_UNKNOWN_VERBS` (lock computer / lock screen)
may use its explicit read/ui classification with an unknown-metadata hint.
An explicit model allow-list still denies excluded commands on every model route.

### Owner decision (Morne, 2026-09-12)

| spoken command | classification | exact | model | reason |
|----------------|----------------|-------|-------|--------|
| close tab | destructive, reversible | Allowed, undoable hint | NeedsConfirmation | A closed tab can be reopened. |
| close window | destructive, reversible | Allowed, undoable hint | NeedsConfirmation | Owner classifies closing as reopenable. |
| lock computer | ui | Allowed | Allowed | Locking changes access state without deleting work. |
| lock screen | ui | Allowed | Allowed | Same Win+L effect as lock computer. |
| permanent delete | destructive, irreversible | NeedsConfirmation | NeedsConfirmation | Shift+Delete bypasses recoverable deletion. |
| going dark | declared destructive; reversibility undeclared | NeedsConfirmation | Denied(unvalidated) (no argument schema; was NeedsConfirmation) | Honor macro metadata; unknown reversibility is irreversible. |
| again | risk of stored target | Target decision; Denied if empty | Target decision; Denied if empty | Repeat is an indirection, not an independent effect. |
| repeat | risk of stored target | Target decision; Denied if empty | Target decision; Denied if empty | Same repeat_last_command method as again. |

Additional classification rules:

| entry / declaration | classification | reason |
|---------------------|----------------|--------|
| Alt+F4, Ctrl+W, Ctrl+F4, Ctrl+Shift+W | destructive, reversible | Classify the closing effect consistently across aliases. |
| Win+L | ui | Locking is not destruction. |
| `reversibility` / `reversible` true or reversible/undoable | reversible | Honor an explicit registry declaration. |
| false, unknown, or missing reversibility | irreversible by default | Never interpret the string unknown as truthy reversibility. |
| unknown plugin risk | unknown on every route | A historical flat safe default is not an author declaration. |
| unknown risk on model routes | NeedsConfirmation for built-ins, unless explicitly excluded; a plugin without an argument schema is Denied(unvalidated) first | Unknown metadata never authorizes an unconfirmed effect. |

Repeat resolves the registered method `repeat_last_command` through
`app._last_command_name` and checks `app._last_command`, then authorizes the
stored target id with the original route and generation. Empty or recursive
repeat history returns `Denied("nothing to repeat")`; a changed built-in
snapshot is denied. The target's args, model allow-list and confirmation
rules apply. A ui target runs; an irreversible destructive target prompts
with the target name. Approval checks that repeat history has not changed,
then dispatches the bound target with the original route and confirmation,
preventing a second prompt or approval of a different last command.

Every decision is logged (`Samsara.execution_policy`) and shown as an
outcome chip: `Confirm: <id>` (pending), `Blocked: <id>` (error),
`Cancelled` (stale).

### Argument schemas (2026-09-13)

An absent or empty `param_schema` is **undeclared** -- never "takes no
arguments".

| schema | model / smart-action route | exact / grammar / macro / schedule route |
|--------|----------------------------|-------------------------------------------|
| undeclared (plugin without one; every Smart Actions tool until `SMART_ACTION_SCHEMAS` names it) | `Denied(unvalidated)` | Allowed to carry only the user's own spoken `remainder`; any other key -> `Denied(invalid_args)` |
| `NO_ARGS_SCHEMA` (commands.json built-ins, raw `key:` presses) | any argument -> `Denied(invalid_args)` | same |
| declared | missing required, extra keys, wrong type (no coercion: `"40"` is not an int, `True` is not an int), outside min/max/choices, string longer than `max_len` (default 500) -> `Denied(invalid_args)` | same; the spoken `remainder` is tolerated |

ACTION2 verbs have the local schema `{"target": str, required, max_len 120}`.
Owner decision (2026-09-13): the exact route keeps the spoken remainder for
undeclared plugins, so "focus obsidian" / "ask ava ..." keep working until
plugins declare schemas.

### Confirmation wording

`NeedsConfirmation.prompt` comes from `confirmation_prompt(command_id, args)`:
a local template (`_CONFIRM_TEMPLATES`, e.g. `Close {target}?`, or the
plugin's registry-declared `preview_template`) filled with resolved values
(one line, control characters removed, at most 60 characters), else
`"<Command>?"`. `Invocation.prompt` is ignored, `ask_ollama` no longer
forwards a model's `CONFIRM <text>`, and a model SCHEDULE is asked as
`Repeat <what> every <n> seconds?`.

### Where risk comes from (never the caller)

* **Built-ins**: classify type and keys. Closing hotkeys are destructive
  and reversible; Win+L is ui; Shift+Delete and quitting default irreversible.
  Text-editing keys and text/mouse commands are write; navigation and launch
  are ui. Methods use an explicit table, with repeat resolved dynamically.
  A macro is as risky as its worst step and defaults irreversible unless declared.
* **ACTION2 verbs**: focus/open ui, close destructive. Grammar stays strict.
* **Smart Actions tools**: tier AUTO ui, SETUP write, ALWAYS_CONFIRM destructive.
* **Plugins**: declared `risk_class` (safe/ui, read, reversible/write,
  destructive), plus `reversibility` or `reversible` metadata. Unknown or
  missing reversibility defaults irreversible. Unknown declared risk never
  falls back to the registry's historical flat safe default.

This replaced the `_UNSAFE_COMMANDS` name denylist in `ask_ollama.py`, which
missed the real `enter` / `delete selection` commands (Astra MODEL_BYPASS).

### Model allow-list

A model (Ava ACTION / ACTION2, Smart Actions) may only name tool ids that
the registry marks read/ui **plus** an explicit list
(`DEFAULT_MODEL_EXTRA_ALLOWLIST`: close window/tab, enter, submit, new line,
delete selection/word/line, cut, paste, undo, redo, select all,
`action2:close`, the SETUP/ALWAYS Smart Actions tools). Permanent delete and going dark are also explicitly listed so a model
proposal reaches confirmation. Registered unknown-risk ids also require
confirmation. Unknown ids are denied before this check. Those extra ids
still go through confirmation. Config:

```json
"execution_policy": {
  "model_tool_extra": ["scroll down"],          // add to the computed list
  "model_tool_allowlist": ["switch window"]     // or replace it entirely
}
```

## Effect paths routed through `authorize()`

| path | where |
|------|-------|
| exact / alias built-in | `CommandExecutor.execute_command` (also macros and scheduler ticks) |
| exact / alias plugin | `CommandExecutor.process_text`, plugin branch |
| model ACTION | `ask_ollama.handle_response` -> `execute_command(route=model)` |
| model / grammar ACTION2 | `ask_ollama._execute_action2` (called by `handle_response` and by the D3 waterfall's `_dispatch_action2`) |
| legacy `EXECUTE <text>` | removed: `Denied(legacy_protocol)`, spoken refusal, no effect |
| scheduled repeat | `ask_ollama._execute_safe` -> `execute_command(route=schedule, generation=...)` |
| Smart Actions tool call | `ToolDispatcher.dispatch` (route=smart_action) |

## One pending operation

`NeedsConfirmation` is staged with `stage_pending()` as a `PendingOperation`
in the existing `ask_ollama._pending_action` slot (record types `action`,
`action2`, `invocation`, all carrying `op`). The Smart Actions Approve /
Reject / Always dialog is a consumer of the same object: its buttons call
`op.approve()` / `op.reject()`, the dispatcher waits on `op.wait(120)`, and a
spoken "yes" closes the dialog.

**The record** (`PendingOperation`): `op_id`, `generation` (the request's),
`arg_hash` (sha256 of command + arguments), `targets` (bound target
versions, re-read through an optional `target_probe` at approval) and a
30 s **monotonic** `deadline` (`expires` mirrors it in wall-clock time for
legacy readers). Single-use: the first resolution wins.

* **Supersede**: staging a new proposal cancels the old one visibly
  (chip `cancelled: superseded`); a late yes cannot reach it.
* **yes**: accepted only as a complete utterance (`classify_reply`: "yes",
  "yeah", "go ahead", "do it", ... with trailing punctuation) while the
  record is live. `CommandExecutor.process_text` answers it
  (`answer_pending`) before any command matching. Refused, with the
  record cancelled and a `cancelled: <why>` chip: deadline passed
  (`expired`), generation bumped (`stale`), target changed. Refused without
  consuming the record: a model-sourced answer (`source="model"`). Not a
  reply at all (dictation / miss, record untouched): a quoted yes, or a yes
  inside a longer sentence ("yes but not now").
* **no**: rejects (chip `cancelled: declined`).
* **wait** / "hold on": extends the deadline once (chip `waiting`); a second
  wait is refused.
* Hands-free lanes: `SessionModeManager(pending_reply_fn=...)` gives the
  pending question priority over every lane (outcome `pending_reply`).

## Request identity and cancellation

The generation is `app._ava_cmd_generation` -- the counter the D3 waterfall
already guarded on. There is no second convention.

* Captured when a request is CREATED (`capture_generation(app)`), never when
  a model finishes: `handle_ask_ava(generation=...)`, the AVA session queue
  (`(generation, text)` items, never bare strings), D3 waterfall items,
  staged confirmations, scheduler tasks. `process_text(generation=None)` and
  an exact-route `execute_command(generation=None)` capture at the call --
  that call is where a spoken exact request is created. Every other route
  must pass the generation it captured; `None` is `Denied(stale)`.
* The plugin branch of `process_text` re-checks freshness immediately before
  calling the handler, and a confirmed plugin approval re-authorizes.
* Bumped by `execution_policy.stop_all()` -- called from `handle_ava_cancel`
  ("ava cancel"), `exit_command_mode`, `exit_ava_command_session`, the
  wake-lane "never mind"/sleep, and the spoken stop words.
* Checked at dequeue (`_on_ava_session_request_done`, `_worker_loop`), when
  a model response returns (`handle_ask_ava` worker), when a confirmation is
  answered, and always in `authorize()` itself.

### Stop path independent of understanding

`stop_all(app, reason)` bumps the generation FIRST, then clears the pending
slot, the scheduler, the AVA session queue and the waterfall queue
(`drain_stale`). It never waits on the inference queue or on a delivery
adapter. `DictationApp._try_stop_utterance` runs it for an exact
`stop` / `cancel` / `cancel that` / `stop it` / `stop that` / `ava stop` /
`ava cancel` / `go to sleep` spoken into any Ava lane, before anything is
queued. "nevermind" stays the pending-only cancel it already was. Drafts
(staged dictation) are untouched. The pending operation is cancelled (not
just forgotten), so a waiting dialog returns at once. Chip: `stopped`.

Hands-free session (`SessionModeManager(stop_fn=...)`): **"stop"** as a whole
utterance runs `stop_fn` (wire it to `stop_all`) -- generation first, model
requests cancelled, queued effects drained -- then returns outcome
`stopped` with the draft kept, the lane unchanged and the microphone still
armed. "stop the music" is ordinary text. **"go to sleep"** runs the same
`stop_fn` FIRST, then retains the draft and disarms (`on_abort`). Without a
`stop_fn` the manager does not intercept "stop" (it cannot stop anything
itself). **"scratch that"** still asks `pending_action_scratch_fn` first --
an unexecuted proposal is cancelled before the undo stack is touched.

`DispatchResult.succeeded` is `COMPLETED` only; `QUEUED` and `MATCHED` are
not success.

## Behaviour changes worth knowing

* Exact close window/tab executes with an undoable hint; lock computer/screen
  executes as ui. Permanent delete and undeclared-reversibility going dark prompt.
* Model write/destructive/unknown commands require confirmation. Undeclared
  plugin risk also prompts on exact, except the explicit read/ui verb table.
* `EXECUTE ...` model output is refused, never executed.

* A tool with no declared argument schema is unavailable to a model; every
  Smart Actions tool call is `Denied(unvalidated)` until its schema is
  reviewed into `execution_policy.SMART_ACTION_SCHEMAS`.
* A "yes" that is part of a sentence is dictation, not a confirmation.

Tests: `tests/test_execution_policy.py`, `tests/test_pending_confirmation.py`,
`tests/test_stop_generation.py`, `tests/test_dispatch_contract.py`.
