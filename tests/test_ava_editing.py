import json

import pytest

from samsara.ava_editing import (
    EditProposalError,
    EditProposalStore,
    StaleEditRequest,
    parse_rewrite_response,
)


def _response(replacement):
    return json.dumps({"replacement": replacement})


def test_parser_normalizes_nfc_and_newlines_without_trimming():
    replacement = parse_rewrite_response(
        _response("  Cafe\u0301\r\nnext  "), source="Original",
    )
    assert replacement == "  Caf\u00e9\nnext  "


@pytest.mark.parametrize("raw", [
    "rewritten text",
    "```json\n{\"replacement\": \"new\"}\n```",
    "[]",
    '{"replacement": "new", "explanation": "because"}',
    '{"replacement": 7}',
])
def test_parser_rejects_non_strict_response_shapes(raw):
    with pytest.raises(EditProposalError):
        parse_rewrite_response(raw, source="Original")


@pytest.mark.parametrize("replacement", [
    "   ",
    "Original",
    "safe<|assistant|>unsafe",
    "safe\u202Eunsafe",
    "safe\x00unsafe",
])
def test_parser_rejects_unsafe_or_non_editing_output(replacement):
    with pytest.raises(EditProposalError):
        parse_rewrite_response(_response(replacement), source="Original")


def test_parser_rejects_unbounded_expansion():
    with pytest.raises(EditProposalError, match="too long"):
        parse_rewrite_response(_response("x" * 1005), source="a")



def test_store_rejects_missing_or_non_text_inputs():
    store = EditProposalStore()
    with pytest.raises(EditProposalError, match="no pending text"):
        store.begin(source=None, instruction="shorten")
    with pytest.raises(EditProposalError, match="instruction is empty"):
        store.begin(source="original", instruction=None)


def test_latest_request_wins_and_old_worker_cannot_publish():
    now = [10.0]
    store = EditProposalStore(clock=lambda: now[0])
    old = store.begin(source="first", instruction="shorten")
    new = store.begin(source="second", instruction="clarify")

    with pytest.raises(StaleEditRequest, match="superseded"):
        store.complete(old, _response("old result"))

    proposal = store.complete(new, _response("new result"))
    assert store.peek() == proposal
    assert proposal.source == "second"
    assert proposal.replacement == "new result"


def test_request_expiry_prevents_publish():
    now = [10.0]
    store = EditProposalStore(ttl_s=5, clock=lambda: now[0])
    request = store.begin(source="original", instruction="shorten")
    now[0] = 15.0

    with pytest.raises(StaleEditRequest, match="expired"):
        store.complete(request, _response("short"))
    assert store.peek() is None


def test_proposal_expiry_clears_peek():
    now = [10.0]
    store = EditProposalStore(ttl_s=5, clock=lambda: now[0])
    request = store.begin(source="original", instruction="shorten")
    store.complete(request, _response("short"))
    now[0] = 15.0
    assert store.peek() is None


def test_invalid_current_response_clears_request():
    store = EditProposalStore()
    request = store.begin(source="original", instruction="shorten")
    with pytest.raises(EditProposalError):
        store.complete(request, "not json")
    with pytest.raises(StaleEditRequest):
        store.complete(request, _response("late"))


def test_discard_invalidates_request_and_proposal():
    store = EditProposalStore()
    request = store.begin(source="original", instruction="shorten")
    store.discard()
    with pytest.raises(StaleEditRequest):
        store.complete(request, _response("short"))
    assert store.peek() is None
