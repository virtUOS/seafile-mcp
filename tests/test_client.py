"""Endpoint-family selection, path handling, and host pinning."""

from __future__ import annotations

import httpx
import pytest
import respx

from seafile_mcp.client import SeafileClient, normalize_path
from seafile_mcp.models import (
    Credentials,
    SeafileAPIError,
    SeafileMCPError,
    TokenMode,
    UnsupportedOperation,
)

from .conftest import SERVER

ACCOUNT = Credentials(token="acct", mode=TokenMode.account)
REPO = Credentials(token="repo", mode=TokenMode.repo)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "/"),
        ("", "/"),
        ("/", "/"),
        ("docs", "/docs"),
        ("/docs/", "/docs"),
        ("/docs//a", "/docs/a"),
        ("/docs/./a", "/docs/a"),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../etc/passwd",
        "/../../etc",
        "/a/../../b",
        # normpath would quietly turn this into a valid-looking path
        "/docs/b/../a",
    ],
)
def test_path_traversal_is_rejected(raw):
    """'..' is refused outright, never normalised away into a different path."""
    with pytest.raises(ValueError, match="traversal"):
        normalize_path(raw)


# --------------------------------------------------------------------------- #
# Endpoint families
# --------------------------------------------------------------------------- #


async def test_account_token_uses_api2_endpoints():
    with respx.mock:
        route = respx.get(f"{SERVER}/api2/repos/").mock(
            return_value=httpx.Response(200, json=[{"id": "r1", "name": "Docs"}])
        )
        libs = await SeafileClient(ACCOUNT).list_libraries()
    assert route.called
    assert libs[0].id == "r1"


async def test_repo_token_uses_via_repo_token_endpoints():
    with respx.mock:
        route = respx.get(f"{SERVER}/api/v2.1/via-repo-token/repo-info/").mock(
            return_value=httpx.Response(200, json={"repo_id": "r9", "repo_name": "One"})
        )
        libs = await SeafileClient(REPO).list_libraries()
    assert route.called
    assert len(libs) == 1, "a library token sees exactly its own library"
    assert libs[0].id == "r9"


async def test_repo_token_listing_does_not_need_repo_id():
    with respx.mock:
        route = respx.get(f"{SERVER}/api/v2.1/via-repo-token/dir/").mock(
            return_value=httpx.Response(
                200, json={"dirent_list": [{"name": "a.txt", "type": "file", "size": 3}]}
            )
        )
        entries = await SeafileClient(REPO).list_directory(None, "/")
    assert route.called
    assert entries[0].path == "/a.txt"


async def test_account_token_requires_repo_id():
    with pytest.raises(ValueError, match="repo_id is required"):
        await SeafileClient(ACCOUNT).list_directory(None, "/")


@pytest.mark.parametrize("operation", ["delete", "move", "copy", "search"])
async def test_repo_token_refuses_operations_seafile_does_not_offer(operation):
    """These have no via-repo-token endpoint; we say so instead of faking it."""
    client = SeafileClient(REPO)
    calls = {
        "delete": lambda: client.delete("r", "/a.txt"),
        "move": lambda: client.move("r", "/a.txt", "/dst"),
        "copy": lambda: client.copy("r", "/a.txt", "/dst"),
        "search": lambda: client.search("q"),
    }
    with pytest.raises(UnsupportedOperation, match="library API token"):
        await calls[operation]()


# --------------------------------------------------------------------------- #
# Host pinning / redirects
# --------------------------------------------------------------------------- #


async def test_download_link_on_foreign_host_is_refused():
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json="https://evil.example/steal")
        )
        with pytest.raises(SeafileMCPError, match="not the configured Seafile host"):
            await SeafileClient(ACCOUNT).get_download_link("r1", "/a.txt")


async def test_no_credentials_are_sent_to_a_foreign_host():
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json="https://evil.example/steal")
        )
        evil = respx.get("https://evil.example/steal").mock(
            return_value=httpx.Response(200, text="pwned")
        )
        with pytest.raises(SeafileMCPError):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/a.txt")
    assert not evil.called, "we must never issue the request at all"


async def test_upload_link_on_foreign_host_is_refused():
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/upload-link/").mock(
            return_value=httpx.Response(200, json="https://evil.example/upload")
        )
        with pytest.raises(SeafileMCPError, match="not the configured Seafile host"):
            await SeafileClient(ACCOUNT).get_upload_link("r1")


async def test_upload_link_is_requested_for_the_upload_directory_repo_token():
    # Seahub's ViaRepoUploadLinkView reads the query param as "path" (unlike
    # every other via-repo-token endpoint's "p" sibling on api2) and mints the
    # upload token scoped to whatever directory that names. If we send the
    # wrong param name it's silently ignored, Seahub scopes the token to "/",
    # and the follow-up upload 403s because the fileserver strictly matches
    # the token's directory against the POSTed parent_dir.
    with respx.mock:
        link_route = respx.get(
            f"{SERVER}/api/v2.1/via-repo-token/upload-link/",
            params={"path": "/reports"},
        ).mock(return_value=httpx.Response(200, json=f"{SERVER}/upload-api/tok"))
        upload_route = respx.post(f"{SERVER}/upload-api/tok").mock(
            return_value=httpx.Response(200, text='"ok"')
        )
        await SeafileClient(REPO).upload_bytes(None, "/reports", "a.txt", b"hi")
    assert link_route.called
    assert upload_route.calls.last.request.read().count(b'name="parent_dir"')
    assert b"/reports" in upload_route.calls.last.request.read()


async def test_upload_link_is_requested_for_the_upload_directory_account_token():
    # The account-token endpoint (api2) uses "p", not "path" — the opposite of
    # the via-repo-token endpoint above. Pin both so a future edit can't
    # accidentally swap them or unify them onto the wrong name.
    with respx.mock:
        link_route = respx.get(
            f"{SERVER}/api2/repos/r1/upload-link/",
            params={"p": "/reports"},
        ).mock(return_value=httpx.Response(200, json=f"{SERVER}/upload-api/tok"))
        upload_route = respx.post(f"{SERVER}/upload-api/tok").mock(
            return_value=httpx.Response(200, text='"ok"')
        )
        await SeafileClient(ACCOUNT).upload_bytes("r1", "/reports", "a.txt", b"hi")
    assert link_route.called
    assert b"/reports" in upload_route.calls.last.request.read()


async def test_redirects_are_not_followed_with_credentials():
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/").mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/"})
        )
        with pytest.raises(SeafileAPIError, match="refusing to follow"):
            await SeafileClient(ACCOUNT).list_libraries()


async def test_download_link_on_configured_host_is_accepted():
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
        )
        link = await SeafileClient(ACCOUNT).get_download_link("r1", "/a.txt")
    assert link.startswith(SERVER)


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


async def test_rename_rejects_a_path_as_the_new_name():
    with pytest.raises(ValueError, match="bare name"):
        await SeafileClient(REPO).rename(None, "/a.txt", "../../b.txt")


async def test_api_errors_surface_the_status():
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        with pytest.raises(SeafileAPIError) as exc:
            await SeafileClient(ACCOUNT).list_libraries()
    assert exc.value.status == 403
