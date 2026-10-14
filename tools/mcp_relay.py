"""Claude Desktop stdio -> Samsara's local HTTP MCP service.

Standalone stdlib entry point: importing samsara would load GUI/app modules.
PyInstaller ships this console executable beside Samsara.exe, sharing _internal.
Only protocol replies go to stdout; diagnostics are fixed, body-free sentences.
"""
from __future__ import annotations

import argparse
import base64
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

DEFAULT_URL = 'http://127.0.0.1:47832/mcp'
MAX_BODY = 16384  # AgentServer's request budget.
MAX_RESPONSE = 256 * 1024
TIMEOUT = 40  # Includes the server's 30-second spoken typing approval window.
VERSIONS = {'2025-11-25', '2026-07-28'}
# STAND-IN COPY (570b): fixed diagnostics await the owner's wording review.


class RelayError(Exception):
    """Fixed safe diagnostics only; never include a caught exception."""


def endpoint(url):
    try:
        parsed = urlsplit(url)
        port = 80 if parsed.port is None else parsed.port
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or parsed.username is not None or parsed.password is not None
                or parsed.path != '/mcp' or parsed.query or parsed.fragment
                or not 1 <= port <= 65535):
            raise ValueError
    except (ValueError, TypeError):
        raise RelayError('Samsara relay accepts only http://127.0.0.1:<port>/mcp.') from None
    return port


def read_token():
    # Matches samsara.paths.samsara_home_dir; reads only, never repairs pairing.
    home = Path(os.environ['SAMSARA_HOME_DIR']) if os.environ.get('SAMSARA_HOME_DIR') else Path.home() / '.samsara'
    try:
        saved = json.loads((home / 'agent_server' / 'token.json').read_text(encoding='utf-8'))
        token = saved.get('token')
        if (saved.get('version') != 1 or not isinstance(token, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{40,128}', token)):
            raise ValueError
        return token
    except (OSError, ValueError, AttributeError, RecursionError):
        raise RelayError('Samsara token is unavailable. Turn on Let AI apps use Samsara first.') from None


class Relay:
    def __init__(self, url, token):
        self.port = endpoint(url)
        self.token = token
        self.session = None
        self.version = None

    def forward(self, request, raw):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
                   'Accept': 'application/json'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        params = request.get('params', {})
        meta = params.get('_meta', {}) if isinstance(params, dict) else {}
        version = meta.get('io.modelcontextprotocol/protocolVersion') if isinstance(meta, dict) else None
        version = version or self.version
        if version in VERSIONS:
            headers['MCP-Protocol-Version'] = version
        if version == '2026-07-28':
            headers['Mcp-Method'] = request['method']
            if request['method'] == 'tools/call' and isinstance(params.get('name'), str):
                name = base64.b64encode(params['name'].encode('utf-8')).decode('ascii')
                headers['Mcp-Name'] = '=?base64?' + name + '?='
        connection = HTTPConnection('127.0.0.1', self.port, timeout=TIMEOUT)
        try:
            connection.request('POST', '/mcp', raw, headers)
            response = connection.getresponse()
            if response.status == 401:
                raise RelayError('Samsara token was rejected. Restart this connection after changing the token.')
            if 300 <= response.status < 400:
                raise RelayError('Samsara relay refused a redirect.')
            data = response.read(MAX_RESPONSE + 1)
            if response.status == 202 and not data:
                return None
            if len(data) > MAX_RESPONSE:
                raise RelayError('Samsara returned an oversized reply.')
            try:
                reply = json.loads(data)
                if not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0':
                    raise ValueError
            except (ValueError, UnicodeError, RecursionError):
                raise RelayError('Samsara did not return a valid MCP reply.') from None
            if request['method'] == 'initialize' and isinstance(reply.get('result'), dict):
                negotiated = reply['result'].get('protocolVersion')
                if negotiated in VERSIONS:
                    self.version = negotiated
                session = response.getheader('Mcp-Session-Id')
                if session and re.fullmatch(r'[A-Za-z0-9_-]{20,128}', session):
                    self.session = session
            return reply
        except (OSError, HTTPException):
            # Never retry a request: speech/typing may already have happened.
            raise RelayError('Samsara is not running or did not reply. Open Samsara and turn on Let AI apps use Samsara.') from None
        finally:
            connection.close()


def serve(relay, incoming, outgoing):
    while True:
        raw = incoming.readline(MAX_BODY + 2)
        if not raw:
            return 0
        if not raw.strip():
            continue
        raw = raw.rstrip(b'\r\n')
        if len(raw) > MAX_BODY:
            raise RelayError('Samsara relay refused an oversized MCP message.')
        try:
            request = json.loads(raw)
            if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                    or not isinstance(request.get('method'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_./-]{1,128}', request['method'])):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise RelayError('Samsara relay received an invalid MCP message.') from None
        reply = relay.forward(request, raw)
        if reply is not None:
            outgoing.write(json.dumps(reply, ensure_ascii=True, separators=(',', ':')).encode('utf-8') + b'\n')
            outgoing.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    options = parser.parse_args(argv)
    try:
        endpoint(options.url)  # Refuse foreign targets before reading any token.
        relay = Relay(options.url, read_token())
        return serve(relay, sys.stdin.buffer, sys.stdout.buffer)
    except RelayError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError, RecursionError):
        print('Samsara relay could not complete the connection.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
