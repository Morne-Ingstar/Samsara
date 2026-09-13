# Execution policy (queue 02c)

`samsara/execution_policy.py` is the ONE choke point between "something
decided to act" and an actual side effect. It answers Astra review
2026-09-12 section 1 items 1 and 3 (a model could execute before any
confirmation; no owner for cancellation) and section 5 item 3 (a stop path
that does not wait on understanding).

## The decision

```python
authorize(Invocation(command_id, args, route, generation, prompt)) ->
    Allowed(reason, risk)
  | NeedsConfirmation(prompt, risk)
  | Denied(reason, risk, detail)      # stale | unknown_command | invalid_args
                                      # | not_allowed_for_model | legacy_protocol
```

Inputs, in the order they are checked:

1. **generation** -- the request identity (below). Not current -> `Denied(stale)`.
2. **command id** -- must resolve in a registry: the built-in table
   (commands.json), the plugin registry, an ACTION2 verb (`action2:close`)
   or a Smart Actions tool (`smart_action:send_email`). Otherwise
   `Denied(unknown_command)`.
3. **args** -- validated against the plugin's `param_schema`
   (type / min / max / choices / required). Otherwise `Denied(invalid_args)`.
4. **route** -- on a model route the id must be on the model allow-list
   (below). Otherwise `Denied(not_allowed_for_model)`.
5. **confirmed** -- the user already answered a prompt for this exact
   invocation: `Allowed(confirmed)` (steps 1-4 still applied).
6. **risk x route** -- the table.

| risk                  | exact / grammar / macro / schedule | model / smart-action |
|-----------------------|------------------------------------|----------------------|
| read, ui              | Allowed                            | Allowed              |
| write                 | Allowed                            | NeedsConfirmation    |
| destructive, unknown  | NeedsConfirmation                  | NeedsConfirmation    |

Every decision is logged (`Samsara.execution_policy`) and shown as an
outcome chip: `Confirm: <id>` (pending), `Blocked: <id>` (error),
`Cancelled` (stale).

### Where risk comes from (never the caller)

* **Built-ins**: type + keys. `alt+f4`, `ctrl+w`, `ctrl+f4`, `ctrl+shift+w`,
  `ctrl+q`, `alt+q`, `win+l`, `ctrl+alt+delete`, `shift+delete` are
  destructive; `enter`, `delete`, `backspace`, `space`, `insert`, single
  characters, `ctrl+{x v z y s d k n o p}` and `text`/`mouse` commands are
  write; navigation (`alt+tab`, arrows, `ctrl+c`, `home`...) is ui.
  `launch` is ui. `method` commands use an explicit table
  (`repeat_last_command` is unknown). A `macro` is as risky as its worst
  step; a step the classifier cannot read is unknown.
* **ACTION2 verbs**: focus/open ui, close destructive.
* **Smart Actions tools**: tier AUTO ui, SETUP write, ALWAYS_CONFIRM destructive.
* **Plugins**: the registry's `risk_class` (`safe` -> ui, `reversible` ->
  write, `destructive`). The registry (02a) keeps two views: the flat
  `risk_class` (declared, else the historical default `safe`) and
  `metadata['risk_class']`, which is `unknown` when nothing was declared.
  User routes use the flat value (an undeclared plugin keeps working for the
  phrase the user spoke); model routes use the declared value only, so an
  undeclared plugin is unknown to a model -- not on the allow-list.

This replaced the `_UNSAFE_COMMANDS` name denylist in `ask_ollama.py`, which
missed the real `enter` / `delete selection` commands (Astra MODEL_BYPASS).

### Model allow-list

A model (Ava ACTION / ACTION2, Smart Actions) may only name tool ids that
the registry marks read/ui **plus** an explicit list
(`DEFAULT_MODEL_EXTRA_ALLOWLIST`: close window/tab, enter, submit, new line,
delete selection/word/line, cut, paste, undo, redo, select all,
`action2:close`, the SETUP/ALWAYS Smart Actions tools). Those extra ids
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
`action2`, `invocation`, all carrying `op`). Voice "yes"
(`handle_ava_confirm`) calls `op.approve()`, which re-enters the same
executor with `confirmed=True` -- still generation-checked. "ava cancel",
"scratch that", the stop path and a newer staging reject it. The Smart
Actions Approve / Reject / Always dialog is a consumer of the same object:
its buttons call `op.approve()` / `op.reject()`, the dispatcher waits on
`op.wait(120)`, and a spoken "yes" closes the dialog.

## Request identity and cancellation

The generation is `app._ava_cmd_generation` -- the counter the D3 waterfall
already guarded on. There is no second convention.

* Captured when a request is made: `handle_ask_ava(generation=...)`, the AVA
  session queue (`(generation, text)` items, never bare strings), D3
  waterfall items, staged confirmations, scheduler tasks.
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
(staged dictation) are untouched.

## Behaviour changes worth knowing

* Exact `close window` / `close tab` (and any other destructive built-in)
  now asks for confirmation on the exact route too; `switch window`,
  `enter`, `copy`, ... run as before.
* A model can no longer run a write command (`enter`, `paste`, `say ...`)
  without a spoken "yes", and cannot name a plugin whose author declared no
  `risk_class` (174 of 189 plugin commands today) -- add ids to
  `execution_policy.model_tool_extra` or declare `risk_class` on the plugin.
* `EXECUTE ...` model output is refused, never executed.

Tests: `tests/test_execution_policy.py`.
