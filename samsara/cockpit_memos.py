"""Selected memo snapshots and reviewed Cockpit proposals.

Pure v1 validation below is vendored from the reviewed 975a/b contract.
No conversation/memory/profile is consulted. All UI strings are PLACEHOLDER COPY.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXTRACTION_VERSION = "cockpit.memo-extraction.v1"
SCHEMA_SHA256 = "cd82a4d42f52a7e5261fea46613b9ca4b89f1dbf5179ad1598c4abb6195fe2b2"
EXTRACTION_SCHEMA = json.loads('{"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"urn:cockpit:memo-extraction:1","title":"975 proposed extraction contract; all examples synthetic","type":"object","properties":{"schema_version":{"const":"cockpit.memo-extraction.v1"},"items":{"type":"array","items":{"oneOf":[{"type":"object","properties":{"item_key":{"type":"string","minLength":1,"maxLength":80},"action":{"const":"create_task"},"target_id":{"type":"null"},"fields":{"type":"object","properties":{"title":{"type":"string","minLength":1,"maxLength":300},"due_date":{"anyOf":[{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},{"type":"null"}]},"due_time":{"anyOf":[{"type":"string","pattern":"^(?:[01]\\\\d|2[0-3]):[0-5]\\\\d$"},{"type":"null"}]},"status":{"enum":["open","done"]},"notes_append":{"type":"string","minLength":1,"maxLength":2000},"priority":{"enum":["high","normal","low"]},"effort_minutes":{"type":"integer","minimum":1,"maximum":1440},"blocked_by":{"type":"array","items":{"type":"string","minLength":1,"maxLength":120},"maxItems":20},"start_after":{"anyOf":[{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},{"type":"null"}]}},"required":["title"],"additionalProperties":false,"minProperties":1},"evidence":{"type":"array","items":{"$ref":"#/$defs/evidence"},"minItems":1,"maxItems":20},"match":{"type":"null"},"time_resolution":{"anyOf":[{"$ref":"#/$defs/time_resolution"},{"type":"null"}]}},"required":["item_key","action","target_id","fields","evidence","match","time_resolution"],"additionalProperties":false},{"type":"object","properties":{"item_key":{"type":"string","minLength":1,"maxLength":80},"action":{"const":"update_task"},"target_id":{"type":"string","minLength":1,"maxLength":120},"fields":{"type":"object","properties":{"title":{"type":"string","minLength":1,"maxLength":300},"due_date":{"anyOf":[{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},{"type":"null"}]},"due_time":{"anyOf":[{"type":"string","pattern":"^(?:[01]\\\\d|2[0-3]):[0-5]\\\\d$"},{"type":"null"}]},"status":{"enum":["open","done"]},"notes_append":{"type":"string","minLength":1,"maxLength":2000},"priority":{"enum":["high","normal","low"]},"effort_minutes":{"type":"integer","minimum":1,"maximum":1440},"blocked_by":{"type":"array","items":{"type":"string","minLength":1,"maxLength":120},"maxItems":20},"start_after":{"anyOf":[{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},{"type":"null"}]}},"required":[],"additionalProperties":false,"minProperties":1},"evidence":{"type":"array","items":{"$ref":"#/$defs/evidence"},"minItems":1,"maxItems":20},"match":{"$ref":"#/$defs/match"},"time_resolution":{"anyOf":[{"$ref":"#/$defs/time_resolution"},{"type":"null"}]}},"required":["item_key","action","target_id","fields","evidence","match","time_resolution"],"additionalProperties":false},{"type":"object","properties":{"item_key":{"type":"string","minLength":1,"maxLength":80},"action":{"const":"create_event"},"target_id":{"type":"null"},"fields":{"type":"object","properties":{"title":{"type":"string","minLength":1,"maxLength":300},"date":{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},"time":{"type":"string","pattern":"^(?:[01]\\\\d|2[0-3]):[0-5]\\\\d$"},"end_time":{"anyOf":[{"type":"string","pattern":"^(?:[01]\\\\d|2[0-3]):[0-5]\\\\d$"},{"type":"null"}]},"timezone":{"type":"string","minLength":1,"maxLength":80},"notes_append":{"type":"string","minLength":1,"maxLength":2000},"related_task_id":{"anyOf":[{"type":"string","minLength":1,"maxLength":120},{"type":"null"}]}},"required":["title","date","time","timezone"],"additionalProperties":false,"minProperties":1},"evidence":{"type":"array","items":{"$ref":"#/$defs/evidence"},"minItems":1,"maxItems":20},"match":{"type":"null"},"time_resolution":{"anyOf":[{"$ref":"#/$defs/time_resolution"},{"type":"null"}]}},"required":["item_key","action","target_id","fields","evidence","match","time_resolution"],"additionalProperties":false},{"type":"object","properties":{"item_key":{"type":"string","minLength":1,"maxLength":80},"action":{"const":"update_event"},"target_id":{"type":"string","minLength":1,"maxLength":120},"fields":{"type":"object","properties":{"title":{"type":"string","minLength":1,"maxLength":300},"date":{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},"time":{"type":"string","pattern":"^(?:[01]\\\\d|2[0-3]):[0-5]\\\\d$"},"end_time":{"anyOf":[{"type":"string","pattern":"^(?:[01]\\\\d|2[0-3]):[0-5]\\\\d$"},{"type":"null"}]},"timezone":{"type":"string","minLength":1,"maxLength":80},"notes_append":{"type":"string","minLength":1,"maxLength":2000},"related_task_id":{"anyOf":[{"type":"string","minLength":1,"maxLength":120},{"type":"null"}]}},"required":[],"additionalProperties":false,"minProperties":1},"evidence":{"type":"array","items":{"$ref":"#/$defs/evidence"},"minItems":1,"maxItems":20},"match":{"$ref":"#/$defs/match"},"time_resolution":{"anyOf":[{"$ref":"#/$defs/time_resolution"},{"type":"null"}]}},"required":["item_key","action","target_id","fields","evidence","match","time_resolution"],"additionalProperties":false},{"type":"object","properties":{"item_key":{"type":"string","minLength":1,"maxLength":80},"action":{"const":"append_note"},"target_id":{"type":"null"},"fields":{"type":"object","properties":{"text":{"type":"string","minLength":1,"maxLength":2000},"date":{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},"related_task_id":{"anyOf":[{"type":"string","minLength":1,"maxLength":120},{"type":"null"}]}},"required":["text","date"],"additionalProperties":false},"evidence":{"type":"array","items":{"$ref":"#/$defs/evidence"},"minItems":1,"maxItems":20},"match":{"type":"null"},"time_resolution":{"anyOf":[{"$ref":"#/$defs/time_resolution"},{"type":"null"}]}},"required":["item_key","action","target_id","fields","evidence","match","time_resolution"],"additionalProperties":false}]},"maxItems":100},"unresolved":{"type":"array","items":{"$ref":"#/$defs/unresolved"},"maxItems":100},"no_change":{"type":"array","items":{"type":"object","properties":{"reason":{"enum":["duplicate","already_done","non_action","unsupported_media","unsupported_target","unsafe_instruction"]},"evidence":{"type":"array","items":{"$ref":"#/$defs/evidence"},"minItems":1,"maxItems":20},"related_item_key":{"anyOf":[{"type":"string","minLength":1,"maxLength":80},{"type":"null"}]}},"required":["reason","evidence","related_item_key"],"additionalProperties":false},"maxItems":100}},"required":["schema_version","items","unresolved","no_change"],"additionalProperties":false,"$defs":{"evidence":{"type":"object","properties":{"memo_id":{"type":"string","minLength":1,"maxLength":120},"start":{"type":"integer","minimum":0},"end":{"type":"integer","minimum":1}},"required":["memo_id","start","end"],"additionalProperties":false},"match":{"type":"object","properties":{"method":{"enum":["explicit_id","exact_title","semantic"]},"candidates":{"type":"array","minItems":1,"maxItems":5,"items":{"type":"object","properties":{"id":{"type":"string","minLength":1,"maxLength":120},"reason":{"type":"string","minLength":1,"maxLength":250}},"required":["id","reason"],"additionalProperties":false}},"needs_resolution":{"type":"boolean"}},"required":["method","candidates","needs_resolution"],"additionalProperties":false},"time_resolution":{"type":"object","properties":{"basis_memo_id":{"type":"string","minLength":1,"maxLength":120},"phrase_start":{"type":"integer","minimum":0},"phrase_end":{"type":"integer","minimum":1},"anchor_local_date":{"type":"string","pattern":"^\\\\d{4}-\\\\d{2}-\\\\d{2}$"},"timezone":{"type":"string","minLength":1,"maxLength":80},"rule":{"enum":["explicit","today","tomorrow","next_weekday"]}},"required":["basis_memo_id","phrase_start","phrase_end","anchor_local_date","timezone","rule"],"additionalProperties":false},"unresolved":{"type":"object","properties":{"key":{"type":"string","minLength":1,"maxLength":80},"reason":{"enum":["ambiguous_target","ambiguous_time","contradiction","missing_transcript","unsupported_target","missing_details"]},"candidate_ids":{"type":"array","items":{"type":"string","minLength":1,"maxLength":120},"maxItems":5},"question":{"type":"string","minLength":1,"maxLength":300},"evidence":{"type":"array","items":{"$ref":"#/$defs/evidence"},"minItems":1,"maxItems":20}},"required":["key","reason","candidate_ids","question","evidence"],"additionalProperties":false}}}')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Invalid calendar date")
    return date.fromisoformat(value)


def _title_words(value):
    return " ".join(re.findall(r"\w+", str(value).casefold()))


def _schema_check(value, schema, root):
    supported = {"$schema", "$id", "title", "$defs", "$ref", "type", "properties", "required",
                 "additionalProperties", "minProperties", "items", "minItems", "maxItems",
                 "minLength", "maxLength", "pattern", "minimum", "maximum", "enum", "const",
                 "oneOf", "anyOf", "description"}
    if set(schema) - supported:
        raise ValueError("Unsupported schema vocabulary")
    if "$ref" in schema:
        return _schema_check(value, root["$defs"][schema["$ref"].removeprefix("#/$defs/")], root)
    for key in ("oneOf", "anyOf"):
        if key in schema:
            count = 0
            for branch in schema[key]:
                try:
                    _schema_check(value, branch, root)
                    count += 1
                except ValueError:
                    pass
            if (key == "oneOf" and count != 1) or (key == "anyOf" and count == 0):
                raise ValueError("Schema alternative mismatch")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if "type" in schema and type(value) is not types[schema["type"]]:
        raise ValueError("Schema type mismatch")
    for key in ("const", "enum"):
        choices = [schema[key]] if key == "const" and key in schema else schema.get(key, [])
        if key in schema and not any(type(value) is type(v) and value == v for v in choices):
            raise ValueError("Schema value mismatch")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if (not set(schema.get("required", [])) <= value.keys() or
            (schema.get("additionalProperties") is False and value.keys() - props.keys()) or
            len(value) < schema.get("minProperties", 0)):
            raise ValueError("Schema object keys mismatch")
        for key in value.keys() & props.keys():
            _schema_check(value[key], props[key], root)
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise ValueError("Schema array bounds")
        if "items" in schema:
            for item in value:
                _schema_check(item, schema["items"], root)
    if isinstance(value, str):
        if (not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")) or
            ("pattern" in schema and not re.search(schema["pattern"], value))):
            raise ValueError("Schema string constraint")
    if type(value) is int and not schema.get("minimum", -float("inf")) <= value <= schema.get("maximum", float("inf")):
        raise ValueError("Schema number bounds")


def normalize_memo_v1(memo):
    result = deepcopy(memo)
    result["memo_id"] = memo.get("memo_id", memo.get("id"))
    if not isinstance(result["memo_id"], str) or not isinstance(memo.get("text"), str):
        raise ValueError("Memo ID/text required")
    result["text"] = memo["text"].replace("\r\n", "\n")
    result["text_sha256"] = hashlib.sha256(result["text"].encode()).hexdigest()
    result["capture_timezone"] = memo.get("capture_timezone", memo.get("timezone"))
    result["has_transcript"] = memo.get("has_transcript", not memo.get("media_only", False))
    return result


def normalize_extraction_v1(draft, context, memos, *, validate=True):
    """Bridge bare source IDs only when unique; never match titles or invent IDs."""
    result = deepcopy(draft)
    records = context["records"]
    def resolve(value, kind=None):
        if value is None:
            return None
        matches = [r["id"] for r in records if (r["id"] == value or r.get("source_id") == value)
                   and (kind is None or r["kind"] == kind)]
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous context ID")
        return matches[0]
    for item in result.get("items", []):
        kind = "task" if item["action"] == "update_task" else "event" if item["action"] == "update_event" else None
        item["target_id"] = resolve(item.get("target_id"), kind)
        for key in ("blocked_by",):
            if key in item["fields"]:
                item["fields"][key] = [resolve(v, "task") for v in item["fields"][key]]
        if "related_task_id" in item["fields"]:
            item["fields"]["related_task_id"] = resolve(item["fields"]["related_task_id"], "task")
        if item.get("match"):
            for candidate in item["match"]["candidates"]:
                candidate["id"] = resolve(candidate["id"], kind)
    for entry in result.get("unresolved", []):
        entry["candidate_ids"] = [resolve(v) for v in entry["candidate_ids"]]
    return validate_extraction_v1(result, context, memos) if validate else result


def validate_extraction_v1(draft, context, memos):
    schema = EXTRACTION_SCHEMA
    _schema_check(draft, schema, schema)
    records = {r["id"]: r for r in context["records"]}
    sources = {}
    for memo in memos:
        memo = normalize_memo_v1(memo)
        if memo["memo_id"] in sources:
            raise ValueError("Duplicate memo ID")
        sources[memo["memo_id"]] = memo
    if len(sources) > 20 or sum(len(m["text"]) for m in sources.values()) > 50000:
        raise ValueError("Oversized memo selection")
    def known(ident, kind=None):
        row = records.get(ident)
        if row is None or (kind and row["kind"] != kind):
            raise ValueError("Wrong or unknown target namespace")
        return row
    def span(mid, start, end):
        memo = sources.get(mid)
        if not memo or not 0 <= start < end <= len(memo["text"]):
            raise ValueError("Invalid evidence span")
        return memo
    keys = set()
    graph = {r["id"]: set(r.get("blocked_by") or []) for r in records.values() if r["kind"] == "task"}
    for group in ("items", "unresolved", "no_change"):
        for item in draft[group]:
            key = item.get("item_key", item.get("key"))
            if key is not None:
                if key in keys:
                    raise ValueError("Duplicate item key")
                keys.add(key)
            for ev in item["evidence"]:
                memo = span(ev["memo_id"], ev["start"], ev["end"])
                if group == "items" and not memo["has_transcript"]:
                    raise ValueError("No transcript for extraction")
            for ident in item.get("candidate_ids", []):
                known(ident)
    patches = {}
    for item in draft["items"]:
        fields = item["fields"]
        action = item["action"]
        target = item["target_id"]
        evidence_text = " ".join(sources[e["memo_id"]]["text"][e["start"]:e["end"]] for e in item["evidence"])
        base = {}
        if action.startswith("update_"):
            base = known(target, "task" if action == "update_task" else "event")
            if not base.get("writable", False):
                raise ValueError("Unsupported write target")
            match = item["match"]
            ids = [c["id"] for c in match["candidates"]]
            if target not in ids or len(ids) != len(set(ids)):
                raise ValueError("Target match mismatch")
            for ident in ids:
                known(ident, base["kind"])
            if (match["method"] == "semantic" or len(ids) > 1) and not match["needs_resolution"]:
                raise ValueError("Ambiguous match requires resolution")
            if match["method"] == "explicit_id":
                if not any(re.search(r"(?<!\w)"+re.escape(str(ident))+r"(?!\w)", evidence_text)
                           for ident in (base.get("source_id", target), target)):
                    raise ValueError("Explicit ID absent from evidence")
            elif match["method"] == "exact_title":
                title = _title_words(base.get("title"))
                if not title or title not in _title_words(evidence_text):
                    raise ValueError("Exact title absent from evidence")
                equal_titles = [r for r in records.values() if r["kind"] == base["kind"]
                                and _title_words(r.get("title")) == title]
                if len(equal_titles) > 1 and not match["needs_resolution"]:
                    raise ValueError("Duplicate exact titles require resolution")
            for field, value in fields.items():
                prior = patches.setdefault((target, field), value)
                if prior != value:
                    raise ValueError("Contradictory updates require resolution")
        for field in ("due_date", "date", "start_after"):
            if fields.get(field) is not None:
                _day(fields[field])
        if "task" in action:
            merged = dict(base, **fields)
            if merged.get("due_time") and not merged.get("due_date"):
                raise ValueError("Task time requires due date")
        related = fields.get("related_task_id")
        if related:
            known(related, "task")
        if "blocked_by" in fields:
            for ident in fields["blocked_by"]:
                known(ident, "task")
                if ident == target:
                    raise ValueError("Self dependency")
            graph[target or "new:"+item["item_key"]] = set(fields["blocked_by"])
        evidence_text = evidence_text.casefold()
        for field, pattern in (("priority", r"\b(urgent\w*|priority|high|low)\b"),
                               ("effort_minutes", r"\b(minute\w*|hour\w*|effort)\b"),
                               ("blocked_by", r"\b(block\w*|depend\w*|waiting)\b")):
            if field in fields and not re.search(pattern, evidence_text):
                raise ValueError("Planning change lacks explicit evidence")
        if action.endswith("event"):
            merged = dict(base, **fields)
            if not all(merged.get(k) for k in ("date", "time", "timezone")):
                raise ValueError("Event needs date/time/timezone")
            _day(merged["date"])
            try:
                zone = ZoneInfo(merged["timezone"])
                wall = datetime.fromisoformat(merged["date"]+"T"+merged["time"])
                offsets = set()
                for fold in (0, 1):
                    aware = wall.replace(tzinfo=zone, fold=fold)
                    if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall:
                        offsets.add(aware.utcoffset())
                if len(offsets) != 1:
                    raise ValueError("Ambiguous or nonexistent event wall time")
            except (ZoneInfoNotFoundError, TypeError) as exc:
                raise ValueError("Unknown event timezone") from exc
            if merged.get("end_time") and merged["end_time"] <= merged["time"]:
                raise ValueError("Event end must follow start")
        temporal = item["time_resolution"]
        if temporal:
            memo = span(temporal["basis_memo_id"], temporal["phrase_start"], temporal["phrase_end"])
            if temporal["basis_memo_id"] not in {e["memo_id"] for e in item["evidence"]}:
                raise ValueError("Temporal basis outside item evidence")
            if not any(e["memo_id"] == temporal["basis_memo_id"] and
                       e["start"] <= temporal["phrase_start"] < temporal["phrase_end"] <= e["end"]
                       for e in item["evidence"]):
                raise ValueError("Temporal phrase outside evidence span")
            anchor = _day(temporal["anchor_local_date"])
            try:
                zone = ZoneInfo(temporal["timezone"])
                captured = datetime.fromisoformat(memo["captured_at"])
                if captured.tzinfo is None or memo["capture_timezone"] != temporal["timezone"]:
                    raise ValueError("Unknown capture timezone")
                if captured.astimezone(zone).date() != anchor:
                    raise ValueError("Incorrect capture date anchor")
            except (KeyError, ZoneInfoNotFoundError) as exc:
                raise ValueError("Missing temporal capture basis") from exc
            rule = temporal["rule"]
            if rule in ("today", "tomorrow"):
                resolved = (anchor + timedelta(days=rule == "tomorrow")).isoformat()
                actual = fields.get("start_after", fields.get("due_date", fields.get("date")))
                if actual != resolved:
                    raise ValueError("Incorrect relative date resolution")
            elif rule == "next_weekday":
                phrase = memo["text"][temporal["phrase_start"]:temporal["phrase_end"]].casefold()
                weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                matches = [i for i, name in enumerate(weekdays) if re.search(r"\b"+name+r"\b", phrase)]
                if len(matches) != 1:
                    raise ValueError("Unknown weekday resolution")
                resolved = (anchor + timedelta(days=(matches[0]-anchor.weekday()) % 7 or 7)).isoformat()
                actual = fields.get("start_after", fields.get("due_date", fields.get("date")))
                if actual != resolved:
                    raise ValueError("Incorrect weekday resolution")
    def visit(node, stack, visited):
        if node in stack:
            raise ValueError("Dependency cycle")
        if node in visited:
            return
        for link in graph.get(node, ()):
            known(link, "task")
            visit(link, stack | {node}, visited)
        visited.add(node)
    visited = set()
    for node in graph:
        visit(node, set(), visited)
    item_keys = {i["item_key"] for i in draft["items"]}
    for entry in draft["no_change"]:
        if entry["related_item_key"] is not None and entry["related_item_key"] not in item_keys:
            raise ValueError("Unknown related item key")
    return deepcopy(draft)


# ---- Read-only source adapters and immutable selection --------------------

class MemoDisposition(ValueError):
    """A visible, path-free reason to stop before inference/application."""


def quick_source(record):
    text = str(record.get("text", "")).replace("\r\n", "\n")
    video_label = bool(record.get("video") or record.get("source") == "video") and text.strip() in {
        "Video memo", "Video memo (partial)"}
    return dict(memo_id=str(record["id"]), source_kind="quick", source_locator=str(record["id"]),
                captured_at=record.get("at", ""), capture_timezone=record.get("capture_timezone"),
                text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                has_transcript=bool(text.strip()) and not video_label,
                partial=bool(record.get("partial")), upstream_id=record.get("upstream_id"))


def legacy_sources(note_path):
    """Read exactly one configured note, never list/glob a vault or read media."""
    path = Path(note_path)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    entries = re.finditer(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2})(?:[^\n]*)\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    result, occurrences = [], {}
    for entry in entries:
        stamp, body = entry.groups()
        datetime.fromisoformat(stamp)
        embeds = re.findall(r"!\[\[([^\]]+)\]\]", body)
        transcript = re.sub(r"!\[\[[^\]]+\]\]", "", body).strip()
        locator = _hash(dict(note=str(path.resolve()), captured=stamp, embeds=embeds))
        occurrences[locator] = occurrences.get(locator, 0) + 1
        revision = hashlib.sha256(transcript.encode()).hexdigest()
        upstream = None
        for embed in embeds:
            match = re.fullmatch(r"memo_(.+)\.wav", Path(embed).name)
            if match:
                upstream = "quick:" + match[1]
        result.append(dict(memo_id="legacy_" + _hash([locator, occurrences[locator], revision]),
                           source_kind="legacy", source_locator=locator, captured_at=stamp.replace(" ", "T"),
                           capture_timezone=None, text=transcript, text_sha256=revision,
                           has_transcript=bool(transcript), partial=False, upstream_id=upstream))
    return result


def load_sources(config, home=None):
    """Read-only default universe. Unlike load_memos, never rebuilds an index."""
    from samsara import quick_memo
    warnings, records = [], []
    path = quick_memo.index_file(home)
    try:
        if path.exists():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            records = [quick_source(row) for row in rows]
        else:
            # Explicit legacy quick markdown fallback with deterministic IDs.
            markdown = quick_memo.memo_file(home)
            if markdown.exists():
                for memo in legacy_sources(markdown):
                    memo["source_kind"] = "quick_markdown"
                    records.append(memo)
    except (OSError, ValueError, KeyError, TypeError):
        warnings.append("Indexed memos are unavailable; the source universe is incomplete.")
    legacy = config.get("voice_memo", {}) or {}
    if legacy.get("vault_dir"):
        vault = Path(legacy["vault_dir"]).resolve()
        note = (vault / legacy.get("note_relpath", "Voice Memos.md")).resolve()
        try:
            if not note.is_relative_to(vault):
                raise ValueError("Configured note leaves vault")
            old = legacy_sources(note)
            by_id = {"quick:" + r["memo_id"]: r for r in records}
            for memo in old:
                original = by_id.get(memo["upstream_id"])
                # Filename establishes lineage only when capture and content agree.
                if (original and original["text_sha256"] == memo["text_sha256"]
                        and original["captured_at"][:16] == memo["captured_at"][:16]):
                    continue  # alias, not a second origin
                records.append(memo)
        except (OSError, ValueError):
            warnings.append("Configured legacy memo note is unavailable; the source universe is incomplete.")
    return records, warnings


@dataclass(frozen=True)
class Selection:
    # JSON storage avoids a mutable nested dict escaping a frozen dataclass.
    _snapshot: str = field(repr=False)
    timezone: str
    mode: str
    warnings: tuple = ()

    @property
    def memos(self):
        return json.loads(self._snapshot)

    @property
    def manifest_hash(self):
        return _hash(self.memos)

    def unchanged(self, current):
        by_id = {m["memo_id"]: m for m in current}
        return all(by_id.get(m["memo_id"]) == m for m in self.memos)


def select_memos(sources, *, mode="last", count=1, on_date=None, checked_ids=(),
                 now=None, display_timezone=None, allow_partial=False, warnings=()):
    now = now or datetime.now().astimezone()
    # astimezone(None) asks the OS for the rules at EACH capture instant.
    # now.tzinfo alone may be a fixed current offset and gets DST days wrong.
    zone = ZoneInfo(display_timezone) if display_timezone else None
    label = display_timezone or str(now.astimezone().tzinfo)
    checked = set(checked_ids)
    if mode not in {"last", "today", "date", "checked"}:
        raise MemoDisposition("Unsupported memo selection.")
    if mode == "last" and (type(count) is not int or not 1 <= count <= 20):
        raise MemoDisposition("Choose between one and twenty memos.")
    day = now.astimezone(zone).date() if mode == "today" else date.fromisoformat(on_date) if mode == "date" else None
    def captured(m):
        try:
            moment = datetime.fromisoformat(m["captured_at"])
            return (moment.replace(tzinfo=zone) if moment.tzinfo is None and zone is not None
                    else moment.astimezone(zone))
        except (ValueError, TypeError, KeyError):
            raise MemoDisposition("A memo has an invalid capture timestamp.") from None
    ordered = sorted(sources, key=lambda m: (captured(m), m["memo_id"]), reverse=True)
    if mode == "last":
        chosen = ordered[:count]
    elif mode == "checked":
        chosen = [m for m in ordered if m["memo_id"] in checked]
        if {m["memo_id"] for m in chosen} != checked:
            raise MemoDisposition("A checked memo is no longer available.")
    else:
        chosen = [m for m in ordered if captured(m).date() == day]
    if not chosen:
        raise MemoDisposition("No saved memos match this selection.")
    if len(chosen) > 20 or sum(len(m["text"]) for m in chosen) > 50000:
        raise MemoDisposition("Selection exceeds twenty memos or 50,000 characters; choose a smaller batch.")
    if len({m["memo_id"] for m in chosen}) != len(chosen):
        raise MemoDisposition("Duplicate source identity; resolve the source before extraction.")
    if any(m["partial"] for m in chosen) and not (allow_partial or mode == "checked"):
        raise MemoDisposition("Partial memos need explicit checked selection.")
    notes = list(warnings)
    if any(not m.get("capture_timezone") for m in chosen):
        notes.append("Capture timezone is unknown for some memos; relative dates need resolution.")
    # Equal text alone does not prove shared origin.
    if len({m["text_sha256"] for m in chosen}) < len(chosen):
        notes.append("Possible duplicate sources are retained for review.")
    return Selection(json.dumps(chosen, ensure_ascii=False, sort_keys=True), label, mode, tuple(notes))


def _missing_transcripts(memos):
    return [dict(key="media_" + _hash(m["memo_id"])[:20], reason="missing_transcript", candidate_ids=[],
                 question="PLACEHOLDER: This memo needs a transcript before it can be processed.",
                 evidence=[dict(memo_id=m["memo_id"], start=0, end=len(m["text"]))])
            for m in memos if not m["has_transcript"] and m["text"]]


def _dst_unresolved(draft, context, memos=()):
    """Demote unsafe event wall times; preserve all other strict validation."""
    _schema_check(draft, EXTRACTION_SCHEMA, EXTRACTION_SCHEMA)
    result = deepcopy(draft)
    records = {r["id"]: r for r in context["records"]}
    sources = {m["memo_id"]: m for m in memos}
    for item in list(result["items"]):
        temporal = item["time_resolution"]
        if temporal and sources:
            source = sources.get(temporal["basis_memo_id"], {})
            try:
                unknown = (not source.get("capture_timezone") or
                           datetime.fromisoformat(source["captured_at"]).tzinfo is None)
            except (ValueError, KeyError):
                unknown = True
            if unknown:
                result["items"].remove(item)
                result["unresolved"].append(dict(key="capture_" + _hash(item["item_key"])[:20],
                    reason="ambiguous_time", candidate_ids=[item["target_id"]] if item["target_id"] else [],
                    question="PLACEHOLDER: Confirm this memo's capture timezone before resolving its date.",
                    evidence=item["evidence"]))
                continue
        if not item["action"].endswith("event"):
            continue
        fields = dict(records.get(item["target_id"], {}), **item["fields"])
        try:
            wall = datetime.fromisoformat(fields["date"] + "T" + fields["time"])
            zone = ZoneInfo(fields["timezone"])
            offsets = {wall.replace(tzinfo=zone, fold=f).utcoffset() for f in (0, 1)
                       if wall.replace(tzinfo=zone, fold=f).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall}
            end = fields.get("end_time")
            if end:
                end_wall = datetime.fromisoformat(fields["date"] + "T" + end)
                end_offsets = {end_wall.replace(tzinfo=zone, fold=f).utcoffset() for f in (0, 1)
                               if end_wall.replace(tzinfo=zone, fold=f).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == end_wall}
            else:
                end_offsets = offsets
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            continue  # strict validator reports malformed fields
        if len(offsets) != 1 or len(end_offsets) != 1:
            result["items"].remove(item)
            result["unresolved"].append(dict(key="dst_" + _hash(item["item_key"])[:20], reason="ambiguous_time",
                candidate_ids=[item["target_id"]] if item["target_id"] else [],
                question="PLACEHOLDER: Choose a unique valid appointment time.", evidence=item["evidence"]))
    remaining = {i["item_key"] for i in result["items"]}
    for entry in result["no_change"]:
        if entry["related_item_key"] not in remaining:
            entry["related_item_key"] = None
    return result


class ProposalRunner:
    """One preview session, bound to one source snapshot and app generation."""
    def __init__(self, app, client, extractor, source_reader):
        self.app, self.client, self.extractor, self.source_reader = app, client, extractor, source_reader
        self.selection = self.context = self.proposal = self.draft = self.operation = None
        self.request_id = None
        self.last_result = None
        self.application_uncertain = False

    def start(self, selection, generation=None):
        from samsara import execution_policy as policy
        from samsara.ava_structured_request import _state
        if self.application_uncertain:
            raise MemoDisposition("Recover the previous batch receipt before starting another extraction.")
        self.cancel()
        old = policy.pending_operation()
        if old and old.invocation.command_id == "approve cockpit memo batch":
            policy._release_slot(old)
            old.cancel(self.app, "new memo preview", chip=False)
        self.generation = policy.capture_generation(self.app) if generation is None else generation
        self._cancelled = False
        self._voice_page = 0
        if not policy.is_fresh(self.app, self.generation):
            raise MemoDisposition("Extraction cancelled before start.")
        self.provider_identity = _state(self.app)[1]
        self.selection = selection
        self.request_id = uuid.uuid4().hex
        self.last_result = None
        self.context = self.client.context()
        memos = selection.memos
        if not selection.unchanged(self.source_reader()):
            raise MemoDisposition("Selected memos changed; select again.")
        transcribed = [m for m in memos if m["has_transcript"]]
        if transcribed:
            draft = self.extractor.extract(transcribed, self.context, self.generation)
        else:
            draft = dict(schema_version=EXTRACTION_VERSION, items=[], unresolved=[], no_change=[])
        draft["unresolved"].extend(_missing_transcripts(memos))
        _schema_check(draft, EXTRACTION_SCHEMA, EXTRACTION_SCHEMA)
        canonical = normalize_extraction_v1(draft, self.context, memos, validate=False)
        self.draft = validate_extraction_v1(_dst_unresolved(canonical, self.context, memos), self.context, memos)
        return self.repreview()

    def _current(self):
        from samsara import execution_policy as policy
        from samsara.ava_structured_request import _state
        if self._cancelled or not policy.is_fresh(self.app, self.generation):
            return False
        try:
            return (_state(self.app)[1] == self.provider_identity
                    and self.selection.unchanged(self.source_reader()))
        except Exception:
            return False

    def repreview(self, *, selected_keys=None, target_choices=None):
        from samsara import execution_policy as policy
        if self.operation is not None:
            policy._release_slot(self.operation)
            self.operation.cancel(self.app, "preview changed", chip=False)
            self.operation = None
        if self._cancelled or not policy.is_fresh(self.app, self.generation) or not self.selection.unchanged(self.source_reader()):
            raise MemoDisposition("Preview source or generation changed; start again.")
        state = self.context["sources"]["state"]
        envelope = dict(schema_version="cockpit.changes.v1", request_id=self.request_id,
                        base_revision=state["revision"], base_hash=state["hash"],
                        sources=self.selection.memos, extraction=self.draft)
        if selected_keys is not None:
            envelope["selected_keys"] = selected_keys
        if target_choices is not None:
            envelope["target_choices"] = target_choices
        proposal = self.client.preview(envelope)
        if (self._cancelled or not policy.is_fresh(self.app, self.generation)
                or not self.selection.unchanged(self.source_reader())):
            self.client.cancel()
            raise MemoDisposition("Late preview discarded after cancellation or source change.")
        manifest = [{k: m.get(k) for k in ("memo_id", "text_sha256", "captured_at", "capture_timezone",
                                         "source_kind", "partial", "upstream_id")} for m in self.selection.memos]
        unsigned = {k: v for k, v in proposal.items() if k != "proposal_hash"}
        if (proposal.get("schema_version") != "cockpit.changes.v1"
                or proposal.get("request_id") != self.request_id
                or proposal.get("base_revision") != state["revision"]
                or proposal.get("base_hash") != state["hash"]
                or proposal.get("source_hash") != _hash(manifest)
                or proposal.get("extraction_hash") != _hash(self.draft)
                or proposal.get("proposal_hash") != _hash(unsigned)):
            self.proposal = None
            raise MemoDisposition("Cockpit preview does not match the selected source and snapshot.")
        self.proposal = proposal
        self._preview_fingerprint = _hash(self.proposal)
        self._binding = {k: self.proposal[k] for k in ("proposal_id", "proposal_hash", "request_id")}
        self._binding.update(source_hash=self.selection.manifest_hash,
                             context_hash=_hash(self.context), generation=self.generation)
        self._reviewed = set()
        return deepcopy(self.proposal)

    def read_page(self, page=0, page_size=3):
        """Local speech templates, complete changed values; model questions unused."""
        if type(page) is not int or page < 0 or not 1 <= page_size <= 5:
            raise MemoDisposition("Invalid preview page.")
        rows = self.proposal["items"][page * page_size:(page + 1) * page_size]
        output = []
        for row in rows:
            diff = row.get("diff") or {}
            position = self.proposal["items"].index(row) + 1
            action = row["action"].replace("_", " ")
            title = diff.get("title") or next((r["title"] for r in self.context["records"]
                                               if r["id"] == row["target_id"]), "")
            details = []
            names = {"t": "title", "due": "deadline", "time": "time", "date": "date", "s": "status",
                     "doneOn": "completion date", "timezone": "timezone", "end_time": "end time",
                     "notes_append": "notes", "priority": "priority", "effort_minutes": "effort in minutes",
                     "blocked_by": "blocked by", "start_after": "planned start", "related_task_id": "related task"}
            for field in diff.get("fields", []):
                path = field["path"]
                leaf = path[-1]
                if leaf not in names:
                    continue
                before = field["before"]["value"] if field["before"]["present"] else "unset"
                after = field["after"]["value"] if field["after"]["present"] else "unset"
                details.append(f"{names[leaf]}: {before} to {after}")
            if row["action"] == "append_note":
                item = next(i for i in self.draft["items"] if i["item_key"] == row["item_key"])
                details.append(f"{item['fields']['date']}: append {item['fields']['text']}")
            if not row["included"]:
                details.append(row["disposition"].replace("_", " "))
            output.append(f"Item {position}, {action}: {title}. " + "; ".join(details) + ".")
            if row.get("included"):
                self._reviewed.add(row["item_key"])
        counts = self.proposal["counts"]
        remainder = ""
        if page == 0:
            remainder = " ".join("Unresolved: " + q["reason"].replace("_", " ") + "."
                                 for q in self.proposal["unresolved"])
            remainder += " ".join("Unchanged: " + q["reason"].replace("_", " ") + "."
                                  for q in self.proposal["no_change"])
        return (f"PLACEHOLDER: {counts['selected']} selected; {counts['excluded']} excluded; "
                f"{counts['unresolved']} unresolved; {counts['no_change']} unchanged. "
                f"Provider {self.provider_identity['provider']}, model {self.provider_identity['model']}; "
                f"{len(self.selection.memos)} selected memos and bounded Cockpit candidates. "
                f"Selection timezone {self.selection.timezone}. " + " ".join(output) + " " + remainder)

    def stage_approval(self, *, listen=True):
        from samsara import execution_policy as policy
        if not self.client.can_confirm:
            raise MemoDisposition("Cockpit voice approval is not available yet; no changes applied.")
        needed = {row["item_key"] for row in self.proposal["items"] if row["included"]}
        if not needed:
            raise MemoDisposition("No resolved changes are selected.")
        if not needed <= self._reviewed:
            raise MemoDisposition("Read all selected changes before approval.")
        if not self._current():
            raise MemoDisposition("Preview is stale or extraction is disabled.")
        if self.operation is policy.pending_operation() and self.operation is not None:
            return self.operation
        inv = policy.Invocation("approve cockpit memo batch", deepcopy(self._binding),
                                policy.Route.EXACT, self.generation)
        def target_probe():
            if not self._current() or _hash(self.proposal) != self._preview_fingerprint:
                return {"gone": True}
            fresh = self.client.context()
            state = fresh["sources"]["state"]
            return dict(source_hash=self.selection.manifest_hash, state={k: state[k] for k in ("revision", "hash")})
        state = self.context["sources"]["state"]
        targets = dict(source_hash=self.selection.manifest_hash, state={k: state[k] for k in ("revision", "hash")})
        self.operation = policy.stage_bound_pending(self.app, inv,
            f"PLACEHOLDER: Apply {len(needed)} reviewed changes? "
            f"{self.proposal['counts']['excluded']} excluded and {self.proposal['counts']['unresolved']} unresolved remain.",
            targets=targets, target_probe=target_probe,
            current_invocation=lambda: policy.Invocation(inv.command_id, deepcopy(self._binding), inv.route, self.generation),
            reply_guard=lambda: self._current() and time.time() < self.proposal["expires_at"],
            on_approve=self._apply, on_reject=lambda _op: self.client.cancel())
        if listen:
            policy.listen_for_confirmation(self.app, self.operation.prompt)
        return self.operation

    def _apply(self, operation):
        try:
            auth = self.client.confirm(self._binding, operation)
            if not self._current():
                self.client.cancel()
                raise MemoDisposition("Cancelled before application.")
            self.application_uncertain = True
            self.last_result = self.client.apply(self._binding, auth["authorization"])
        except Exception:
            # Recover once; NEVER re-extract, re-confirm or repeat an apply.
            try:
                self.last_result = self.client.recover(self.request_id)
            except Exception:
                self.last_result = dict(status="unknown", request_id=self.request_id)
        receipt = self.last_result.get("receipt") or self.last_result
        if receipt.get("status") in {"applied", "undone"}:
            self.application_uncertain = False
        if receipt.get("status") == "applied":
            message = (f"PLACEHOLDER: Applied {receipt['counts']['selected']} changes. "
                       f"{receipt['counts']['excluded']} excluded and {receipt['counts']['unresolved']} unresolved remain.")
        else:
            message = "PLACEHOLDER: Application status is unknown. Recover this request before starting again."
        try:
            from plugins.commands.cockpit import announce
            announce(self.app, message)
        except Exception:
            pass  # a speech/display failure must not change a durable receipt
        return self.last_result

    def recover(self, request_id):
        self.last_result = self.client.recover(request_id)
        receipt = self.last_result.get("receipt") or self.last_result
        if receipt.get("status") in {"applied", "undone"}:
            self.application_uncertain = False
        return deepcopy(self.last_result)

    def cancel(self):
        from samsara import execution_policy as policy
        self._cancelled = True
        if self.operation is not None:
            policy._release_slot(self.operation)
            self.operation.cancel(self.app, "cancelled", chip=False)
            self.operation = None
        self.client.cancel()



def bounded_memo_signals(sources, records, *, now):
    """Pure adapter over explicitly supplied saved sources; never reads a vault.

    Only exact namespaced ID mentions qualify. Urgency requires a standalone
    `Urgent task:<id>.` directive, so quoted/negated/general topic text cannot
    assert it. Ambiguous titles and unknown timestamps yield no signal.
    """
    if len(sources) > 20 or sum(len(s.get('text', '')) for s in sources) > 50000:
        raise MemoDisposition('Memo signal scope exceeds twenty sources or 50000 characters.')
    known = {r['id'] for r in records}
    result = []
    for source in sources:
        if not source.get('has_transcript') or source.get('partial'):
            continue
        try:
            stamp = datetime.fromisoformat(source['captured_at'])
            if stamp.tzinfo is None or not 0 <= (now.date()-stamp.astimezone(now.tzinfo).date()).days <= 7:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        text = source['text']
        if hashlib.sha256(text.replace('\r\n', '\n').encode()).hexdigest() != source.get('text_sha256'):
            raise MemoDisposition('Memo signal source changed; select it again.')
        for ident in sorted(known):
            match = re.search(r'(?<![\w:])'+re.escape(ident)+r'(?![\w:-])', text)
            if not match:
                continue
            urgent = bool(re.search(r'(?m)^Urgent '+re.escape(ident)+r'[.!]?$', text))
            result.append(dict(target_id=ident, origin_id=source.get('upstream_id') or ('quick:' if source.get('source_kind')=='quick' else '')+source['memo_id'],
                captured_at=source['captured_at'], owner_urgent=urgent,
                evidence=dict(memo_id=source['memo_id'], text_sha256=source['text_sha256'], start=match.start(), end=match.end())))
    return result


class FollowupRunner(ProposalRunner):
    """Deterministic voice draft through the existing confirmed changes service.

    No extractor/provider needed. Production approval retains 975c's trusted
    native adapter gate; preview never claims an applied change.
    """
    def __init__(self, app, client):
        self._instruction_sources = []
        super().__init__(app, client, None, lambda: deepcopy(self._instruction_sources))

    def _current(self):
        from samsara import execution_policy as policy
        return (not self._cancelled and policy.is_fresh(self.app, self.generation)
                and self.selection.unchanged(self.source_reader()))

    def start_fields(self, target_id, fields, *, context=None, generation=None):
        from samsara import execution_policy as policy
        if self.application_uncertain:
            raise MemoDisposition('Recover the previous receipt before another change.')
        self.generation = policy.capture_generation(self.app) if generation is None else generation
        self._cancelled = False
        self._voice_page = 0
        self.context = deepcopy(context) if context is not None else self.client.context()
        row = next((r for r in self.context['records'] if r['id'] == target_id), None)
        if not row or row.get('kind') not in ('task', 'event') or not row.get('writable'):
            raise MemoDisposition('Ticket, goal and routine writes are unsupported in V1.')
        # Callers may submit only these explicitly voiced, bounded fields.
        allowed = {'start_after', 'due_date', 'status', 'priority'} if row['kind'] == 'task' else {'date', 'time'}
        if not fields or set(fields)-allowed:
            raise MemoDisposition('Unsupported follow-up fields.')
        self.request_id = uuid.uuid4().hex
        text = target_id + ' ' + ' '.join(k + ' ' + str(v) for k, v in sorted(fields.items()))
        source = normalize_memo_v1(dict(memo_id='instruction_'+self.request_id, source_kind='direct_instruction',
            source_locator='session', captured_at=self.context['captured_at'], capture_timezone=self.context.get('timezone'),
            text=text, has_transcript=True, partial=False, upstream_id=None))
        self._instruction_sources = [source]
        self.selection = Selection(json.dumps([source]), self.context.get('timezone') or self.context.get('utc_offset') or 'unknown', 'direct')
        self.provider_identity = dict(provider='local templates', model='none')
        self.draft = dict(schema_version=EXTRACTION_VERSION, items=[dict(item_key='followup',
            action='update_task' if row['kind'] == 'task' else 'update_event', target_id=target_id, fields=deepcopy(fields),
            evidence=[dict(memo_id=source['memo_id'], start=0, end=len(text))],
            match=dict(method='explicit_id', candidates=[dict(id=target_id, reason='Explicit owner target')], needs_resolution=False),
            time_resolution=None)], unresolved=[], no_change=[])
        validate_extraction_v1(self.draft, self.context, [source])
        return self.repreview()

    def start_undo(self, receipt_id):
        from samsara import execution_policy as policy
        if self.application_uncertain:
            raise MemoDisposition('Recover the previous receipt before undo.')
        self.generation = policy.capture_generation(self.app)
        self.context = self.client.context()
        original = self.client.recover(receipt_id).get('receipt')
        if not original or original.get('status') != 'applied':
            raise MemoDisposition('An applied receipt is required for undo.')
        self.request_id = uuid.uuid4().hex
        if not policy.is_fresh(self.app, self.generation):
            raise MemoDisposition('Undo cancelled before preview.')
        self._cancelled = False
        self._voice_page = 0
        self._instruction_sources = []
        self.selection = Selection('[]', self.context.get('timezone') or 'unknown', 'undo')
        self.provider_identity = dict(provider='local templates', model='none')
        proposal = self.client.undo_preview(receipt_id, self.request_id)
        state = self.context['sources']['state']
        if not self._current():
            raise MemoDisposition('Late undo preview discarded.')
        if (proposal.get('schema_version') != 'cockpit.changes.v1' or proposal.get('kind') != 'undo'
                or proposal.get('undo_of') != receipt_id or proposal.get('request_id') != self.request_id
                or proposal.get('base_hash') != state['hash'] or proposal.get('base_revision') != state['revision']
                or proposal.get('source_hash') != original.get('source_hash')
                or proposal.get('proposal_hash') != _hash({k:v for k,v in proposal.items() if k!='proposal_hash'})):
            raise MemoDisposition('Undo preview does not match the receipt and snapshot.')
        self.proposal = proposal
        self._preview_fingerprint = _hash(proposal)
        self._binding = {k:proposal[k] for k in ('proposal_id','proposal_hash','request_id')}
        self._binding.update(source_hash=self.selection.manifest_hash, context_hash=_hash(self.context), generation=self.generation)
        self._reviewed = set()
        return deepcopy(proposal)

    def read_page(self, page=0, page_size=3):
        if type(page) is not int or page < 0 or not 1 <= page_size <= 5:
            raise MemoDisposition('Invalid preview page.')
        output = []
        for row in self.proposal['items'][page*page_size:(page+1)*page_size]:
            diff = row.get('diff') or {}
            details = []
            for field in diff.get('fields', []):
                before = field['before']['value'] if field['before']['present'] else 'unset'
                after = field['after']['value'] if field['after']['present'] else 'unset'
                details.append('.'.join(field['path']) + ': ' + str(before) + ' to ' + str(after))
            if diff.get('collection') == 'notes':
                details.append('Restore the reviewed note addition')
            output.append(str(diff.get('title') or row['target_id']) + ': ' + '; '.join(details) + '.')
            if row.get('included'):
                self._reviewed.add(row['item_key'])
        return 'PLACEHOLDER: Preview only. ' + ' '.join(output)
