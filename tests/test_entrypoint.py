"""The edge guard and the startup capability probe."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from seafile_mcp import ocr
from seafile_mcp import __main__ as entrypoint
from seafile_mcp.__main__ import (
    RequireCredential,
    _configure_debug,
    _ocr_startup_probe,
    probe_search_support,
)
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


# --------------------------------------------------------------------------- #
# Debug logging (SEAFILE_MCP_DEBUG)
# --------------------------------------------------------------------------- #

AREAS = ("auth", "client", "ocr", "server")


@pytest.fixture
def area_loggers():
    """Every logger _configure_debug may touch, restored afterwards."""
    names = ["seafile_mcp"] + [f"seafile_mcp.{a}" for a in AREAS]
    root = logging.getLogger()
    saved = {n: logging.getLogger(n).level for n in names}
    root_level = root.level
    root.setLevel(logging.INFO)
    yield {a: logging.getLogger(f"seafile_mcp.{a}") for a in AREAS}
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)
    root.setLevel(root_level)


def _debugging(loggers) -> set[str]:
    return {a for a, lg in loggers.items() if lg.isEnabledFor(logging.DEBUG)}


def test_debug_logging_is_off_unless_asked_for(area_loggers):
    _configure_debug(Settings(SEAFILE_SERVER_URL=SERVER))
    assert _debugging(area_loggers) == set()


def test_debug_logging_turns_on_only_the_areas_named(area_loggers):
    _configure_debug(Settings(SEAFILE_SERVER_URL=SERVER, debug="ocr,client"))
    assert _debugging(area_loggers) == {"ocr", "client"}


def test_debug_all_turns_on_every_area(area_loggers):
    _configure_debug(Settings(SEAFILE_SERVER_URL=SERVER, debug="all"))
    assert _debugging(area_loggers) == set(AREAS)


def test_debug_logging_never_raises_the_http_libraries(area_loggers):
    """Their own DEBUG output is a flood and carries URLs."""
    _configure_debug(Settings(SEAFILE_SERVER_URL=SERVER, debug="all"))
    assert not logging.getLogger("httpx").isEnabledFor(logging.DEBUG)


def test_debug_logging_announces_itself_at_info(area_loggers, caplog):
    """Visible at the default level, so debug left on in production is noticed."""
    caplog.set_level(logging.INFO, logger="seafile_mcp")
    _configure_debug(Settings(SEAFILE_SERVER_URL=SERVER, debug="ocr"))
    assert "Debug logging on for: ocr" in caplog.text


def test_ocr_debug_logs_the_configuration_but_never_the_key(area_loggers, caplog):
    caplog.set_level(logging.DEBUG, logger="seafile_mcp.ocr")
    _configure_debug(
        _ocr_settings(debug="ocr", ocr_api_key="super-secret-key")
    )

    assert "lightonai/LightOnOCR-2-1B" in caplog.text
    assert "max_edge=1540" in caplog.text
    assert "api_key=set" in caplog.text
    assert "super-secret-key" not in caplog.text


def test_the_ocr_configuration_is_not_logged_without_ocr(area_loggers, caplog):
    caplog.set_level(logging.DEBUG, logger="seafile_mcp.ocr")
    _configure_debug(Settings(SEAFILE_SERVER_URL=SERVER, debug="ocr"))
    assert "max_edge" not in caplog.text



# --------------------------------------------------------------------------- #
# What never reaches the log
# --------------------------------------------------------------------------- #


@pytest.fixture
def http_loggers():
    names = ("", "httpx", "httpcore")  # "" is the root logger
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_the_http_libraries_never_log_request_urls(http_loggers, monkeypatch, level):
    """httpx logs every request's full URL at INFO: a search query in plain
    text, and Seafile download links whose path is an access token. Our own
    client debug lines cover requests by endpoint instead."""
    # basicConfig is stubbed out, so set the root level it would have set:
    # without that, pytest's WARNING root would hide httpx and prove nothing.
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)
    monkeypatch.setattr(entrypoint, "install_redaction", lambda: None)
    logging.getLogger().setLevel(level)

    entrypoint._configure_logging(level)

    for name in ("httpx", "httpcore"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
        assert logging.getLogger(name).isEnabledFor(logging.WARNING)


def test_a_rejected_setting_is_reported_without_its_value(monkeypatch, caplog):
    """pydantic puts the rejected input in its message; for a URL with a
    password in it, that message is the leak."""
    async def must_not_start(*_):
        raise AssertionError("the settings were accepted and startup began")

    monkeypatch.setattr(entrypoint, "_configure_logging", lambda level: None)
    monkeypatch.setattr(entrypoint, "_startup_probe", must_not_start)
    monkeypatch.setenv("SEAFILE_MCP_OCR_URL", "http://user:hunter2-secret@gpu:8000/v1")
    monkeypatch.setenv("SEAFILE_MCP_OCR_MODEL", "m")
    entrypoint.get_settings.cache_clear()
    caplog.set_level(logging.ERROR, logger="seafile_mcp")

    assert entrypoint.main(["--transport", "http"]) == 2

    assert "SEAFILE_MCP_OCR_API_KEY" in caplog.text
    assert "hunter2-secret" not in caplog.text
