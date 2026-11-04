"""Bounded, closed-menu Ava plans. Models propose; policy authorizes each step."""
import json
from dataclasses import dataclass

from samsara import execution_policy as policy
from samsara.command_registry import DispatchState
from samsara.execution_policy import Invocation, Route
from samsara.ava_capabilities import CapabilityError


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
    binding: object = None

    @property
    def label(self):
        return (self.command_id + " " + self.app_name).strip()


def grounded_app_name(name, utterance):
    """A plan target must be a complete, contiguous span of the user's tokens."""
    from plugins.commands import ask_ollama as ava
    target = ava._WORD_RE.findall(name.lower())
    said = ava._WORD_RE.findall((utterance or "").lower())
    return bool(target) and any(said[i:i + len(target)] == target
                               for i in range(len(said) - len(target) + 1))


def validate_plan(plan, menu, utterance, *, empty=False, request=None):
    from plugins.commands import ask_ollama as ava
    if not isinstance(plan, list) or not (0 if empty else 1) <= len(plan) <= 4:
        raise ValueError("plan length")
    steps = []
    for item in plan:
        if request is not None and request.capabilities is not None:
            from samsara.command_invocation_adapters import bind
            caps = request.capabilities
            cid = item if isinstance(item, str) else item.get('id') if isinstance(item, dict) else None
            if cid == 'edit_text':
                raise ValueError('standalone edit required')
            offered_id = cid if caps and cid in caps.offered else next((
                key for key, value in (caps.offered.items() if caps else ())
                if value.record()['canonical_phrase'] == cid), None)
            if offered_id is not None:
                if isinstance(item, dict) and set(item) != {'id', 'args'}:
                    raise ValueError('step shape')
                binding = bind(caps, offered_id, item['args'] if isinstance(item, dict) else {}, utterance)
                inv = binding.invocation()
                if not ava.action_is_grounded(inv.command_id, utterance):
                    raise ValueError('ungrounded app')
                steps.append(Step(inv.command_id, binding=binding))
                continue
            # The established APP adapter has its own grounding/schema/policy
            # choke point. No other unoffered names may use this compatibility.
            if not (isinstance(item, dict) and cid in request.app_ids):
                descriptor = caps.index.by_id.get(cid) if caps else None
                raise CapabilityError((descriptor.record()['reason'] if descriptor else '') or 'unoffered')
        if isinstance(item, str):
            step = Step(item)
        elif (isinstance(item, dict) and set(item) == {"id", "args"}
              and isinstance(item["id"], str) and item["id"] in {"open", "close", "focus"}
              and isinstance(item["args"], dict) and set(item["args"]) == {"app_name"}):
            name = item["args"]["app_name"]
            if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
                    or any(ord(c) < 32 for c in name) or not grounded_app_name(name, utterance)):
                raise ValueError("ungrounded app")
            step = Step(item["id"], name)
        else:
            raise ValueError("step shape")
        if (((request is None or request.capabilities is None) and step.command_id not in menu)
                or not ava.action_is_grounded(step.command_id, utterance)):
            raise ValueError("plan authority")
        if step.command_id in {"open", "close", "focus"} and not step.app_name:
            raise ValueError("missing app")
        steps.append(step)
    return tuple(steps)


def handle_json(app, response, utterance, generation, *, request=None):
    from plugins.commands import ask_ollama as ava
    from samsara import ava_pending
    if not response.lstrip().startswith(("{", "[")):
        return None  # Existing conversation history may still use the old grammar.
    try:
        from samsara.ava_capability_protocol import parse
        data = parse(response)
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
        from samsara.ava_edit import session
        if session.pending() is not None:
            return ava.TurnOutcome("plan", "refused", reason="edit_pending")
        menu = getattr(ava._turn_local, "plan_menu", None)
        if request is not None:
            menu = request.menu
        elif menu is None:
            menu = ava.offered_menu(app, utterance) or ()
        if kind == "app" and set(data) == {"type", "verb", "argument"}:
            if any(not isinstance(data[k], str) or "\n" in data[k] or "\r" in data[k]
                   for k in ("verb", "argument")):
                raise ValueError("app fields")
            if (data['verb'] not in {'open', 'close', 'focus'}
                    or not 1 <= len(data['argument'].strip()) <= 120
                    or not grounded_app_name(data['argument'], utterance)
                    or (request is not None and request.capabilities is not None
                        and data['verb'] not in request.app_ids)):
                raise ValueError('ungrounded app')
            return ava.handle_response(app, f"CONFIRM App.\nAPP {data['verb']} | {data['argument']}",
                                       original_text=utterance, generation=generation)
        if kind == "schedule" and set(data) == {"type", "seconds", "command"}:
            if type(data["seconds"]) is not int or not isinstance(data['command'], str):
                raise ValueError("schedule fields")
            if request is not None and request.capabilities is not None:
                caps = request.capabilities
                invocation = (caps.bind(data['command'], source_text=utterance) if data['command'] in caps.offered
                              else caps.bind_legacy(data['command'], source_text=utterance))
                data['command'] = invocation.command_id
            elif data['command'] not in menu:
                raise ValueError('schedule authority')
            return ava.handle_response(app, f"CONFIRM Schedule.\nSCHEDULE {data['seconds']} {data['command']}",
                original_text=utterance, generation=generation,
                capabilities=request.capabilities if request is not None else None)
        if kind not in {"plan", "ask"}:
            raise ValueError("unknown response")
        plan = validate_plan(data.get("plan"), menu, utterance,
            empty=kind == "ask" and data.get("kind") != "yes_no", request=request)
        runner = Plan(app, plan, tuple(menu), utterance, generation, request=request)
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
    except CapabilityError as exc:
        from samsara.ava_capability_protocol import refusal
        return refusal(app, exc.reason)
    except (ValueError, TypeError, KeyError):
        # Stand-in copy (624). Refuse the entire response before any effect.
        ava.speak(app, "I couldn't use that plan. Nothing was run.")
        return ava.TurnOutcome("plan", "refused", reason="invalid_plan")


