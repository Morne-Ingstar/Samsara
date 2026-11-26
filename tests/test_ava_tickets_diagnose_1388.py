"""T10 diagnostic contract with synthetic HTTP responses only."""
from http.client import RemoteDisconnected
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from samsara import ava_tickets
from tools.build_receipts import installed_probe


PRIVATE_FIELDS = ('title', 'left_off', 'next', 'waiting_on', 'updated')
PRIVATE_TEXT = {name: f'private-synthetic-{name}' for name in PRIVATE_FIELDS}
PRIVATE_ERROR = 'private-synthetic-exception'


def response_document(*rows, rev=1):
    return json.dumps({'rev': rev, 'tickets': rows}).encode()


@pytest.fixture
def http(monkeypatch):
    def install(payload=b'', *, status=200, content_type='application/json',
                request_error=None, header_error=None, body_error=None):
        body = BytesIO(payload)
        calls = []

        def read1(size):
            data = body.read(size)
            if not data and body_error:
                raise body_error
            return data

        def getresponse():
            if header_error:
                raise header_error
            return SimpleNamespace(status=status, getheader=lambda *_: content_type, read1=read1)

        def request(method, route, *, headers):
            calls.append((method, route, headers))
            if request_error:
                raise request_error

        def connect(host, port, *, timeout):
            assert (host, port, timeout) == ('127.0.0.1', 8790, 1.0)
            return SimpleNamespace(request=request, getresponse=getresponse, sock=None,
                                   close=lambda: calls.append('closed'))

        monkeypatch.setattr(ava_tickets, 'HTTPConnection', connect)
        return calls

    return install


def assert_diagnostic(value, reason, detail, *, status=None, size=None, count=None):
    serialized = json.dumps(value)
    if any(private in serialized for private in (*PRIVATE_TEXT.values(), PRIVATE_ERROR, 'T10', 'T299')):
        pytest.fail('The diagnostic contains private input.', pytrace=False)
    assert set(value) == {'reason', 'detail', 'http_status', 'elapsed_ms', 'body_bytes', 'ticket_count'}
    assert (value['reason'], value['detail']) == (reason, detail)
    for key, expected in (('http_status', status), ('body_bytes', size), ('ticket_count', count)):
        assert value[key] == expected
        assert value[key] is None or type(value[key]) is int
    assert type(value['elapsed_ms']) is int and value['elapsed_ms'] >= 0


@pytest.mark.parametrize('ticket_id', ['', None, 10, 'T0', 'T299 plus another ticket'])
def test_invalid_id_uses_no_connection(monkeypatch, ticket_id):
    monkeypatch.setattr(ava_tickets, 'HTTPConnection', lambda *a, **kw: pytest.fail('unexpected HTTP'))
    assert_diagnostic(ava_tickets.diagnose(ticket_id), 'invalid_id', 'bad_request_id')


@pytest.mark.parametrize('phase,error,reason,detail,status,size', [
    ('request', ConnectionRefusedError, 'api_unavailable', 'connection_refused', None, None),
    ('request', OSError, 'api_unavailable', 'os_error', None, None),
    ('header', RemoteDisconnected, 'api_unavailable', 'http_protocol_error', None, None),
    ('header', TimeoutError, 'timeout', 'response_headers_timeout', None, None),
    ('body', TimeoutError, 'timeout', 'response_body_timeout', 200, 1),
])
def test_transport_classification(http, phase, error, reason, detail, status, size):
    # The body timeout occurs after one byte arrives, so the count reflects bytes read.
    calls = http(b'{', **{f'{phase}_error': error(PRIVATE_ERROR)})
    assert_diagnostic(ava_tickets.diagnose('T10'), reason, detail, status=status, size=size)
    assert calls == [('GET', '/api/tickets', {'Accept': 'application/json'}), 'closed']


@pytest.mark.parametrize('status,reported', [(302, 302), (403, 403), (404, 404), (500, 500), (999, None)])
def test_non_200_does_not_read_body(http, status, reported):
    http(status=status, body_error=AssertionError('unexpected body read'))
    assert_diagnostic(ava_tickets.diagnose('T10'), 'http_status', 'non_200', status=reported)


def test_unexpected_content_type_does_not_read_body(http):
    http(content_type=f'text/html; {PRIVATE_ERROR}', body_error=AssertionError('unexpected body read'))
    assert_diagnostic(ava_tickets.diagnose('T10'), 'schema_error', 'unexpected_content_type', status=200)


