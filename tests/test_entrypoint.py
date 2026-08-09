"""The edge guard and the startup capability probe."""

from __future__ import annotations

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from seafile_mcp.__main__ import RequireCredential, probe_search_support

from .conftest import SERVER


@pytest.fixture
def guarded_app():
    async def ok(request):
        return PlainTextResponse("reached")

    app = Starlette(routes=[Route("/mcp", ok), Route("/healthz", ok)])
    return TestClient(RequireCredential(app, mcp_path="/mcp"))


def test_request_without_a_credential_is_rejected(guarded_app):
    resp = guarded_app.get("/mcp")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Token")
    assert resp.json()["error"] == "missing_credential"


def test_authorization_header_passes_the_guard(guarded_app):
    resp = guarded_app.get("/mcp", headers={"Authorization": "Token abc"})
    assert resp.status_code == 200


def test_x_seafile_token_header_passes_the_guard(guarded_app):
    resp = guarded_app.get("/mcp", headers={"X-Seafile-Token": "abc"})
    assert resp.status_code == 200


def test_the_guard_only_covers_the_mcp_path(guarded_app):
    assert guarded_app.get("/healthz").status_code == 200


def test_the_401_body_carries_no_credential_hint(guarded_app):
    """The error tells the user what to do without echoing anything secret."""
    body = guarded_app.get("/mcp").text
    assert "API Token" in body


# --------------------------------------------------------------------------- #
# Search probe
# --------------------------------------------------------------------------- #


async def test_search_probe_reads_404_as_community_edition():
    """CE does not route /api2/search/ at all, so it 404s before auth runs."""
    with respx.mock:
        respx.get(f"{SERVER}/api2/search/").mock(return_value=httpx.Response(404))
        assert await probe_search_support() is False


@pytest.mark.parametrize("status", [401, 403])
async def test_search_probe_reads_auth_errors_as_professional_edition(status):
    """The endpoint exists (it demanded a token), so the feature is present."""
    with respx.mock:
        respx.get(f"{SERVER}/api2/search/").mock(return_value=httpx.Response(status))
        assert await probe_search_support() is True


async def test_search_probe_sends_no_credential():
    with respx.mock:
        route = respx.get(f"{SERVER}/api2/search/").mock(
            return_value=httpx.Response(401)
        )
        await probe_search_support()
    assert "authorization" not in route.calls[0].request.headers


async def test_search_probe_degrades_when_seafile_is_unreachable():
    with respx.mock:
        respx.get(f"{SERVER}/api2/search/").mock(
            side_effect=httpx.ConnectError("refused")
        )
        assert await probe_search_support() is False