class Plan:
    def __init__(self, app, steps, menu, utterance, generation, *, request=None):
        self.app, self.steps, self.menu = app, steps, menu
        self.utterance, self.generation, self.index = utterance, generation, 0
        self.request = request

    def eligible(self):
        from plugins.commands import ask_ollama as ava
        if self.request is not None and self.request.capabilities is not None:
            try:
                self.request.capabilities._fresh()
            except (ValueError, AttributeError):
                return False
        return ava._ai_state(self.app).allowed and policy.is_current(self.app, self.generation)

    def failure(self, reason, *, index=None):
        from plugins.commands import ask_ollama as ava
        index = self.index if index is None else index
        completed = "; ".join(f"step {i + 1}: {s.label}" for i, s in enumerate(self.steps[:self.index]))
        progress = f"Already ran: {completed}." if completed else "Nothing was run."
        # Stand-in copy (640); names come only from the validated plan.
        ava.speak(self.app, f"Step {index + 1} failed: {self.steps[index].label}. "
                  f"Reason: {reason.replace('_', ' ')}. I stopped the plan. {progress}")
        return ava.TurnOutcome("plan", "failed", reason=reason, name=str(index + 1))

    def invocation(self, step):
        from samsara.effect_policy import bind_plan_input
        if step.binding is not None:
            return bind_plan_input(step.binding.invocation(), self.app, self.app.command_executor)
        # Named apps use the same APP choke point for preflight and execution.
        inv = Invocation(f"action2:{step.command_id}" if step.app_name else step.command_id,
            {"target": step.app_name} if step.app_name else {},
            route=Route.MODEL, generation=self.generation, source_text=self.utterance)
        return bind_plan_input(inv, self.app, self.app.command_executor)

    def run(self, *, approved=False):
        from plugins.commands import ask_ollama as ava
        from samsara import ava_pending
        from samsara.ava_turn_queue import ownership_lock
        with ownership_lock(self.app):
            if not self.eligible():
                return ava.TurnOutcome("plan", "stale", reason="stale")
            if self.index == 0:
                for i, step in enumerate(self.steps):
                    try:
                        decision = policy.authorize(self.invocation(step), app=self.app,
                            executor=self.app.command_executor, quiet=True)
                    except ValueError:
                        return self.failure('stale_contract', index=i)
                    if isinstance(decision, policy.Denied):
                        return self.failure(decision.reason, index=i)
                # NeedsConfirmation is only a preview; each step still asks locally.
            while self.index < len(self.steps):
                if not self.eligible():
                    return ava.TurnOutcome("plan", "stale", reason="stale")
                step = self.steps[self.index]
                cid = step.command_id
                executor = self.app.command_executor
                try:
                    inv = self.invocation(step)
                except ValueError:
                    return self.failure('stale_contract')
                decision = policy.authorize(inv, app=self.app, executor=executor, confirmed=approved)
                if isinstance(decision, policy.Denied):
                    return self.failure(decision.reason)
                if isinstance(decision, policy.NeedsConfirmation):
                    from samsara.effect_policy import plan_input_current, input_confirmation_prompt
                    if policy.pending_operation() is not None:
                        return self.failure("another_confirmation")
                    targets, probe, foreground = policy.bind_staged_target(inv, executor)
                    binding = ({"foreground_target": targets, "foreground_probe": probe} if foreground
                               else {"targets": targets, "target_probe": probe})
                    prompt = input_confirmation_prompt(inv, decision.prompt)
                    op = policy.stage_pending(self.app, inv, prompt,
                        on_approve=lambda _op: self.run(approved=True),
                        refusal_guard=lambda: None if (self.eligible() and plan_input_current(inv, self.app))
                        else "stale", **binding)
                    from samsara.message_delivery import bind_email_question
                    bind_email_question(self.app, op, getattr(inv.effect_target, 'message', None))
                    return ava_pending.ask(self.app, self, "policy", prompt, (), operation=op)
                try:
                    if step.app_name:
                        from plugins.commands.app_verbs import ActionResult
                        result = ava._execute_action2(self.app, cid, step.app_name, route=Route.MODEL,
                            generation=self.generation, source_text=self.utterance, confirmed=approved)
                        state = DispatchState.COMPLETED if result is ActionResult.DONE else DispatchState.FAILED
                    else:
                        extra = ({'args': inv.args, 'binding': step.binding} if step.binding else {})
                        result = executor.execute_canonical(cid, self.app, route=Route.MODEL,
                            generation=self.generation, source_text=self.utterance, confirmed=approved, **extra)
                        state = result.state
                except Exception:
                    return self.failure("exception")
                approved = False  # Approval applies to this step only.
                if state is not DispatchState.COMPLETED:
                    return self.failure(str(state.value))
                self.index += 1
            return ava.TurnOutcome("plan", "completed", name="plan")
