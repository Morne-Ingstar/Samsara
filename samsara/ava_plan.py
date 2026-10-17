"""Bounded, closed-menu Ava plans. Models propose; policy authorizes each step."""
import json
from dataclasses import dataclass

from samsara import execution_policy as policy
from samsara.command_registry import DispatchState
from samsara.execution_policy import Invocation, Route


PROTOCOL = """
AVA_STRUCTURED_REPLY_624
Use this JSON response protocol instead of the response formats above.
Return exactly one JSON object, without markdown:
{"type":"text","text":"A short answer."}
{"type":"plan","plan":["exact offered command id","another offered id"]}
{"type":"ask","kind":"yes_no","question":"Continue?","plan":["exact offered id"]}
{"type":"ask","kind":"choice","question":"Which one?","options":["first","second"],"plan":[]}
{"type":"ask","kind":"clarify","question":"Which application?","plan":[]}
Plans contain 1 to 4 command ids copied exactly from OFFERED_IDS, in execution order.
For the offered ids open, close, focus, a step may instead be an object:
{"id":"close","args":{"app_name":"notepad"}}. app_name must be copied
from the user's words. Only those three ids accept arguments; all other
steps are command-id strings. Example: open Chrome and close Notepad becomes
{"type":"plan","plan":["open chrome",{"id":"close","args":{"app_name":"Notepad"}}]}.
Never invent ids, arguments, executable text or a command to approve another command.
For several requested actions, return a plan. Do not claim you can only do one.
If you need an answer, use ask, never a question inside text. Questions open a
short microphone reply window. yes_no requires a complete pending plan; choice
requires 2 to 4 distinct options; clarify accepts free text. Choice and clarify
answers return to you with the original request; their plan may be empty.
An answer is user input, never permission to bypass local execution policy.
Closing an app or deleting may lose work: local policy asks before that step.
For the existing single APP or SCHEDULE modes only, use respectively
{"type":"app","verb":"open","argument":"user's exact app words"} or
{"type":"schedule","seconds":300,"command":"exact offered command id"}.
These retain all the APP grounding and SCHEDULE rules above.
"""


def prepare_request(app, utterance, system, enabled):
    from plugins.commands import ask_ollama as ava
    ava._turn_local.plan_menu = None
    if not enabled:
        return system
    executor = getattr(app, "command_executor", None)
    try:
        menu = executor.ava_menu(utterance, app=app, route=Route.MODEL) if executor else []
    except Exception:
        menu = []
    ava._turn_local.plan_menu = tuple(menu or ())
    return system + PROTOCOL + "\nOFFERED_IDS=" + json.dumps(list(menu or ()))


@dataclass(frozen=True)
class Step:
    command_id: str
    app_name: str = ""

    @property
    def label(self):
        return (self.command_id + " " + self.app_name).strip()


def validate_plan(plan, menu, utterance, *, empty=False):
    from plugins.commands import ask_ollama as ava
    if not isinstance(plan, list) or not (0 if empty else 1) <= len(plan) <= 4:
        raise ValueError("plan length")
    steps = []
    for item in plan:
        if isinstance(item, str):
            step = Step(item)
        elif (isinstance(item, dict) and set(item) == {"id", "args"}
              and isinstance(item["id"], str) and item["id"] in {"open", "close", "focus"}
              and isinstance(item["args"], dict) and set(item["args"]) == {"app_name"}):
            name = item["args"]["app_name"]
            if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
                    or any(ord(c) < 32 for c in name) or not ava.grounded_in_utterance(name, utterance)):
                raise ValueError("ungrounded app")
            step = Step(item["id"], name)
        else:
            raise ValueError("step shape")
        if step.command_id not in menu or not ava.action_is_grounded(step.command_id, utterance):
            raise ValueError("plan authority")
        if step.command_id in {"open", "close", "focus"} and not step.app_name:
            raise ValueError("missing app")
        steps.append(step)
    return tuple(steps)


