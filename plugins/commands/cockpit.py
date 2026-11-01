"""Memo previews and local readback. All phrases/copy are PLACEHOLDER COPY.

No command takes an authorization flag, provider override, path or raw patch.
External GPT/Claude drafts continue through Cockpit's existing import UI.
"""
from __future__ import annotations

import re

from samsara.plugin_commands import command

WORDS = dict(zip(("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty").split(), range(1, 21)))
WORDS["a"] = 1


def selection_args(remainder):
    value = " ".join((remainder or "").lower().split()).rstrip(".!?")
    if value in ("", "last", "last memo", "last one"):
        return dict(mode="last", count=1)
    if value in ("today", "today's memos"):
        return dict(mode="today")
    match = re.fullmatch(r"(?:last )?(\d+|" + "|".join(WORDS) + r")(?: memos?(?: for cockpit)?)?", value)
    if match:
        count = WORDS.get(match[1], int(match[1]) if match[1].isdigit() else 0)
        if 1 <= count <= 20:
            return dict(mode="last", count=count)
    match = re.fullmatch(r"(?:on )?(\d{4}-\d{2}-\d{2})", value)
    if match:
        return dict(mode="date", on_date=match[1])
    from samsara.cockpit_memos import MemoDisposition
    raise MemoDisposition("PLACEHOLDER: Say last three, today, or on a calendar date; limit twenty memos.")


def announce(app, message):
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        show(message, "info")
    # Inference consent never grants network speech consent.
    tts = app.config.get("tts", {}) or {}
    if tts.get("engine") == "edge" and tts.get("network_voice_accepted") is not True:
        return
    from plugins.commands.ask_ollama import speak
    speak(app, message, category="confirmation")


def runner_for(app, home=None):
    from samsara.cockpit_client import CockpitClient
    from samsara.cockpit_memos import ProposalRunner, load_sources
    from samsara.ava_structured_request import StructuredRequest
    runner = getattr(app, "_cockpit_memo_runner", None)
    if runner is None:
        runner = ProposalRunner(app, CockpitClient(owner_adapter=getattr(app, "_cockpit_owner_adapter", None)), StructuredRequest(app),
                                lambda: load_sources(app.config, home)[0])
        app._cockpit_memo_runner = runner
    return runner


def start_preview(app, args, *, home=None, completed=None):
    from samsara.ava_structured_request import _state
    from samsara.cockpit_memos import load_sources, select_memos
    from samsara.runtime import thread_registry
    # Fail closed before memo or Cockpit access when disabled/unqualified.
    _state(app)
    previous = getattr(app, "_cockpit_memo_runner", None)
    if previous and previous.application_uncertain:
        from samsara.cockpit_memos import MemoDisposition
        raise MemoDisposition("Recover the previous batch receipt before starting another extraction.")
    sources, warnings = load_sources(app.config, home)
    selection = select_memos(sources, warnings=warnings, **args)
    if previous:
        previous.cancel()
    app._cockpit_memo_runner = None
    runner = runner_for(app, home)
    # Capture generation at invocation, not once the worker eventually starts.
    from samsara import execution_policy as policy
    generation = policy.capture_generation(app)
    def work():
        try:
            if not policy.is_fresh(app, generation) or app._cockpit_memo_runner is not runner:
                return
            runner.start(selection, generation)
            if not policy.is_fresh(app, generation) or app._cockpit_memo_runner is not runner:
                return
            if completed:
                completed(runner, "")
            else:
                announce(app, runner.read_page(0) + " PLACEHOLDER: Say read next Cockpit changes, then review Cockpit batch.")
        except Exception as exc:
            # Only safe fixed dispositions may be displayed; never backend body.
            from samsara.cockpit_memos import MemoDisposition
            from samsara.cockpit_client import CockpitError
            from samsara.ava_structured_request import ExtractionRefused
            message = str(exc) if isinstance(exc, (MemoDisposition, CockpitError, ExtractionRefused)) else "PLACEHOLDER: Memo preview failed validation; no changes applied."
            if completed:
                completed(None, message)
            else:
                announce(app, message)
    thread_registry.spawn("cockpit.memo.preview", work, daemon=True)
    return True


@command("preview cockpit memos", aliases=["use memos for cockpit", "turn my last memo into a cockpit update"],
         pack="utilities", risk_class="read", ai_composable=True, side_effects=["network", "ui"],
         param_schema={"remainder": {"type": "str", "required": False, "max_len": 80}},
         preview_template="PLACEHOLDER: Preview selected saved memos; final approval is required.")
