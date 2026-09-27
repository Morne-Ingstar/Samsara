"""Private AI HTTP requests with a fixed destination and no redirect replay."""
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import requests


class AITransportRefused(requests.exceptions.ConnectionError):
    """A destination or redirect was refused before private data could leave."""


def _parse(url, label):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or any(char.isspace() for char in url)
                or "\\" in url or "%" in parsed.netloc or parsed.port == 0):
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise AITransportRefused(f"{label} AI service address refused") from None


def _local_url(url, ollama_host=None):
    parsed = _parse(url, "local")
    host = parsed.hostname.lower()
    if host == "localhost":
        address = ipaddress.ip_address("127.0.0.1")
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            configured = _parse(ollama_host, "local") if ollama_host else None
            if configured is None or host != configured.hostname.lower():
                raise AITransportRefused("local AI service address refused") from None
            try:
                addresses = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(
                    host, parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM)}
            except (OSError, ValueError):
                raise AITransportRefused("local AI service address refused") from None
            if not addresses or not all(candidate.is_loopback for candidate in addresses):
                raise AITransportRefused("local AI service address refused")
            address = sorted(addresses, key=lambda candidate: candidate.version)[0]
    if not address.is_loopback:
        raise AITransportRefused("local AI service address refused")
    authority = f"[{address}]" if address.version == 6 else str(address)
    if parsed.port is not None:
        authority += f":{parsed.port}"
    return urlunsplit(parsed._replace(netloc=authority))


def _cloud_url(url):
    from samsara.cloud_llm import BUILTIN_PROVIDERS

    parsed = _parse(url, "cloud")
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.hostname.lower() not in {provider["host"] for provider in BUILTIN_PROVIDERS.values()}):
        raise AITransportRefused("cloud AI provider address refused")
    return url


def _request(method, url, *, label, timeout, json=None, headers=None, request_fn=None):
    options = {"timeout": timeout, "allow_redirects": False}
    if headers is not None:
        options["headers"] = headers
    if method == "POST":
        options["json"] = json
    with requests.Session() as session:
        session.trust_env = False
        response = (request_fn(url, **options) if request_fn is not None
                    else session.request(method, url, **options))
    if 300 <= response.status_code < 400:
        response.close()
        raise AITransportRefused(f"{label} AI service tried to redirect - refused")
    return response


def post_local(url, json, timeout, *, ollama_host=None):
    return _request("POST", _local_url(url, ollama_host), label="local", json=json, timeout=timeout)


def get_local(url, timeout, *, ollama_host=None, request_fn=None):
    return _request("GET", _local_url(url, ollama_host), label="local", timeout=timeout,
                    request_fn=request_fn)


def post_cloud(url, json, timeout, *, headers=None, request_fn=None):
    return _request("POST", _cloud_url(url), label="cloud", json=json, timeout=timeout,
                    headers=headers, request_fn=request_fn)


def get_cloud(url, timeout, *, headers=None, request_fn=None):
    return _request("GET", _cloud_url(url), label="cloud", timeout=timeout,
                    headers=headers, request_fn=request_fn)


class LocalClient:
    """Compatibility facade for the shared corrections/editing HTTP seam."""

    post = staticmethod(post_local)
    get = staticmethod(get_local)


def notify_refusal(app, error):
    message = str(error)
    try:
        show = getattr(app, "_show_outcome_chip", None)
        if callable(show):
            show(message, "error")
    except Exception:
        pass
    try:
        from plugins.commands.ask_ollama import speak

        speak(app, message, category="confirmation")
    except Exception:
        pass
