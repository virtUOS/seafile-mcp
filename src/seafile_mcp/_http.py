"""Shared httpx client.

Two settings here are load-bearing security properties, not tuning knobs:

``follow_redirects=False``
    Seafile hands us upload and download URLs. If we followed redirects with the
    user's token attached, a redirect to an attacker-controlled host would leak a
    live credential. Callers must inspect 3xx responses explicitly.

host pinning (:func:`assert_same_host`)
    Every URL we receive from Seafile is checked against the configured upstream
    before we issue a request to it, for the same reason.
"""

from __future__ import annotations

import httpx

from .config import get_settings
from .models import SeafileMCPError

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            timeout=settings.request_timeout_s,
            follow_redirects=False,
            headers={"User-Agent": "seafile-mcp"},
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _host_of(url: str) -> tuple[str, int | None]:
    parsed = httpx.URL(url)
    return parsed.host, parsed.port


def assert_same_host(url: str) -> str:
    """Refuse any URL that does not live on the configured Seafile host.

    Seafile normally returns links on its own host; a link pointing elsewhere
    means either a misconfiguration or an attempt to capture the token we would
    otherwise attach.
    """
    settings = get_settings()
    want_host, want_port = _host_of(settings.server_url)
    got_host, got_port = _host_of(url)
    if got_host != want_host or got_port != want_port:
        raise SeafileMCPError(
            f"Refusing to follow a link to {got_host!r}: it is not the configured "
            f"Seafile host ({want_host!r}). No credentials were sent."
        )
    return url
