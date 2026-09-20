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

A credential may also carry a **repo pin**, written ``"<token>:repo_id:<library-id>"``.
Seafile knows nothing of that format, so it is split off here and only the bare token
ever reaches Seafile; the pin travels on :class:`Credentials` and confines every
operation to one library (see :meth:`SeafileClient._pin_or`).
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

#: sha256(f"{token}:{repo_id}") -> expires_at, for pins already confirmed reachable.
#: Only successes are cached, so fixing a permission problem takes effect at once.
_pin_cache: dict[str, float] = {}


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


#: Marks a repo pin inside a credential. Only this exact marker counts — a parser
#: this liberal about input has no business claiming every colon it is handed.
PIN_MARKER = ":repo_id:"


def split_pin(raw: str) -> tuple[str, str | None]:
    """Split ``"<token>:repo_id:<library-id>"`` into its parts.

    A credential without the marker passes through whole, so one that merely
    happens to contain a colon is never mangled into a bogus pin. A marker with
    nothing after it is rejected rather than ignored: the caller plainly meant to
    confine this credential, and quietly handing back an unrestricted one is the
    worst possible reading of a typo.
    """
    token, marker, repo_id = raw.partition(PIN_MARKER)
    if not marker:
        return raw, None
    repo_id = repo_id.strip()
    if not repo_id:
        raise AuthError(
            f"This token ends in '{PIN_MARKER}' with no library id after it. Use "
            f"'<token>{PIN_MARKER}<library-id>' to confine it to one library, or "
            f"supply the token on its own."
        )
    return token.strip(), repo_id


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


async def _probe_pin(token: str, repo_id: str) -> bool:
    """True if this account token can actually reach the pinned library."""
    settings = get_settings()
    client = get_http_client()
    resp = await client.get(
        f"{settings.server_url}/api2/repos/{repo_id}/",
        headers={"Authorization": f"Token {token}"},
    )
    return resp.status_code == 200


async def _check_pin(token: str, repo_id: str) -> None:
    """Confirm a pin once, then remember it for a while.

    Validating up front turns a mistyped library id into one clear error instead of
    every later tool call quietly coming back empty.
    """
    key = _token_hash(f"{token}:{repo_id}")
    expires = _pin_cache.get(key)
    if expires and expires > time.monotonic():
        return
    if not await _probe_pin(token, repo_id):
        raise AuthError(
            f"This token cannot reach library {repo_id!r}. Check the library id, "
            f"and that the account it belongs to has access to that library."
        )
    _pin_cache[key] = time.monotonic() + _MODE_CACHE_TTL_S


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
    raw = read_raw_token()
    if not raw:
        raise AuthError(
            "No Seafile token supplied. Set your Seafile API token as this MCP "
            "server's API key (Authorization header), or SEAFILE_API_TOKEN when "
            "running over stdio."
        )

    token, pinned_repo_id = split_pin(raw)
    settings = get_settings()
    key = _token_hash(token)

    # When the domain allow list is active we must re-check the account on every
    # call, so the mode cache is bypassed entirely rather than caching identity.
    mode: TokenMode | None = None
    if not settings.allowed_email_domains:
        cached = _mode_cache.get(key)
        if cached and cached[1] > time.monotonic():
            mode = cached[0]

    if mode is None:
        email = await _probe_account(token)
        if email is not None:
            _check_email_domain(email)
            mode = TokenMode.account
        elif await _probe_repo(token):
            mode = TokenMode.repo
        else:
            raise AuthError(
                "Seafile rejected this token. Check that it is current and was "
                "copied in full — either an account token or a library API token "
                "works."
            )
        if not settings.allowed_email_domains:
            _mode_cache[key] = (mode, time.monotonic() + _MODE_CACHE_TTL_S)

    if pinned_repo_id:
        if mode is TokenMode.repo:
            raise AuthError(
                "This is a library API token, which Seafile already confines to one "
                "library. Drop the ':<library-id>' suffix."
            )
        await _check_pin(token, pinned_repo_id)

    return Credentials(token=token, mode=mode, pinned_repo_id=pinned_repo_id)


def clear_mode_cache() -> None:
    """Test hook."""
    _mode_cache.clear()
    _pin_cache.clear()