def preview_memos(app, remainder=""):
    try:
        return start_preview(app, selection_args(remainder))
    except ValueError as exc:
        announce(app, str(exc))
        return False


@command("use today's memos for cockpit", pack="utilities", risk_class="read", ai_composable=True,
         side_effects=["network", "ui"], param_schema={"remainder": {"type": "str", "max_len": 0}})
def preview_today(app, remainder=""):
    return preview_memos(app, "today")


@command("read next cockpit changes", pack="utilities", risk_class="read", ai_composable=False,
         side_effects=["ui"], param_schema={"remainder": {"type": "str", "max_len": 0}})
def next_changes(app, remainder=""):
    runner = getattr(app, "_cockpit_memo_runner", None)
    if runner is None or runner.proposal is None:
        return False
    page = getattr(runner, "_voice_page", 0) + 1
    if page * 3 >= len(runner.proposal["items"]):
        page = 0
    runner._voice_page = page
    announce(app, runner.read_page(page))
    return True


@command("review cockpit batch", pack="utilities", risk_class="write", ai_composable=False,
         side_effects=["ui"], param_schema={"remainder": {"type": "str", "max_len": 0}})
def review_batch(app, remainder=""):
    runner = getattr(app, "_cockpit_memo_runner", None)
    if runner is None or runner.proposal is None:
        return False
    try:
        runner.stage_approval()
        return True
    except ValueError as exc:
        announce(app, str(exc))
        return False


@command("recover cockpit batch", pack="utilities", risk_class="read", ai_composable=False,
         side_effects=["network", "ui"], param_schema={"remainder": {"type": "str", "max_len": 0}})
def recover_batch(app, remainder=""):
    runner = getattr(app, "_cockpit_memo_runner", None)
    if runner is None or runner.request_id is None:
        announce(app, "PLACEHOLDER: No batch request is available in this session.")
        return False
    try:
        result = runner.recover(runner.request_id)
        receipt = result.get("receipt") or result
        announce(app, "PLACEHOLDER: Batch status " + receipt.get("status", "unknown; keep the request for recovery") + ".")
        return True
    except ValueError as exc:
        announce(app, str(exc))
        return False
@command("skip cockpit item", pack="utilities", risk_class="write", ai_composable=False,
         side_effects=["network", "ui"], param_schema={"remainder": {"type": "str", "required": True, "max_len": 80}})
def skip_item(app, remainder=""):
    runner = getattr(app, "_cockpit_memo_runner", None)
    if runner is None or runner.proposal is None:
        return False
    word = remainder.strip().lower()
    number = WORDS.get(word, int(word) if word.isdigit() else 0)
    rows = runner.proposal["items"]
    if not 1 <= number <= len(rows):
        return False
    keys = [r["item_key"] for i, r in enumerate(rows, 1) if r["included"] and i != number]
    runner.repreview(selected_keys=keys, target_choices=runner.proposal["target_choices"])
    runner._voice_page = 0
    announce(app, runner.read_page(0))
    return True


@command("choose cockpit target", pack="utilities", risk_class="write", ai_composable=False,
         side_effects=["network", "ui"], param_schema={"remainder": {"type": "str", "required": True, "max_len": 80}})
def choose_target(app, remainder=""):
    """PLACEHOLDER: choose cockpit target item two candidate one."""
    runner = getattr(app, "_cockpit_memo_runner", None)
    match = re.fullmatch(r"item (\d+|\w+) candidate (\d+|\w+)", remainder.strip().lower())
    if runner is None or runner.proposal is None or not match:
        return False
    numbers = [WORDS.get(word, int(word) if word.isdigit() else 0) for word in match.groups()]
    item_no, candidate_no = numbers
    rows = runner.proposal["items"]
    if not 1 <= item_no <= len(rows):
        return False
    row = rows[item_no - 1]
    candidates = row["candidates"]
    if not 1 <= candidate_no <= len(candidates):
        return False
    choices = dict(runner.proposal["target_choices"])
    choices[row["item_key"]] = candidates[candidate_no - 1]["id"]
    keys = [r["item_key"] for r in rows if r["included"] or r is row]
    runner.repreview(selected_keys=keys, target_choices=choices)
    runner._voice_page = 0
    announce(app, runner.read_page(0))
    return True


