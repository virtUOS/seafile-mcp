"""Seafile Web API client.

Seafile exposes two disjoint API families and which one applies depends entirely
on the *kind* of token supplied:

``account`` mode -> ``/api2/...`` and ``/api/v2.1/...``
    Everything, across every library the account can see.

``repo`` mode -> ``/api/v2.1/via-repo-token/...``
    One library, and only these operations: list a directory, create a directory,
    rename, get an upload link, get a download link, read repo info. Seafile
    publishes **no** delete, move, copy or search endpoint for library tokens, so
    those raise :class:`UnsupportedOperation` rather than being faked.

API reference: https://seafile-api.readme.io/ and
https://plus.seafile.com/published/web-api/v2.1/library-api-tokens.md
"""

from __future__ import annotations

import posixpath
from typing import Any
from urllib.parse import quote

import httpx

from ._http import assert_same_host, get_http_client
from .config import get_settings
from .models import (
    Credentials,
    DirEntry,
    FileInfo,
    LibraryInfo,
    SeafileAPIError,
    TokenMode,
    UnsupportedOperation,
)


def normalize_path(path: str | None) -> str:
    """Normalise a library-relative path and reject traversal.

    Seafile paths are absolute within a library. ``..`` is rejected outright on
    the *raw* input rather than normalised away: ``posixpath.normpath`` silently
    collapses a leading ``..`` at the root (``/../../etc`` becomes ``/etc``), so
    normalising first would quietly rewrite the caller's path instead of
    refusing it. No legitimate Seafile path contains ``..``.
    """
    raw = (path or "/").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    if ".." in raw.split("/"):
        raise ValueError(f"Invalid path {path!r}: path traversal is not allowed.")
    normalized = posixpath.normpath(raw)
    return "/" if normalized == "." else normalized


def is_library_root(path: str) -> bool:
    return normalize_path(path) == "/"


