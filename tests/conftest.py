from __future__ import annotations

import asyncio

import pytest

from seafile_mcp import auth, safety
from seafile_mcp._http import close_http_client
from seafile_mcp.config import get_settings

SERVER = "https://seafile.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SEAFILE_SERVER_URL", SERVER)
    monkeypatch.delenv("SEAFILE_API_TOKEN", raising=False)
    monkeypatch.delenv("SEAFILE_MCP_MODE", raising=False)
    monkeypatch.delenv("SEAFILE_MCP_ALLOWED_EMAIL_DOMAINS", raising=False)
    get_settings.cache_clear()
    auth.clear_mode_cache()
    safety.reset_state()
    yield
    asyncio.run(close_http_client())
    get_settings.cache_clear()
    auth.clear_mode_cache()
    safety.reset_state()


@pytest.fixture
def no_http_headers(monkeypatch: pytest.MonkeyPatch):
    """Simulate stdio / no active HTTP request."""
    monkeypatch.setattr(auth, "get_http_headers", lambda **kw: {})


@pytest.fixture
def headers(monkeypatch: pytest.MonkeyPatch):
    """Let a test set the headers seen by the auth layer."""
    current: dict[str, str] = {}

    def _set(**kw: str) -> None:
        current.clear()
        current.update({k.replace("_", "-").lower(): v for k, v in kw.items()})

    monkeypatch.setattr(auth, "get_http_headers", lambda **kw: dict(current))
    return _set