@command("approve cockpit memo batch", pack="utilities", risk_class="destructive", ai_composable=False,
         side_effects=["network"], voice_triggerable=False, reversible=False,
         param_schema={key: {"type": "int" if key == "generation" else "str", "required": True}
                       for key in ("proposal_id", "proposal_hash", "request_id", "source_hash", "context_hash", "generation")})
def approval_identity(app, remainder=""):
    """Registry identity for bound policy only. Dispatch never approves a batch."""
    return False



def briefing_client(app):
    from samsara.cockpit_client import CockpitClient
    return getattr(app, '_cockpit_read_client', None) or CockpitClient(owner_adapter=getattr(app, '_cockpit_owner_adapter', None))


def find_briefing_item(briefing, target):
    from samsara.cockpit_memos import MemoDisposition
    value = ' '.join(target.casefold().split())
    rows = [r for k in ('actionable', 'waiting', 'deferred', 'routines', 'omitted', 'unavailable') for r in briefing.get(k, [])]
    if value in ('', 'that', 'that one'):
        ident = next(iter(briefing.get('recommendations', [])), None)
        matches = [r for r in rows if r['id'] == ident]
    else:
        matches = [r for r in rows if value in (r['id'].casefold(), ' '.join((r.get('title') or '').casefold().split()))]
    if len(matches) != 1:
        raise MemoDisposition('Name one exact Cockpit title or namespaced ID; no target was selected.')
    return matches[0]


def read_briefing(app):
    result = briefing_client(app).briefing()
    app._cockpit_last_briefing = result
    return result['speech_text']


def why_briefing(app, target=''):
    result = getattr(app, '_cockpit_last_briefing', None) or briefing_client(app).briefing()
    row = find_briefing_item(result, target)
    evidence = '; '.join(r['text'] for r in row['reasons'])
    return 'PLACEHOLDER: ' + row['title'] + '. ' + evidence + '. Snapshot ' + result['captured_at'] + '. ' + '; '.join(row.get('warnings', []))


def _briefing_work(app, work):
    from samsara import execution_policy as policy
    generation = policy.capture_generation(app)
    from samsara.runtime import thread_registry
    def run():
        try:
            if not policy.is_fresh(app, generation):
                return
            message = work()
            if policy.is_fresh(app, generation):
                announce(app, message)
        except ValueError as exc:
            if policy.is_fresh(app, generation):
                announce(app, str(exc))
    thread_registry.spawn('cockpit.briefing', run, daemon=True)
    return True


# Deliberately distinct from tasks.py's Todoist `what's on today` identity and aliases.
@command('cockpit today', aliases=['read cockpit today', 'what should i do in cockpit'], pack='utilities',
         risk_class='read', ai_composable=True, side_effects=['network', 'ui'],
         param_schema={'remainder': {'type': 'str', 'max_len': 0}})
def today_briefing(app, remainder=''):
    return _briefing_work(app, lambda: read_briefing(app))


@command('why cockpit item', aliases=['what about cockpit item'], pack='utilities', risk_class='read',
         ai_composable=True, side_effects=['network', 'ui'],
         param_schema={'remainder': {'type': 'str', 'max_len': 300}})
def why_item(app, remainder=''):
    return _briefing_work(app, lambda: why_briefing(app, remainder))