@pytest.mark.parametrize('payload,reason,detail,count', [
    pytest.param(b'{bad', 'schema_error', 'invalid_json', None, id='malformed-json'),
    pytest.param(b'\xff', 'schema_error', 'invalid_json', None, id='non-utf8'),
    pytest.param(b'[' * 1200 + b']' * 1200, 'schema_error', 'invalid_json', None, id='deep-json'),
    pytest.param(b'1' * 5000, 'schema_error', 'invalid_json', None, id='oversized-integer'),
    pytest.param(b'[]', 'schema_error', 'invalid_envelope', None, id='invalid-envelope'),
    pytest.param(response_document({'id': 'T10'}, rev=False),
                 'schema_error', 'invalid_envelope', 1, id='bad-revision'),
    pytest.param(response_document(), 'missing_id', 'empty_store', 0, id='empty-store'),
    pytest.param(response_document({'id': 'T299'}), 'missing_id', 'id_not_found', 1, id='absent-id'),
    pytest.param(response_document({'id': 'T10'}, {'id': 'T0'}),
                 'schema_error', 'invalid_ticket_identifiers', 2, id='invalid-unrelated-id'),
    pytest.param(response_document({'id': 'T10'}, {'id': 'T299'}, {'id': 'T299'}),
                 'schema_error', 'duplicate_ticket_identifiers', 3, id='duplicate-unrelated-id'),
    pytest.param(response_document({'id': 'T10', 'status': PRIVATE_ERROR}),
                 'schema_error', 'invalid_ticket_status', 1, id='bad-status'),
    pytest.param(response_document({'id': 'T10', 'title': [PRIVATE_ERROR]}),
                 'schema_error', 'invalid_ticket_field', 1, id='bad-text-field'),
    pytest.param(response_document({'id': 'T10', 'updated': [PRIVATE_ERROR]}),
                 'schema_error', 'invalid_updated_field', 1, id='bad-updated-field'),
    pytest.param(response_document({'id': 'T10', **PRIVATE_TEXT}), 'ok', 'ok', 1, id='valid-ticket'),
])
def test_body_classification_and_privacy(http, payload, reason, detail, count):
    http(payload, content_type='Application/JSON; charset=utf-8')
    assert_diagnostic(ava_tickets.diagnose('T10'), reason, detail,
                      status=200, size=len(payload), count=count)


def test_body_read_stops_at_limit_plus_one(http, monkeypatch):
    monkeypatch.setattr(ava_tickets, '_MAX_BYTES', 4)
    http(b'[]' * 8)
    assert_diagnostic(ava_tickets.diagnose('T10'), 'schema_error', 'response_too_large', status=200, size=5)


def test_body_deadline_reports_bytes_read_and_elapsed_time(http, monkeypatch):
    http(b'{')
    timestamps = iter((10, 10, 10.25, 11, 11.125))
    monkeypatch.setattr(ava_tickets, 'time', SimpleNamespace(monotonic=timestamps.__next__))

    value = ava_tickets.diagnose('T10')

    assert_diagnostic(value, 'timeout', 'response_body_timeout', status=200, size=1)
    assert value['elapsed_ms'] == 1125


@pytest.mark.parametrize('status,text,reason,answer_status', [
    (200, "Cockpit returned invalid ticket data. I couldn't read that ticket.", 'schema_error', None),
    (999, "I couldn't read Cockpit's tickets. Its local API returned an error.", 'http_status', 999),
])
def test_lookup_keeps_legacy_answer(http, status, text, reason, answer_status):
    http(b'{', status=status)
    assert ava_tickets.lookup('T10') == ava_tickets.Answer(text, 'error', reason, answer_status)


def test_installed_diagnostic_is_fixed_to_t10_and_separate_from_lookup(http):
    payload = response_document({'id': 'T10', **PRIVATE_TEXT}, {'id': 'T299'})
    calls = http(payload)
    value = installed_probe.perform(None, None, 'ticket_lookup_diagnostic')
    assert_diagnostic(value, 'ok', 'ok', status=200, size=len(payload), count=2)
    assert calls == [('GET', '/api/tickets', {'Accept': 'application/json'}), 'closed']

    http(payload)
    assert installed_probe.perform(None, None, 'ticket_lookup') == {
        'lookup_success': True, 'has_id': True, 'has_checkpoint': True,
    }
