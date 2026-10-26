"""The edge guard and the startup capability probe."""

from __future__ import annotations

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from seafile_mcp import ocr
from seafile_mcp.__main__ import RequireCredential, _ocr_startup_probe, probe_search_support
from seafile_mcp.config import Settings

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


# --------------------------------------------------------------------------- #
# OCR probe
# --------------------------------------------------------------------------- #

OCR_URL = "http://gpu.test:8010/v1"


def _ocr_settings(**kw) -> Settings:
    return Settings(
        SEAFILE_SERVER_URL=SERVER,
        ocr_url=OCR_URL,
        ocr_model="lightonai/LightOnOCR-2-1B",
        **kw,
    )


def _models(*ids: str) -> httpx.Response:
    return httpx.Response(
        200, json={"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}
    )


async def test_ocr_probe_confirms_the_model_is_served(caplog):
    with respx.mock:
        respx.get(f"{OCR_URL}/models").mock(return_value=_models("lightonai/LightOnOCR-2-1B"))
        assert await ocr.probe_service(_ocr_settings()) == "ok"


async def test_ocr_probe_warns_when_the_model_is_not_served(caplog):
    with respx.mock:
        respx.get(f"{OCR_URL}/models").mock(return_value=_models("some/other-model"))
        assert await ocr.probe_service(_ocr_settings()) == "model_missing"

    assert "some/other-model" in caplog.text
    assert "SEAFILE_MCP_OCR_MODEL" in caplog.text


async def test_ocr_probe_sends_the_ocr_key():
    with respx.mock:
        route = respx.get(f"{OCR_URL}/models").mock(
            return_value=_models("lightonai/LightOnOCR-2-1B")
        )
        await ocr.probe_service(_ocr_settings(ocr_api_key="k"))

    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@pytest.mark.parametrize(
    "mock,expected",
    [
        ({"side_effect": httpx.ConnectError("refused")}, "unreachable"),
        ({"return_value": httpx.Response(401)}, "error"),
        ({"return_value": httpx.Response(200, text="<html>")}, "error"),
    ],
)
async def test_ocr_probe_only_warns_when_the_service_is_not_ready(mock, expected):
    """vLLM takes minutes to load a model. The probe must not take OCR away
    from a server that booted first; it only says what it found."""
    with respx.mock:
        respx.get(f"{OCR_URL}/models").mock(**mock)
        assert await ocr.probe_service(_ocr_settings()) == expected


def test_the_startup_probe_runs_in_its_own_loop_and_cleans_up():
    """Run from main() via asyncio.run, before uvicorn's loop exists. The
    client it opened must be closed, or the first real OCR call would reuse
    a connection bound to a loop that no longer runs."""
    with respx.mock:
        respx.get(f"{OCR_URL}/models").mock(side_effect=httpx.ConnectError("refused"))
        assert _ocr_startup_probe(_ocr_settings()) == "unreachable"

    assert ocr._client is None