def followup(app, action, target, value=None):
    from datetime import date, timedelta
    from samsara.cockpit_memos import FollowupRunner, MemoDisposition
    from samsara import execution_policy as policy
    generation = policy.capture_generation(app)
    client = briefing_client(app)
    context = client.context()
    # Resolve only against the fresh complete write context; exact title or ID.
    last = getattr(app, '_cockpit_last_briefing', {})
    row = find_briefing_item(dict(actionable=context['records'], recommendations=last.get('recommendations', [])), target)
    if row['kind'] not in ('task', 'event') or not row.get('writable'):
        raise MemoDisposition('Ticket, goal and routine writes are unsupported in V1.')
    tomorrow = (date.fromisoformat(context['local_date']) + timedelta(days=1)).isoformat()
    if action == 'move':
        requested = value if isinstance(value, dict) else {'date': value or 'tomorrow'}
        day = tomorrow if requested['date'] == 'tomorrow' else date.fromisoformat(requested['date']).isoformat()
        if requested.get('time') and row['kind'] != 'event':
            raise MemoDisposition('A task move preserves its deadline; name an appointment to change its clock time.')
        fields = {'start_after' if row['kind'] == 'task' else 'date': day}
        if requested.get('time'):
            fields['time'] = requested['time']
    elif action == 'deadline' and row['kind'] == 'task':
        fields = {'due_date': tomorrow if value == 'tomorrow' else date.fromisoformat(value).isoformat()}
    elif action == 'complete' and row['kind'] == 'task':
        fields = {'status': 'done'}
    elif action == 'priority' and row['kind'] == 'task' and value in ('high', 'normal', 'low'):
        fields = {'priority': value}
    else:
        raise MemoDisposition('This follow-up is unsupported in V1.')
    previous = getattr(app, '_cockpit_memo_runner', None)
    if previous:
        if previous.application_uncertain:
            raise MemoDisposition('Recover the previous receipt before another change.')
        previous.cancel()
    runner = FollowupRunner(app, client)
    app._cockpit_memo_runner = runner
    runner.start_fields(row['id'], fields, context=context, generation=generation)
    message = runner.read_page()
    if row['kind'] == 'event':
        message += ' Appointment: ' + str(row.get('date')) + ' ' + str(row.get('time') or row.get('due_time')) + ' to ' + str(fields.get('date',row.get('date'))) + ' ' + str(fields.get('time',row.get('time') or row.get('due_time'))) + '.'
    if fields.get('start_after') and row.get('due_date') and row['due_date'] < fields['start_after']:
        message += ' Planned work starts after the preserved deadline ' + row['due_date'] + '.'
    message += ' No changes applied. Review Cockpit batch for approval.'
    return message


@command('move cockpit item', pack='utilities', risk_class='write', ai_composable=False,
         side_effects=['network', 'ui'], param_schema={'remainder': {'type':'str','required':True,'max_len':350}})
def move_item(app, remainder=''):
    match = re.fullmatch(r'(.+) to (tomorrow|\d{4}-\d{2}-\d{2})(?: at ((?:[01]\d|2[0-3]):[0-5]\d))?', remainder.strip(), re.I)
    if not match:
        announce(app, 'PLACEHOLDER: Name the Cockpit item followed by to tomorrow.')
        return False
    return _briefing_work(app, lambda: followup(app, 'move', match[1], dict(date=match[2].lower(),time=match[3])))


@command('set cockpit deadline', pack='utilities', risk_class='write', ai_composable=False,
         side_effects=['network', 'ui'], param_schema={'remainder': {'type':'str','required':True,'max_len':350}})
def set_deadline(app, remainder=''):
    match = re.fullmatch(r'(.+) to (tomorrow|\d{4}-\d{2}-\d{2})', remainder.strip(), re.I)
    if not match:
        return False
    return _briefing_work(app, lambda: followup(app, 'deadline', match[1], match[2].lower()))


@command('complete cockpit item', pack='utilities', risk_class='write', ai_composable=False,
         side_effects=['network', 'ui'], param_schema={'remainder': {'type':'str','required':True,'max_len':300}})
def complete_item(app, remainder=''):
    return _briefing_work(app, lambda: followup(app, 'complete', remainder))


@command('set cockpit priority', pack='utilities', risk_class='write', ai_composable=False,
         side_effects=['network', 'ui'], param_schema={'remainder': {'type':'str','required':True,'max_len':350}})
def set_priority(app, remainder=''):
    match = re.fullmatch(r'(.+) to (high|normal|low)', remainder.strip(), re.I)
    if not match:
        return False
    return _briefing_work(app, lambda: followup(app, 'priority', match[1], match[2].lower()))


@command('undo cockpit update', pack='utilities', risk_class='write', ai_composable=False,
         side_effects=['network', 'ui'], param_schema={'remainder': {'type':'str','max_len':100}})
def undo_update(app, remainder=''):
    def work():
        from samsara.cockpit_memos import FollowupRunner, MemoDisposition
        old = getattr(app, '_cockpit_memo_runner', None)
        receipt = ((old.last_result or {}).get('receipt') or old.last_result or {}) if old else {}
        receipt_id = remainder.strip() or receipt.get('receipt_id')
        if not receipt_id:
            raise MemoDisposition('Name the applied receipt to preview its undo.')
        if old:
            if old.application_uncertain:
                raise MemoDisposition('Recover the previous receipt before undo.')
            old.cancel()
        runner = FollowupRunner(app, briefing_client(app))
        app._cockpit_memo_runner = runner
        runner.start_undo(receipt_id)
        return runner.read_page() + ' No changes applied. Review Cockpit batch for approval.'
    return _briefing_work(app, work)
