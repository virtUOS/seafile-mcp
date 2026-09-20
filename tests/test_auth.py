"""Credential resolution: normalization, probe order, and isolation."""

from __future__ import annotations

import httpx
import pytest
import respx

from seafile_mcp import auth
from seafile_mcp.auth import normalize_token, resolve_credentials, split_pin
from seafile_mcp.models import AuthError, TokenMode

from .conftest import SERVER

ACCOUNT_URL = f"{SERVER}/api2/account/info/"
REPO_URL = f"{SERVER}/api/v2.1/via-repo-token/repo-info/"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        ("Token abc123", "abc123"),
        ("TOKEN abc123", "abc123"),
        ("abc123", "abc123"),
        ("  abc123  ", "abc123"),
        ("Bearer   abc123 ", "abc123"),
    ],
)
def test_normalize_token_accepts_every_client_convention(raw, expected):
    assert normalize_token(raw) == expected


def test_normalize_token_keeps_unknown_schemes_intact():
    # Not a scheme we recognise, so it is not ours to strip.
    assert normalize_token("Basic abc123") == "Basic abc123"


async def test_missing_credential_is_rejected(no_http_headers):
    with pytest.raises(AuthError, match="No Seafile token"):
        await resolve_credentials()


async def test_env_token_used_for_stdio(no_http_headers, monkeypatch):
    monkeypatch.setenv("SEAFILE_API_TOKEN", "Token envtok")
    from seafile_mcp.config import get_settings

    get_settings.cache_clear()

    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        creds = await resolve_credentials()
    assert creds.mode is TokenMode.account
    assert creds.token == "envtok"


async def test_x_seafile_token_header_is_a_fallback(headers):
    headers(x_seafile_token="repo-token")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(401))
        respx.get(REPO_URL).mock(return_value=httpx.Response(200, json={"repo_id": "r"}))
        creds = await resolve_credentials()
    assert creds.mode is TokenMode.repo


async def test_account_probed_before_repo(headers):
    headers(authorization="Token acct")
    with respx.mock:
        account = respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        repo = respx.get(REPO_URL).mock(return_value=httpx.Response(200))
        creds = await resolve_credentials()
    assert creds.mode is TokenMode.account
    assert account.called
    assert not repo.called, "repo endpoint should not be probed once account succeeds"


async def test_token_rejected_by_both_probes(headers):
    headers(authorization="Token nope")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(401))
        respx.get(REPO_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(AuthError, match="rejected this token"):
            await resolve_credentials()


async def test_outgoing_scheme_is_always_seafiles_token_form(headers):
    """Clients send Bearer; Seafile only accepts 'Token'."""
    headers(authorization="Bearer abc")
    with respx.mock:
        route = respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        await resolve_credentials()
    assert route.calls[0].request.headers["authorization"] == "Token abc"


async def test_mode_cache_never_crosses_tokens(headers):
    """A cache hit for one token must not answer for another."""
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            side_effect=lambda request: httpx.Response(200, json={"email": "a@b.c"})
            if request.headers["authorization"] == "Token acct"
            else httpx.Response(401)
        )
        respx.get(REPO_URL).mock(return_value=httpx.Response(200, json={"repo_id": "r"}))

        headers(authorization="Token acct")
        first = await resolve_credentials()

        headers(authorization="Token repo")
        second = await resolve_credentials()

        headers(authorization="Token acct")
        third = await resolve_credentials()

    assert first.mode is TokenMode.account
    assert second.mode is TokenMode.repo, "second token must be probed on its own"
    assert third.mode is TokenMode.account
    assert first.token != second.token


async def test_mode_cache_holds_no_token_or_user_data(headers):
    headers(authorization="Token secrettoken")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "someone@example.org"})
        )
        await resolve_credentials()

    dumped = repr(auth._mode_cache)
    assert "secrettoken" not in dumped
    assert "someone@example.org" not in dumped


# --------------------------------------------------------------------------- #
# Repo-pinned credentials
# --------------------------------------------------------------------------- #

REPO_INFO_URL = f"{SERVER}/api2/repos/lib1/"


@pytest.mark.parametrize(
    "raw,token,pin",
    [
        ("abc123", "abc123", None),
        ("abc123:repo_id:lib1", "abc123", "lib1"),
        ("abc123:repo_id:1a2b-3c4d-5e6f", "abc123", "1a2b-3c4d-5e6f"),
        ("abc123:repo_id: lib1 ", "abc123", "lib1"),
    ],
)
def test_split_pin_separates_the_library_from_the_token(raw, token, pin):
    assert split_pin(raw) == (token, pin)


