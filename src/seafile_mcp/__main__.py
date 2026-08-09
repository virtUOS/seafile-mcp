"""Entrypoint: transport selection, startup capability probe, and the HTTP guard."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ._http import close_http_client, get_http_client
from .config import get_settings
from .safety import install_redaction
from .server import build_server

logger = logging.getLogger("seafile_mcp")


async def probe_search_support() -> bool:
    """Detect whether this Seafile instance has the search API at all.

    Seafile file search is a Professional-edition feature; Community Edition does
    not route ``/api2/search/`` at all. We probe *unauthenticated* on purpose:
    URL routing happens before authentication, so a missing feature answers 404
    while a present one answers 401/403. That lets us tell "not installed" from
    "needs a token" without holding any credential of our own.
    """
    settings = get_settings()
    try:
        resp = await get_http_client().get(f"{settings.server_url}/api2/search/")
    except httpx.HTTPError as exc:
        logger.warning(
            "Could not reach Seafile to probe for search support (%s). "
            "Disabling seafile_search; set SEAFILE_MCP_ENABLE_SEARCH=true to force it.",
            exc.__class__.__name__,
        )
        return False
    if resp.status_code in (401, 403):
        return True
    if resp.status_code == 404:
        logger.info(
            "Seafile has no /api2/search/ endpoint (Community Edition); "
            "seafile_search will not be registered."
        )
        return False
    logger.warning(
        "Unexpected status %s probing /api2/search/; disabling seafile_search.",
        resp.status_code,
    )
    return False


class RequireCredential:
    """Reject unauthenticated MCP requests at the edge.

    Deny by default: there is no anonymous path into any tool. The token itself
    is validated against Seafile later; this only ensures one was supplied.
    """

    def __init__(self, app: ASGIApp, mcp_path: str) -> None:
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(self.mcp_path):
            headers = {k.decode().lower(): v for k, v in scope.get("headers", [])}
            if not headers.get("authorization") and not headers.get("x-seafile-token"):
                response = JSONResponse(
                    {
                        "error": "missing_credential",
                        "detail": (
                            "Supply your Seafile API token as this server's API key. "
                            "Create one in Seafile under library -> Advanced -> API Token."
                        ),
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": 'Token realm="seafile-mcp"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    install_redaction()


async def _startup_probe(force: str | None) -> bool:
    if force is not None:
        return force.strip().lower() in ("1", "true", "yes")
    try:
        return await probe_search_support()
    finally:
        await close_http_client()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seafile-mcp")
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default="http",
        help="http for a shared multi-user deployment, stdio for a local single user",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    _configure_logging(args.log_level)

    try:
        settings = get_settings()
    except Exception as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    import os

    search_enabled = asyncio.run(_startup_probe(os.environ.get("SEAFILE_MCP_ENABLE_SEARCH")))

    mcp = build_server(search_enabled=search_enabled, settings=settings)
    logger.info(
        "seafile-mcp starting: upstream=%s mode=%s search=%s transport=%s",
        settings.server_url,
        settings.mode.value,
        "on" if search_enabled else "off",
        args.transport,
    )

    if args.transport == "stdio":
        if not settings.api_token:
            logger.warning(
                "No SEAFILE_API_TOKEN set; stdio transport has no other way to "
                "obtain a credential and every call will fail."
            )
        mcp.run(transport="stdio")
        return 0

    import uvicorn

    app = mcp.http_app(
        path=settings.http_path,
        middleware=[Middleware(RequireCredential, mcp_path=settings.http_path)],
    )
    uvicorn.run(
        app,
        host=args.host or settings.http_host,
        port=args.port or settings.http_port,
        log_level=args.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
