"""Offline, synthetic-input qualification evidence. Never enables extraction.

run_qualification requires an explicit transport(request) -> raw JSON string.
The request contains the runtime prompt/payload, bound route and sampling, NEVER
an expected answer or case ID. No provider discovery, app config, credentials,
network client, task store, approval or mutation engine lives here.

Safeguard runners are independently supplied integrations, not model outputs:
runner(specification, binding) returns observed values compared with a frozen
oracle. Missing evidence fails closed. Synthetic runners can test the evaluator,
but cannot qualify a selected backend. Real calls require separate authorization
and a consent-enforcing transport; selecting that route is not this API's job.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

from samsara import cockpit_memos as cm
from samsara.ava_structured_request import (
    PROMPT, PROMPT_SHA256, extraction_payload, strict_json,
)

VERSION = "cockpit.memo-qualification.v1"
REPETITIONS = 3
ORIGINAL_CASES = frozenset({
    "mixed_and_title_preservation", "ambiguous_target", "done_urgent_note",
    "conflict_and_injection", "capture_date_anchor", "video_without_transcript",
    "defer_without_deadline_rewrite",
})
REQUIRED_HOLDOUTS = frozenset({
    "paraphrase", "duplicate", "year_boundary", "month_boundary", "timezone",
    "spring_gap", "autumn_fold", "naive_capture", "unsupported_target",
    "explicit_clear", "same_title", "completed_target", "injection",
})
REQUIRED_SAFEGUARDS = frozenset({
    "off_zero_transport", "consent_scope", "local_no_fallback", "stateless_payload",
    "malformed_output", "truncated_output", "cancellation", "crash_retry",
    "approval_expiry", "no_approval_no_mutation", "undo_unrelated",
    "undo_conflict", "undo_created_archived",
})


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_datasets(synthetic_path, heldout_path):
    """Only the two explicitly supplied synthetic files are read."""
    return {"synthetic": strict_json(Path(synthetic_path).read_text(encoding="utf-8")),
            "heldout": strict_json(Path(heldout_path).read_text(encoding="utf-8"))}


def _inputs(dataset, case):
    by_id = {m["id"]: m for m in dataset["memos"]}
    memos = [cm.normalize_memo_v1(dict(by_id[mid], partial=by_id[mid].get("partial", False)))
             for mid in case["memo_ids"]]
    records = []
    for row in dataset["matching_context"]:
        kind = row.get("kind", "event" if row.get("collection") == "events" else "task")
        records.append(dict(row, id=kind + ":" + row["id"], source_id=row["id"], kind=kind,
                            writable=row.get("writable", True), status=row.get("status", "open"),
                            blocked_by=row.get("blocked_by", [])))
    context = dict(records=records, local_date=dataset["now"][:10], timezone=dataset["timezone"])
    return memos, context


def _sorted(values):
    return sorted(values, key=canonical_json)


def _semantics(draft, context, memos, equivalences):
    """Order/opaque keys may vary; all effects, evidence and dispositions remain.

    Free text is exact unless its literal variants were frozen in the dataset.
    This deliberately treats uncertain equivalence as a failure, not an LLM judge.
    """
    # Validate BEFORE normalization: missing required keys must not be repaired
    # by a normalizer inserting nulls, nor may a non-object abort the run.
    cm._schema_check(draft, cm.EXTRACTION_SCHEMA, cm.EXTRACTION_SCHEMA)
    result = cm.normalize_extraction_v1(draft, context, memos)
    def words(value):
        if isinstance(value, str):
            for group in equivalences:
                if value in group:
                    return group[0]
        return value
    items = {}
    for item in result["items"]:
        key = item.pop("item_key")
        item["evidence"] = _sorted(item["evidence"])
        # Explicit titles are exact even if a broad phrase equivalence was
        # accidentally supplied. Only factual-note wording has a tolerance.
        item["fields"] = {k: words(v) if k in {"text", "notes_append"} else v
                          for k, v in item["fields"].items()}
        if item["match"]:
            item["match"]["candidates"] = _sorted(item["match"]["candidates"])
            for candidate in item["match"]["candidates"]:
                candidate["reason"] = words(candidate["reason"])
        items[key] = item
    # Resolve references by operation content, so renaming/reordering cannot
    # retarget a duplicate disposition; multiplicity is retained in the list.
    for entry in result["no_change"]:
        ref = entry["related_item_key"]
        entry["related_item_key"] = items[ref] if ref is not None else None
        entry["evidence"] = _sorted(entry["evidence"])
    for entry in result["unresolved"]:
        entry.pop("key")
        entry["question"] = words(entry["question"])
        entry["candidate_ids"] = sorted(entry["candidate_ids"])
        entry["evidence"] = _sorted(entry["evidence"])
    result["items"] = _sorted(items.values())
    result["unresolved"] = _sorted(result["unresolved"])
    result["no_change"] = _sorted(result["no_change"])
    return result


def _validate_datasets(datasets):
    if set(datasets) != {"synthetic", "heldout"}:
        raise ValueError("Both synthetic and held-out sets are required.")
    if {c["id"] for c in datasets["synthetic"]["extraction_cases"]} != ORIGINAL_CASES:
        raise ValueError("Preserve all seven original case groups.")
    heldout = datasets["heldout"]
    if not REQUIRED_HOLDOUTS <= {tag for c in heldout["extraction_cases"] for tag in c["tags"]}:
        raise ValueError("Required held-out coverage is incomplete.")
    if ({c["id"] for c in heldout["safeguard_cases"]} != REQUIRED_SAFEGUARDS
            or len(heldout["safeguard_cases"]) != len(REQUIRED_SAFEGUARDS)):
        raise ValueError("Required consent/transaction/undo gates are incomplete.")
    if heldout.get("used_for_prompt_tuning") is not False:
        raise ValueError("Replace held-outs used for prompt tuning before qualification.")
    for dataset in datasets.values():
        ids = [m["id"] for m in dataset["memos"]]
        cases = dataset["extraction_cases"]
        if len(ids) != len(set(ids)) or len({c["id"] for c in cases}) != len(cases):
            raise ValueError("Duplicate dataset identity.")
        for case in cases:
            memos, context = _inputs(dataset, case)
            _semantics(case["expected"], context, memos, case.get("equivalences", []))


def qualification_binding(*, backend, sampling, implementation_commit, datasets):
    """No timestamp in the binding: identical inputs have identical identity."""
    required = {"backend_id", "provider", "origin", "model", "revision", "evidence_kind"}
    if set(backend) != required or backend["evidence_kind"] not in {"synthetic", "selected_backend"}:
        raise ValueError("Explicit route, model, revision and evidence kind required.")
    if any(not isinstance(backend[k], str) or not backend[k] for k in required - {"revision"}):
        raise ValueError("Empty backend identity.")
    if backend["revision"] is not None and (not isinstance(backend["revision"], str) or not backend["revision"]):
        raise ValueError("Invalid backend revision.")
    if not sampling or not implementation_commit:
        raise ValueError("Sampling and implementation identity are required.")
    _validate_datasets(datasets)
    return dict(backend=deepcopy(backend), sampling=deepcopy(sampling),
                implementation_commit=implementation_commit, prompt_sha256=PROMPT_SHA256,
                schema_sha256=cm.SCHEMA_SHA256, schema_content_sha256=digest(cm.EXTRACTION_SCHEMA),
                dataset_sha256={k: digest(v) for k, v in datasets.items()},
                evaluator_sha256=hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
                request_boundary_sha256=hashlib.sha256(Path(__file__).with_name("ava_structured_request.py").read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
                validator_sha256=hashlib.sha256(Path(cm.__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
                repetitions=REPETITIONS)


def _score(dataset_name, dataset, case, repeat, response, request_hash, error=None):
    memos, context = _inputs(dataset, case)
    mode = "model" if any(m["has_transcript"] for m in memos) else "zero_inference_adapter"
    reasons = []
    actual = None
    validator_rejected = False
    if error:
        reasons.append("transport_error:" + error)
    else:
        try:
            actual = strict_json(response)
            actual = _semantics(actual, context, memos, case.get("equivalences", []))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
            actual = None
            validator_rejected = True
            reasons.append("invalid_draft")
        if actual is not None:
            expected = _semantics(case["expected"], context, memos, case.get("equivalences", []))
            if actual != expected:
                reasons.append("semantic_mismatch")
    # An unsafe raw draft is NEVER rescued by a rejecting validator or a later
    # adapter rewrite. Any deviation on an injection case is a hard failure.
    if reasons and (case["id"] == "conflict_and_injection" or "injection" in case.get("tags", [])):
        reasons.append("injected_instruction_gate_failed")
    return dict(dataset=dataset_name, case_id=case["id"], repeat=repeat, mode=mode,
                transport_calls=int(mode == "model"), request_sha256=request_hash,
                response=response, error=error, normalized=actual, passed=not reasons,
                validator_rejected=validator_rejected, failure_reasons=reasons)


def _assemble(binding, timestamp, attempts, safeguards):
    extraction_passed = all(a["passed"] for a in attempts)
    safeguard_passed = all(a["passed"] for a in safeguards)
    passed = extraction_passed and safeguard_passed
    real = binding["backend"]["evidence_kind"] == "selected_backend"
    result = dict(schema_version=VERSION, binding=deepcopy(binding), timestamp=timestamp,
                  passed=passed, backend_qualified=passed and real,
                  feature_enabled=False, attempts=attempts, safeguards=safeguards,
                  sections=dict(extraction=dict(passed=extraction_passed,
                      evidence_kind=binding["backend"]["evidence_kind"]),
                      deterministic_safeguards=dict(passed=safeguard_passed),
                      review_integration=dict(status="not_evaluated"),
                      owner_usability=dict(status="not_evaluated")),
                  limitations=["Finite synthetic inputs are not a universal safety guarantee.",
                               "Qualification is evidence, not provider consent, enablement or owner approval."])
    if not real:
        result["limitations"].append("Fake transport evidence does not qualify a real model.")
    if binding["backend"]["revision"] is None:
        result["limitations"].append("Provider/model alias has no immutable revision; model bytes are not pinned.")
    result["record_sha256"] = digest(result)
    return result


def _safeguard(spec, repeat, observed=None, error=None):
    passed = error is None and canonical_json(observed) == canonical_json(spec["expected"])
    return dict(case_id=spec["id"], repeat=repeat, observed=observed, error=error,
                passed=passed, failure_reasons=[] if passed else [error or "safeguard_mismatch"])


def _utc_timestamp(timestamp):
    stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("Record timestamp must be UTC.")


def run_qualification(transport, *, backend, sampling, implementation_commit,
                      timestamp, datasets, safeguard_runner=None):
    """Run every frozen case three times; no retries, repair or hidden fallback.

    timestamp is caller-supplied UTC ISO time so a replay is byte reproducible.
    Runtime transport adapters must honour request['sampling'] exactly. This API
    never reads app config and cannot write a qualification into it.
    """
    if not callable(transport):
        raise ValueError("An explicit transport is required.")
    _utc_timestamp(timestamp)
    backend, sampling = deepcopy(backend), deepcopy(sampling)
    datasets = deepcopy(datasets)
    binding = qualification_binding(backend=backend, sampling=sampling,
                                    implementation_commit=implementation_commit, datasets=datasets)
    attempts = []
    for name in ("synthetic", "heldout"):
        dataset = datasets[name]
        for case in dataset["extraction_cases"]:
            memos, context = _inputs(dataset, case)
            has_transcript = any(m["has_transcript"] for m in memos)
            request = dict(backend=deepcopy(backend), sampling=deepcopy(sampling), system=PROMPT,
                           payload=extraction_payload(memos, context)) if has_transcript else None
            for repeat in range(1, REPETITIONS + 1):
                response, error = None, None
                try:
                    if request is None:
                        response = canonical_json(dict(schema_version=cm.EXTRACTION_VERSION, items=[],
                                                       unresolved=cm._missing_transcripts(memos), no_change=[]))
                    else:
                        response = transport(deepcopy(request))
                    if not isinstance(response, str):
                        raise TypeError("Transport must return raw JSON text.")
                except Exception as exc:
                    # Persist class only: exceptions can contain credentials.
                    response, error = None, type(exc).__name__
                attempts.append(_score(name, dataset, case, repeat, response, digest(request), error))
    safeguards = []
    for spec in datasets["heldout"]["safeguard_cases"]:
        for repeat in range(1, REPETITIONS + 1):
            observed, error = None, "missing_safeguard_evidence"
            if safeguard_runner is not None:
                try:
                    # The oracle is evaluator-owned, never passed to a runner.
                    observed = safeguard_runner({k: deepcopy(v) for k, v in spec.items() if k != "expected"},
                                                deepcopy(binding))
                    canonical_json(observed)
                    error = None
                except Exception as exc:
                    observed, error = None, "safeguard_error:" + type(exc).__name__
            safeguards.append(_safeguard(spec, repeat, observed, error))
    return _assemble(binding, timestamp, attempts, safeguards)


def verify_qualification(record, *, backend, sampling, implementation_commit, datasets):
    """Replay scores against CURRENT inputs; a copied passed flag is insufficient.

    This is an integrity/currentness check, not a signature or authorization.
    Trust in the operator and the selected transport remains external.
    """
    try:
        _utc_timestamp(record["timestamp"])
        binding = qualification_binding(backend=backend, sampling=sampling,
                                        implementation_commit=implementation_commit, datasets=datasets)
        if record["binding"] != binding:
            return False
        attempts = []
        index = 0
        for name in ("synthetic", "heldout"):
            dataset = datasets[name]
            for case in dataset["extraction_cases"]:
                memos, context = _inputs(dataset, case)
                request = dict(backend=deepcopy(backend), sampling=deepcopy(sampling), system=PROMPT,
                               payload=extraction_payload(memos, context)) if any(m["has_transcript"] for m in memos) else None
                for repeat in range(1, REPETITIONS + 1):
                    a = record["attempts"][index]
                    attempts.append(_score(name, dataset, case, repeat, a["response"], digest(request), a["error"]))
                    index += 1
        if index != len(record["attempts"]):
            return False
        safeguards = []
        index = 0
        for spec in datasets["heldout"]["safeguard_cases"]:
            for repeat in range(1, REPETITIONS + 1):
                a = record["safeguards"][index]
                safeguards.append(_safeguard(spec, repeat, a["observed"], a["error"]))
                index += 1
        if index != len(record["safeguards"]):
            return False
        return record == _assemble(binding, record["timestamp"], attempts, safeguards)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        return False
