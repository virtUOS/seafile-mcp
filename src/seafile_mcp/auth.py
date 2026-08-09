"""Credential resolution.

The user's Seafile token *is* the API key. There is no separate MCP account and
nothing is stored: each request carries its own credential and the server keeps
no per-user state, so there is no cache from which one user's token could ever be
served to another.

Clients disagree about how to send a credential, and we do not control them, so
resolution is deliberately liberal:

1. ``Authorization`` header (``Bearer x``, ``Token x``, or a bare token)
2. ``X-Seafile-Token`` header, for clients that reserve or rewrite ``Authorization``
3. ``SEAFILE_API_TOKEN`` env var, used by the single-user stdio transport
"""

from __future__ import annotations

import hashlib
import logging
import time

from fastmcp.server.dependencies import get_http_headers

from ._http import get_http_client
from .config import get_settings
from .models import AuthError, Credentials, TokenMode

logger = logging.getLogger(__name__)

_SCHEMES = ("bearer", "token")

#: sha256(token) -> (mode, expires_at). Holds no secret and no user data, so it
#: cannot leak anything across users; it exists purely to avoid re-probing the
#: token type on every call.
_mode_cache: dict[str, tuple[TokenMode, float]] = {}
_MODE_CACHE_TTL_S = 300.0


def normalize_token(raw: str) -> str:
    """Strip an auth scheme word if the client added one.

    Seafile requires the literal word ``Token``; clients variously send
    ``Bearer``, ``Token``, or nothing at all.
    """
    value = raw.strip()
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in _SCHEMES:
        return parts[1].strip()
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def read_raw_token() -> str | None:
    """Find a credential for the current request, or None."""
    headers = get_http_headers(include={"authorization", "x-seafile-token"})
    for key in ("authorization", "x-seafile-token"):
        value = headers.get(key) or headers.get(key.title())
        if value and value.strip():
            return normalize_token(value)

    # stdio transport: single user, credential comes from their own config.
    env_token = get_settings().api_token
    if env_token and env_token.strip():
        return normalize_token(env_token)
    return None


async def _probe_account(token: str) -> str | None:
    """Return the account email if this is an account token, else None."""
    settings = get_settings()
    client = get_http_client()
    resp = await client.get(
        f"{settings.server_url}/api2/account/info/",
        headers={"Authorization": f"Token {token}"},
    )
    if resp.status_code == 200:
        try:
            return resp.json().get("email")
        except ValueError:
            return ""
    return None


async def _probe_repo(token: str) -> bool:
    """True if this is a library (repo) token."""
    settings = get_settings()
    client = get_http_client()
    resp = await client.get(
        f"{settings.server_url}/api/v2.1/via-repo-token/repo-info/",
        headers={"Authorization": f"Token {token}"},
    )
    return resp.status_code == 200


def _check_email_domain(email: str | None) -> None:
    allowed = get_settings().allowed_email_domains
    if not allowed:
        return
    domain = (email or "").rsplit("@", 1)[-1].lower()
    if domain not in allowed:
        raise AuthError(
            "This Seafile account is not permitted to use this MCP server "
            "(email domain not in the allow list)."
        )


async def resolve_credentials() -> Credentials:
    """Resolve and validate the caller's credential.

    Raises :class:`AuthError` when absent or rejected by Seafile — we never fall
    back to an anonymous or shared identity.
    """
    token = read_raw_token()
    if not token:
        raise AuthError(
            "No Seafile token supplied. Set your Seafile API token as this MCP "
            "server's API key (Authorization header), or SEAFILE_API_TOKEN when "
            "running over stdio."
        )

    settings = get_settings()
    key = _token_hash(token)

    # When the domain allow list is active we must re-check the account on every
    # call, so the mode cache is bypassed entirely rather than caching identity.
    if not settings.allowed_email_domains:
        cached = _mode_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return Credentials(token=token, mode=cached[0])

    email = await _probe_account(token)
    if email is not None:
        _check_email_domain(email)
        mode = TokenMode.account
    elif await _probe_repo(token):
        mode = TokenMode.repo
    else:
        raise AuthError(
            "Seafile rejected this token. Check that it is current and was copied "
            "in full — either an account token or a library API token works."
        )

    if not settings.allowed_email_domains:
        _mode_cache[key] = (mode, time.monotonic() + _MODE_CACHE_TTL_S)
    return Credentials(token=token, mode=mode)


def clear_mode_cache() -> None:
    """Test hook."""
    _mode_cache.clear()