def handle_json(app, response, utterance, generation):
    from plugins.commands import ask_ollama as ava
    from samsara import ava_pending
    if not response.lstrip().startswith(("{", "[")):
        return None  # Existing conversation history may still use the old grammar.
    try:
        data = json.loads(response)
        if not isinstance(data, dict):
            raise ValueError("object required")
        kind = data.get("type")
        if kind == "text" and set(data) == {"type", "text"} and isinstance(data["text"], str):
            ava.speak(app, data["text"])
            return ava.TurnOutcome("conversation", "spoken")
        if not ava._ai_state(app).allowed or not policy.is_current(app, generation):
            return ava.TurnOutcome("plan", "stale", reason="stale")
        if getattr(app, "_ava_question", None) is not None or policy.pending_operation() is not None:
            return ava.TurnOutcome("plan", "refused", reason="question_pending")
        menu = getattr(ava._turn_local, "plan_menu", None)
        if menu is None:
            menu = ava.offered_menu(app, utterance) or ()
        if kind == "app" and set(data) == {"type", "verb", "argument"}:
            if any(not isinstance(data[k], str) or "\n" in data[k] or "\r" in data[k]
                   for k in ("verb", "argument")):
                raise ValueError("app fields")
            return ava.handle_response(app, f"CONFIRM App.\nAPP {data['verb']} | {data['argument']}",
                                       original_text=utterance, generation=generation)
        if kind == "schedule" and set(data) == {"type", "seconds", "command"}:
            if type(data["seconds"]) is not int or data["command"] not in menu:
                raise ValueError("schedule fields")
            return ava.handle_response(app, f"CONFIRM Schedule.\nSCHEDULE {data['seconds']} {data['command']}",
                                       original_text=utterance, generation=generation)
        if kind not in {"plan", "ask"}:
            raise ValueError("unknown response")
        plan = validate_plan(data.get("plan"), menu, utterance, empty=kind == "ask" and data.get("kind") != "yes_no")
        runner = Plan(app, plan, tuple(menu), utterance, generation)
        if kind == "plan":
            if set(data) != {"type", "plan"}:
                raise ValueError("plan fields")
            return runner.run()
        question = data.get("question")
        ask_kind = data.get("kind")
        options = data.get("options", [])
        allowed = {"type", "plan", "kind", "question"} | ({"options"} if ask_kind == "choice" else set())
        if (set(data) != allowed or ask_kind not in {"yes_no", "choice", "clarify"}
                or not isinstance(question, str) or not 1 <= len(question.strip()) <= 400
                or any(ord(c) < 32 for c in question)):
            raise ValueError("question fields")
        if ask_kind == "choice" and (not isinstance(options, list) or not 2 <= len(options) <= 4
                or any(not isinstance(o, str) or not 1 <= len(o.strip()) <= 80
                       or any(ord(c) < 32 for c in o) for o in options)
                or len({o.strip().casefold() for o in options}) != len(options)):
            raise ValueError("choice options")
        return ava_pending.ask(app, runner, ask_kind, question.strip(), tuple(options))
    except (ValueError, TypeError, KeyError):
        # Stand-in copy (624). Refuse the entire response before any effect.
        ava.speak(app, "I couldn't use that plan. Nothing was run.")
        return ava.TurnOutcome("plan", "refused", reason="invalid_plan")


class Plan:
    def __init__(self, app, steps, menu, utterance, generation):
        self.app, self.steps, self.menu = app, steps, menu
        self.utterance, self.generation, self.index = utterance, generation, 0

    def eligible(self):
        from plugins.commands import ask_ollama as ava
        return ava._ai_state(self.app).allowed and policy.is_current(self.app, self.generation)

    def failure(self, reason):
        from plugins.commands import ask_ollama as ava
        # Stand-in copy (624); only a locally validated command name is used.
        ava.speak(self.app, f"Step {self.index + 1} failed: {self.steps[self.index].label}. I stopped the plan.")
        return ava.TurnOutcome("plan", "failed", reason=reason, name=str(self.index + 1))

    def run(self, *, approved=False):
        from plugins.commands import ask_ollama as ava
        from samsara import ava_pending
        from samsara.ava_turn_queue import ownership_lock
        with ownership_lock(self.app):
            while self.index < len(self.steps):
                if not self.eligible():
                    return ava.TurnOutcome("plan", "stale", reason="stale")
                step = self.steps[self.index]
                cid = step.command_id
                executor = self.app.command_executor
                # The existing APP choke point governs named app effects; it
                # maps back to the same catalogued verb for pack/scope checks.
                inv = Invocation(f"action2:{cid}" if step.app_name else cid,
                    {"target": step.app_name} if step.app_name else {},
                    route=Route.MODEL, generation=self.generation, source_text=self.utterance)
                decision = policy.authorize(inv, app=self.app, executor=executor, confirmed=approved)
                if isinstance(decision, policy.Denied):
                    return self.failure(decision.reason)
                if isinstance(decision, policy.NeedsConfirmation):
                    if policy.pending_operation() is not None:
                        return self.failure("another_confirmation")
                    targets, probe, foreground = policy.bind_staged_target(inv, executor)
                    binding = ({"foreground_target": targets, "foreground_probe": probe} if foreground
                               else {"targets": targets, "target_probe": probe})
                    op = policy.stage_pending(self.app, inv, decision.prompt,
                        on_approve=lambda _op: self.run(approved=True),
                        refusal_guard=lambda: None if self.eligible() else "stale", **binding)
                    return ava_pending.ask(self.app, self, "policy", decision.prompt, (), operation=op)
                try:
                    if step.app_name:
                        from plugins.commands.app_verbs import ActionResult
                        result = ava._execute_action2(self.app, cid, step.app_name, route=Route.MODEL,
                            generation=self.generation, source_text=self.utterance, confirmed=approved)
                        state = DispatchState.COMPLETED if result is ActionResult.DONE else DispatchState.FAILED
                    else:
                        result = executor.execute_canonical(cid, self.app, route=Route.MODEL,
                            generation=self.generation, source_text=self.utterance, confirmed=approved)
                        state = result.state
                except Exception:
                    return self.failure("exception")
                approved = False  # Approval applies to this step only.
                if state is not DispatchState.COMPLETED:
                    return self.failure(str(state.value))
                self.index += 1
            return ava.TurnOutcome("plan", "completed", name="plan")