class SeafileClient:
    """One instance per request, bound to that request's credentials."""

    def __init__(self, creds: Credentials) -> None:
        self._creds = creds
        self._settings = get_settings()
        self._http = get_http_client()

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #

    @property
    def mode(self) -> TokenMode:
        return self._creds.mode

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._creds.token}"}

    def _url(self, path: str) -> str:
        return f"{self._settings.server_url}{path}"

    def _require_account(self, operation: str) -> None:
        if self._creds.mode is not TokenMode.account:
            raise UnsupportedOperation(
                f"{operation} is not available with a library API token — Seafile "
                f"provides no via-repo-token endpoint for it. Use an account token "
                f"if you need this operation."
            )

    def _require_repo_id(self, repo_id: str | None) -> str:
        if self._creds.mode is TokenMode.account and not repo_id:
            raise ValueError(
                "repo_id is required when using an account token. Call "
                "seafile_list_libraries first to find the library id."
            )
        return repo_id or ""

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        headers = {**self._headers(), **kw.pop("headers", {})}
        resp = await self._http.request(method, url, headers=headers, **kw)
        if resp.is_redirect:
            raise SeafileAPIError(
                resp.status_code,
                "Seafile returned a redirect; refusing to follow it with credentials "
                "attached.",
            )
        if resp.status_code >= 400:
            raise SeafileAPIError(resp.status_code, _short_body(resp))
        return resp

    async def _get_json(self, url: str, **kw: Any) -> Any:
        resp = await self._request("GET", url, **kw)
        return resp.json()

    # ------------------------------------------------------------------ #
    # libraries
    # ------------------------------------------------------------------ #

    async def list_libraries(self) -> list[LibraryInfo]:
        if self._creds.mode is TokenMode.repo:
            return [await self.get_library_info(None)]
        data = await self._get_json(self._url("/api2/repos/"))
        return [
            LibraryInfo(
                id=item["id"],
                name=item.get("name", ""),
                permission=item.get("permission"),
                size=item.get("size"),
                modified=str(item.get("mtime") or item.get("last_modified") or ""),
                encrypted=item.get("encrypted"),
            )
            for item in data
        ]

    async def get_library_info(self, repo_id: str | None) -> LibraryInfo:
        if self._creds.mode is TokenMode.repo:
            data = await self._get_json(
                self._url("/api/v2.1/via-repo-token/repo-info/")
            )
            return LibraryInfo(
                id=data.get("repo_id", ""),
                name=data.get("repo_name", ""),
                permission=data.get("permission"),
                size=data.get("size"),
            )
        rid = self._require_repo_id(repo_id)
        data = await self._get_json(self._url(f"/api2/repos/{rid}/"))
        return LibraryInfo(
            id=data.get("id", rid),
            name=data.get("name", ""),
            permission=data.get("permission"),
            size=data.get("size"),
            modified=str(data.get("mtime") or ""),
            encrypted=data.get("encrypted"),
        )

    # ------------------------------------------------------------------ #
    # browsing
    # ------------------------------------------------------------------ #

    async def list_directory(self, repo_id: str | None, path: str) -> list[DirEntry]:
        p = normalize_path(path)
        if self._creds.mode is TokenMode.repo:
            data = await self._get_json(
                self._url("/api/v2.1/via-repo-token/dir/"),
                params={"path": p},
            )
            items = data.get("dirent_list", data) if isinstance(data, dict) else data
        else:
            rid = self._require_repo_id(repo_id)
            items = await self._get_json(
                self._url(f"/api2/repos/{rid}/dir/"), params={"p": p}
            )

        entries: list[DirEntry] = []
        for item in items:
            name = item.get("name", "")
            kind = item.get("type", "")
            kind = "dir" if kind in ("dir", "folder") else "file"
            entries.append(
                DirEntry(
                    name=name,
                    path=posixpath.join(p, name),
                    type=kind,
                    size=item.get("size"),
                    modified=str(item.get("mtime") or item.get("last_modified") or ""),
                )
            )
        return entries

    async def get_file_info(self, repo_id: str | None, path: str) -> FileInfo:
        p = normalize_path(path)
        if self._creds.mode is TokenMode.repo:
            # No dedicated detail endpoint for library tokens; derive it from the
            # parent directory listing instead of pretending one exists.
            parent = posixpath.dirname(p) or "/"
            target = posixpath.basename(p)
            for entry in await self.list_directory(repo_id, parent):
                if entry.name == target:
                    return FileInfo(
                        name=entry.name,
                        path=p,
                        size=entry.size,
                        modified=entry.modified,
                    )
            raise SeafileAPIError(404, f"No such file: {p}")

        rid = self._require_repo_id(repo_id)
        data = await self._get_json(
            self._url(f"/api2/repos/{rid}/file/detail/"), params={"p": p}
        )
        return FileInfo(
            name=data.get("name", posixpath.basename(p)),
            path=p,
            size=data.get("size"),
            modified=str(data.get("mtime") or ""),
            id=data.get("id"),
        )

    async def get_download_link(self, repo_id: str | None, path: str) -> str:
        p = normalize_path(path)
        if self._creds.mode is TokenMode.repo:
            url = await self._get_json(
                self._url("/api/v2.1/via-repo-token/download-link/"),
                params={"path": p},
            )
        else:
            rid = self._require_repo_id(repo_id)
            url = await self._get_json(
                self._url(f"/api2/repos/{rid}/file/"), params={"p": p}
            )
        if isinstance(url, dict):
            url = url.get("download_link") or url.get("url") or ""
        return assert_same_host(str(url))

    async def read_file_bytes(self, repo_id: str | None, path: str) -> bytes:
        link = await self.get_download_link(repo_id, path)
        resp = await self._http.get(link)
        if resp.status_code >= 400:
            raise SeafileAPIError(resp.status_code, _short_body(resp))
        return resp.content

    # ------------------------------------------------------------------ #
    # writing
    # ------------------------------------------------------------------ #

    async def get_upload_link(self, repo_id: str | None, parent_dir: str = "/") -> str:
        p = normalize_path(parent_dir)
        if self._creds.mode is TokenMode.repo:
            url = await self._get_json(
                self._url("/api/v2.1/via-repo-token/upload-link/"), params={"p": p}
            )
        else:
            rid = self._require_repo_id(repo_id)
            url = await self._get_json(
                self._url(f"/api2/repos/{rid}/upload-link/"), params={"p": p}
            )
        if isinstance(url, dict):
            url = url.get("upload_link") or url.get("url") or ""
        return assert_same_host(str(url))

    async def upload_bytes(
        self,
        repo_id: str | None,
        parent_dir: str,
        filename: str,
        data: bytes,
        replace: bool = True,
    ) -> None:
        p = normalize_path(parent_dir)
        link = await self.get_upload_link(repo_id, p)
        form = {
            "parent_dir": p,
            "replace": "1" if replace else "0",
        }
        files = {"file": (filename, data)}
        resp = await self._http.post(
            link, data=form, files=files, headers=self._headers()
        )
        if resp.status_code >= 400:
            raise SeafileAPIError(resp.status_code, _short_body(resp))

    async def create_directory(self, repo_id: str | None, path: str) -> None:
        p = normalize_path(path)
        if self._creds.mode is TokenMode.repo:
            await self._request(
                "POST",
                self._url("/api/v2.1/via-repo-token/dir/"),
                params={"path": p},
                data={"operation": "mkdir"},
            )
            return
        rid = self._require_repo_id(repo_id)
        await self._request(
            "POST",
            self._url(f"/api2/repos/{rid}/dir/"),
            params={"p": p},
            data={"operation": "mkdir"},
        )

    async def rename(self, repo_id: str | None, path: str, new_name: str) -> str:
        p = normalize_path(path)
        if "/" in new_name:
            raise ValueError("new_name must be a bare name, not a path.")
        if self._creds.mode is TokenMode.repo:
            await self._request(
                "POST",
                self._url("/api/v2.1/via-repo-token/dir/"),
                params={"path": p},
                data={"operation": "rename", "newname": new_name},
            )
        else:
            rid = self._require_repo_id(repo_id)
            entry = await self._entry_kind(repo_id, p)
            endpoint = "dir" if entry == "dir" else "file"
            await self._request(
                "POST",
                self._url(f"/api2/repos/{rid}/{endpoint}/"),
                params={"p": p},
                data={"operation": "rename", "newname": new_name},
            )
        return posixpath.join(posixpath.dirname(p) or "/", new_name)

    async def move(
        self,
        repo_id: str | None,
        path: str,
        dst_dir: str,
        dst_repo_id: str | None = None,
    ) -> str:
        self._require_account("move")
        return await self._transfer("move", repo_id, path, dst_dir, dst_repo_id)

    async def copy(
        self,
        repo_id: str | None,
        path: str,
        dst_dir: str,
        dst_repo_id: str | None = None,
    ) -> str:
        self._require_account("copy")
        return await self._transfer("copy", repo_id, path, dst_dir, dst_repo_id)

    async def _transfer(
        self,
        operation: str,
        repo_id: str | None,
        path: str,
        dst_dir: str,
        dst_repo_id: str | None,
    ) -> str:
        rid = self._require_repo_id(repo_id)
        p = normalize_path(path)
        dst = normalize_path(dst_dir)
        kind = await self._entry_kind(repo_id, p)
        endpoint = "dir" if kind == "dir" else "file"
        await self._request(
            "POST",
            self._url(f"/api2/repos/{rid}/{endpoint}/"),
            params={"p": p},
            data={
                "operation": operation,
                "dst_repo": dst_repo_id or rid,
                "dst_dir": dst,
            },
        )
        return posixpath.join(dst, posixpath.basename(p))

    async def delete(self, repo_id: str | None, path: str) -> None:
        self._require_account("delete")
        rid = self._require_repo_id(repo_id)
        p = normalize_path(path)
        kind = await self._entry_kind(repo_id, p)
        endpoint = "dir" if kind == "dir" else "file"
        await self._request(
            "DELETE", self._url(f"/api2/repos/{rid}/{endpoint}/"), params={"p": p}
        )

    # ------------------------------------------------------------------ #
    # search
    # ------------------------------------------------------------------ #

    async def search(
        self, query: str, repo_id: str | None = None, limit: int = 25
    ) -> list[FileInfo]:
        self._require_account("search")
        params: dict[str, Any] = {"q": query, "per_page": limit}
        if repo_id:
            params["search_repo"] = repo_id
        data = await self._get_json(self._url("/api2/search/"), params=params)
        results = data.get("results", []) if isinstance(data, dict) else data
        return [
            FileInfo(
                name=item.get("name", ""),
                path=item.get("fullpath") or item.get("path", ""),
                size=item.get("size"),
                modified=str(item.get("last_modified") or ""),
                id=item.get("repo_id"),
            )
            for item in results[:limit]
        ]

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    async def _entry_kind(self, repo_id: str | None, path: str) -> str:
        """'dir' or 'file' — Seafile uses different endpoints for each."""
        p = normalize_path(path)
        if p == "/":
            return "dir"
        parent = posixpath.dirname(p) or "/"
        target = posixpath.basename(p)
        for entry in await self.list_directory(repo_id, parent):
            if entry.name == target:
                return entry.type
        raise SeafileAPIError(404, f"No such file or directory: {p}")

    async def count_items(self, repo_id: str | None, path: str) -> tuple[int, bool]:
        """Return (item count, is_directory), counting recursively.

        Used to refuse oversized deletes before issuing them.
        """
        kind = await self._entry_kind(repo_id, path)
        if kind != "dir":
            return 1, False

        total = 0
        stack = [normalize_path(path)]
        while stack:
            current = stack.pop()
            for entry in await self.list_directory(repo_id, current):
                total += 1
                if entry.type == "dir":
                    stack.append(entry.path)
                if total > get_settings().max_delete_items * 4:
                    # Far past any threshold we would accept; stop walking.
                    return total, True
        return total, True


def _short_body(resp: httpx.Response) -> str:
    try:
        text = resp.text
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"
    text = " ".join(text.split())
    return text[:300] if text else resp.reason_phrase


__all__ = ["SeafileClient", "normalize_path", "is_library_root", "quote"]
