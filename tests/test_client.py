"""Endpoint-family selection, path handling, and host pinning."""

from __future__ import annotations

import httpx
import pytest
import respx

from seafile_mcp.client import SeafileClient, normalize_path
from seafile_mcp.config import get_settings
from seafile_mcp.models import (
    Credentials,
    SafetyError,
    SeafileAPIError,
    SeafileMCPError,
    TokenMode,
    UnsupportedOperation,
)

from .conftest import SERVER
from .conftest import image_bytes as _image_bytes

ACCOUNT = Credentials(token="acct", mode=TokenMode.account)
REPO = Credentials(token="repo", mode=TokenMode.repo)
PINNED = Credentials(token="acct", mode=TokenMode.account, pinned_repo_id="mine")


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
# Repo-pinned credentials
# --------------------------------------------------------------------------- #

#: Every account-mode operation that names a library. A pinned credential must
#: refuse a foreign library through all of them — this is the guard that catches a
#: tool added later forgetting the pin, which is the main risk of enforcing it in
#: code rather than relying on Seafile.
PINNED_OPERATIONS = {
    "list_directory": lambda c, rid: c.list_directory(rid, "/"),
    "get_library_info": lambda c, rid: c.get_library_info(rid),
    "get_file_info": lambda c, rid: c.get_file_info(rid, "/a.txt"),
    "get_download_link": lambda c, rid: c.get_download_link(rid, "/a.txt"),
    "get_upload_link": lambda c, rid: c.get_upload_link(rid, "/"),
    "create_directory": lambda c, rid: c.create_directory(rid, "/new"),
    "rename": lambda c, rid: c.rename(rid, "/a.txt", "b.txt"),
    "move": lambda c, rid: c.move(rid, "/a.txt", "/dst"),
    "copy": lambda c, rid: c.copy(rid, "/a.txt", "/dst"),
    "delete": lambda c, rid: c.delete(rid, "/a.txt"),
    "search": lambda c, rid: c.search("q", rid),
    "count_items": lambda c, rid: c.count_items(rid, "/"),
}


@pytest.mark.parametrize("operation", sorted(PINNED_OPERATIONS))
async def test_pinned_credential_refuses_every_foreign_repo_operation(operation):
    client = SeafileClient(PINNED)
    with pytest.raises(UnsupportedOperation, match="confined to library 'mine'"):
        await PINNED_OPERATIONS[operation](client, "somebody-elses-repo")


@pytest.mark.parametrize("operation", sorted(PINNED_OPERATIONS))
async def test_pinned_credential_never_reaches_seafile_for_a_foreign_repo(operation):
    """The refusal happens before any request, so no token is ever sent."""
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(UnsupportedOperation):
            await PINNED_OPERATIONS[operation](SeafileClient(PINNED), "foreign")
        assert not mock.calls


async def test_pinned_credential_supplies_the_repo_when_none_is_given():
    with respx.mock:
        route = respx.get(f"{SERVER}/api2/repos/mine/dir/").mock(
            return_value=httpx.Response(200, json=[])
        )
        await SeafileClient(PINNED).list_directory(None, "/")
    assert route.called


async def test_pinned_credential_accepts_its_own_repo():
    with respx.mock:
        route = respx.get(f"{SERVER}/api2/repos/mine/dir/").mock(
            return_value=httpx.Response(200, json=[])
        )
        await SeafileClient(PINNED).list_directory("mine", "/")
    assert route.called


async def test_pinned_credential_lists_only_its_own_library():
    """Otherwise the account-wide listing hands back every other library's id."""
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/mine/").mock(
            return_value=httpx.Response(200, json={"id": "mine", "name": "Mine"})
        )
        every_repo = respx.get(f"{SERVER}/api2/repos/").mock(
            return_value=httpx.Response(200, json=[{"id": "other", "name": "Other"}])
        )
        libraries = await SeafileClient(PINNED).list_libraries()

    assert not every_repo.called
    assert [lib.id for lib in libraries] == ["mine"]


async def test_pinned_credential_refuses_copying_out_to_another_library():
    """Copying *into* a library needs no read access, so dst needs the same check."""
    client = SeafileClient(PINNED)
    with pytest.raises(UnsupportedOperation, match="confined to library 'mine'"):
        await client.copy("mine", "/a.txt", "/dst", dst_repo_id="exfiltrate-here")


async def test_pinned_search_scopes_the_query_to_the_pinned_library():
    with respx.mock:
        route = respx.get(f"{SERVER}/api2/search/").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        await SeafileClient(PINNED).search("anything")
    assert route.calls.last.request.url.params["search_repo"] == "mine"


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