@pytest.mark.parametrize(
    "raw",
    [
        "abc:123",  # a colon is not by itself a pin
        "abc123:repoid:lib1",  # marker misspelt
        "abc123:repo-id:lib1",
        "Basic abc:123",  # a scheme we do not recognise, left intact upstream
    ],
)
def test_split_pin_leaves_anything_without_the_marker_alone(raw):
    """Only the marker means a pin, so a stray colon is never mangled into one.

    Such a credential then fails as an unrecognised token, which is what it is —
    never as an unrestricted one.
    """
    assert split_pin(raw) == (raw, None)


def test_split_pin_refuses_an_empty_library_id():
    """Silently returning an unrestricted credential is the worst reading of a typo."""
    with pytest.raises(AuthError, match="no library id"):
        split_pin("abc123:repo_id:")


async def test_pinned_credential_is_validated_and_carried(headers):
    headers(authorization="Token acct:repo_id:lib1")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        probe = respx.get(REPO_INFO_URL).mock(
            return_value=httpx.Response(200, json={"id": "lib1"})
        )
        creds = await resolve_credentials()

    assert probe.called
    assert creds.pinned_repo_id == "lib1"
    assert creds.token == "acct", "only the bare token may reach Seafile"


async def test_only_the_bare_token_is_sent_to_seafile(headers):
    headers(authorization="Token acct:repo_id:lib1")
    with respx.mock:
        route = respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        respx.get(REPO_INFO_URL).mock(return_value=httpx.Response(200, json={}))
        await resolve_credentials()

    assert route.calls[0].request.headers["authorization"] == "Token acct"


async def test_unreachable_pinned_library_fails_fast(headers):
    """Better one clear error than every later call quietly coming back empty."""
    headers(authorization="Token acct:repo_id:lib1")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        respx.get(REPO_INFO_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(AuthError, match="cannot reach library 'lib1'"):
            await resolve_credentials()


async def test_failed_pin_is_not_cached(headers):
    """A fixed permission must take effect at once, not after the TTL."""
    headers(authorization="Token acct:repo_id:lib1")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        probe = respx.get(REPO_INFO_URL).mock(return_value=httpx.Response(403))
        for _ in range(2):
            with pytest.raises(AuthError):
                await resolve_credentials()

    assert probe.call_count == 2


async def test_validated_pin_is_cached(headers):
    headers(authorization="Token acct:repo_id:lib1")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        probe = respx.get(REPO_INFO_URL).mock(
            return_value=httpx.Response(200, json={"id": "lib1"})
        )
        await resolve_credentials()
        await resolve_credentials()

    assert probe.call_count == 1


async def test_pin_cache_holds_no_token(headers):
    headers(authorization="Token secrettoken:repo_id:lib1")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@b.c"})
        )
        respx.get(REPO_INFO_URL).mock(return_value=httpx.Response(200, json={}))
        await resolve_credentials()

    assert "secrettoken" not in repr(auth._pin_cache)


async def test_pinning_a_library_token_is_a_misconfiguration(headers):
    """Seafile already confines it; a second scope means the user misunderstood."""
    headers(authorization="Token repotok:repo_id:lib1")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(401))
        respx.get(REPO_URL).mock(return_value=httpx.Response(200, json={"repo_id": "r"}))
        with pytest.raises(AuthError, match="already confines to one library"):
            await resolve_credentials()


async def test_email_domain_allowlist_blocks_outsiders(headers, monkeypatch):
    monkeypatch.setenv("SEAFILE_MCP_ALLOWED_EMAIL_DOMAINS", "uni-osnabrueck.de")
    from seafile_mcp.config import get_settings

    get_settings.cache_clear()

    headers(authorization="Token acct")
    with respx.mock:
        respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "someone@elsewhere.com"})
        )
        with pytest.raises(AuthError, match="not permitted"):
            await resolve_credentials()


async def test_allowlist_bypasses_cache_so_it_cannot_be_stale(headers, monkeypatch):
    monkeypatch.setenv("SEAFILE_MCP_ALLOWED_EMAIL_DOMAINS", "example.org")
    from seafile_mcp.config import get_settings

    get_settings.cache_clear()
    headers(authorization="Token acct")

    with respx.mock:
        route = respx.get(ACCOUNT_URL).mock(
            return_value=httpx.Response(200, json={"email": "a@example.org"})
        )
        await resolve_credentials()
        await resolve_credentials()

    assert route.call_count == 2, "allowlist must re-check identity every call"
    assert not auth._mode_cache