# --------------------------------------------------------------------------- #
# Download cap
# --------------------------------------------------------------------------- #


def _cap_at_1mb(monkeypatch):
    monkeypatch.setenv("SEAFILE_MCP_MAX_DOWNLOAD_MB", "1")
    get_settings.cache_clear()


def _mock_download(body: bytes):
    respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
        return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
    )
    return respx.get(f"{SERVER}/f/abc/").mock(
        return_value=httpx.Response(200, content=body)
    )


async def test_a_file_within_the_cap_comes_back_whole():
    with respx.mock:
        _mock_download(b"hello")
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/a.txt")
    assert got.data == b"hello"
    assert got.partial is False


async def test_an_oversized_text_file_comes_back_as_a_prefix(monkeypatch):
    """The first N bytes of a log or Markdown file are exactly the first N
    bytes of its text, so refusing it outright would be pure loss."""
    _cap_at_1mb(monkeypatch)
    body = b"a" * (3 * 1024 * 1024)

    with respx.mock:
        _mock_download(body)
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/big.log")

    assert got.partial is True
    assert len(got.data) == 1024 * 1024
    assert got.data == body[: 1024 * 1024]
    assert got.total_size == len(body)


async def test_an_oversized_pdf_is_refused_before_the_whole_transfer(monkeypatch):
    """A PDF's xref table is at the end, so a prefix cannot be opened. Bailing
    on the declared size means a 2 GB file costs one chunk, not the full cap."""
    _cap_at_1mb(monkeypatch)
    body = b"%PDF-1.7" + b"\x00" * (3 * 1024 * 1024)

    with respx.mock:
        _mock_download(body)
        with pytest.raises(SafetyError, match="cannot be read from part of a file"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/big.pdf")


async def test_an_oversized_ooxml_file_is_refused(monkeypatch):
    """.docx/.xlsx/.pptx are ZIPs, and the central directory is at the end."""
    _cap_at_1mb(monkeypatch)
    body = b"PK\x03\x04" + b"\x00" * (3 * 1024 * 1024)

    with respx.mock:
        _mock_download(body)
        with pytest.raises(SafetyError, match="cannot be read from part of a file"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/big.docx")


async def test_an_oversized_pdf_with_no_declared_size_is_still_refused(monkeypatch):
    """Chunked transfer sends no Content-Length, so the same decision has to
    hold once the cap is actually reached."""
    _cap_at_1mb(monkeypatch)

    async def stream():
        yield b"%PDF-1.7" + b"\x00" * 100_000
        for _ in range(20):
            yield b"\x00" * 100_000

    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
        )
        respx.get(f"{SERVER}/f/abc/").mock(
            return_value=httpx.Response(200, stream=stream())
        )
        with pytest.raises(SafetyError, match="larger than this server"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/big.pdf")


async def test_a_small_pdf_is_not_refused(monkeypatch):
    """The format check must only fire alongside the size limit."""
    _cap_at_1mb(monkeypatch)
    body = b"%PDF-1.7 tiny"

    with respx.mock:
        _mock_download(body)
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/small.pdf")

    assert got.data == body
    assert got.partial is False


async def test_an_oversized_pdf_with_no_declared_size_is_refused_after_download(monkeypatch):
    """When Content-Length is absent (chunked transfer), the same safety decision
    must hold once we've actually received the data. A PDF prefix cannot be opened,
    so we should raise even though partial=False because the loop ended naturally."""
    _cap_at_1mb(monkeypatch)

    async def stream():
        # Send just over 1 MB of PDF data, but no Content-Length
        yield b"%PDF-1.7" + b"\x00" * 100_000
        for _ in range(20):
            yield b"\x00" * 100_000  # Total: ~2 MB

    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
        )
        respx.get(f"{SERVER}/f/abc/").mock(
            return_value=httpx.Response(200, stream=stream())
        )
        with pytest.raises(SafetyError, match="larger than this server"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/big.pdf")
async def test_a_redirect_on_the_download_is_refused():
    """The streaming rewrite must not quietly regain follow_redirects."""
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
        )
        respx.get(f"{SERVER}/f/abc/").mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/"})
        )
        with pytest.raises(SeafileAPIError, match="refusing to follow"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/a.txt")


async def test_an_error_on_the_download_is_reported():
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
        )
        respx.get(f"{SERVER}/f/abc/").mock(
            return_value=httpx.Response(404, text="gone")
        )
        with pytest.raises(SeafileAPIError, match="404"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/a.txt")


# --------------------------------------------------------------------------- #
# The cap boundary
# --------------------------------------------------------------------------- #


async def test_a_file_of_exactly_the_cap_is_not_partial(monkeypatch):
    """Reaching the cap means the file ends there, not that more follows.

    With `total >= max_bytes` this was flagged partial although the download
    had completed, which told the caller there was content the server could
    not reach when there was none.
    """
    _cap_at_1mb(monkeypatch)
    body = b"a" * (1024 * 1024)

    with respx.mock:
        _mock_download(body)
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/exact.txt")

    assert got.partial is False
    assert got.data == body


async def test_a_pdf_of_exactly_the_cap_is_returned_not_refused(monkeypatch):
    """Same boundary, sharper consequence: a complete PDF was being refused."""
    _cap_at_1mb(monkeypatch)
    body = b"%PDF-1.7" + b"\x00" * (1024 * 1024 - 8)
    assert len(body) == 1024 * 1024

    with respx.mock:
        _mock_download(body)
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/exact.pdf")

    assert got.partial is False
    assert got.data == body


async def test_one_byte_over_the_cap_is_partial(monkeypatch):
    """The other side of the boundary still has to behave."""
    _cap_at_1mb(monkeypatch)
    body = b"a" * (1024 * 1024 + 1)

    with respx.mock:
        _mock_download(body)
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/over.txt")

    assert got.partial is True
    assert len(got.data) == 1024 * 1024


async def test_a_declared_size_of_zero_is_a_size_not_a_missing_one():
    """`declared or None` turned a real 0 into "size unknown".

    The Content-Length is set explicitly here: httpx omits the header entirely
    for an empty body, which would exercise the absent case rather than this
    one.
    """
    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
        )
        respx.get(f"{SERVER}/f/abc/").mock(
            return_value=httpx.Response(200, content=b"", headers={"content-length": "0"})
        )
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/empty.txt")

    assert got.data == b""
    assert got.partial is False
    assert got.total_size == 0


async def test_a_missing_content_length_reports_no_size():
    with respx.mock:
        _mock_download(b"hello")
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/a.txt")
    assert got.data == b"hello"


async def test_an_oversized_image_is_refused_before_the_whole_transfer(monkeypatch):
    """A prefix of a PNG is not a smaller picture, it is an undecodable
    fragment — and a decoder that does limp through one hands a vision model a
    half-grey image it will describe with confidence. A wrong answer, not a
    partial one."""
    _cap_at_1mb(monkeypatch)
    body = _image_bytes(8, 8, "PNG") + b"\x00" * (3 * 1024 * 1024)

    with respx.mock:
        _mock_download(body)
        with pytest.raises(SafetyError, match="cannot be read from part of a file"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/big.png")


async def test_an_oversized_image_with_no_declared_size_is_refused(monkeypatch):
    """The chunked-transfer path, where the decision has to hold again once the
    cap is actually reached."""
    _cap_at_1mb(monkeypatch)
    head = _image_bytes(8, 8, "JPEG")

    async def stream():
        yield head + b"\x00" * 100_000
        for _ in range(20):
            yield b"\x00" * 100_000

    with respx.mock:
        respx.get(f"{SERVER}/api2/repos/r1/file/").mock(
            return_value=httpx.Response(200, json=f"{SERVER}/f/abc/")
        )
        respx.get(f"{SERVER}/f/abc/").mock(
            return_value=httpx.Response(200, stream=stream())
        )
        with pytest.raises(SafetyError, match="larger than this server"):
            await SeafileClient(ACCOUNT).read_file_bytes("r1", "/big.jpg")


async def test_an_image_within_the_cap_is_not_refused(monkeypatch):
    """The format check only fires alongside the size limit."""
    _cap_at_1mb(monkeypatch)
    body = _image_bytes(40, 30, "PNG")

    with respx.mock:
        _mock_download(body)
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/small.png")

    assert got.partial is False
    assert got.data == body


async def test_an_oversized_text_file_starting_with_bm_is_still_a_prefix(monkeypatch):
    """The BMP false-positive guard, at the layer where it actually costs
    something: "BM" alone would make this text file get refused instead of
    returned."""
    _cap_at_1mb(monkeypatch)
    body = b"BMW and other marques, discussed at length. " * 80_000

    with respx.mock:
        _mock_download(body)
        got = await SeafileClient(ACCOUNT).read_file_bytes("r1", "/cars.txt")

    assert got.partial is True
    assert got.data == body[: 1024 * 1024]
