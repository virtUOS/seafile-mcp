"""Deployment configuration.

Only SEAFILE_SERVER_URL is required. Everything else has a safe default; in
particular the server starts in ``safe_write`` mode, which never registers the
delete tool at all.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .documents import (
    DEFAULT_MAX_IMAGE_EDGE_PX,
    DEFAULT_MAX_IMAGE_MB,
    DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES,
    DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES,
    DEFAULT_XLSX_PREVIEW_THRESHOLD_SHEETS,
)


def _parse_all_or_int(v: object) -> object:
    """Shared 'before' validator: "all" (any case/whitespace) -> None."""
    if isinstance(v, str) and v.strip().lower() == "all":
        return None
    return v


def _require_positive_or_none(v: int | None, env_name: str) -> int | None:
    """Shared 'after' validator: reject 0 or negative."""
    if v is not None and v < 1:
        raise ValueError(
            f"{env_name} must be a positive integer, or \"all\" to always "
            f"extract everything"
        )
    return v


class Mode(str, Enum):
    """Which tools this deployment is willing to expose.

    Tools outside the active tier are never registered, so a model cannot call
    what it cannot see. See docs/clients.md for the rationale.
    """

    read_only = "read_only"
    safe_write = "safe_write"
    full = "full"


#: Mutating tools permitted in each tier. ``read_only`` gets none of them.
MUTATING_TOOLS: dict[Mode, frozenset[str]] = {
    Mode.read_only: frozenset(),
    Mode.safe_write: frozenset(
        {
            "seafile_write_file",
            "seafile_upload_file",
            "seafile_create_directory",
            "seafile_rename",
            "seafile_move",
            "seafile_copy",
        }
    ),
    Mode.full: frozenset(
        {
            "seafile_write_file",
            "seafile_upload_file",
            "seafile_create_directory",
            "seafile_rename",
            "seafile_move",
            "seafile_copy",
            "seafile_delete",
        }
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEAFILE_MCP_",
        env_file=".env",
        extra="ignore",
    )

    #: Deployment-fixed upstream. Never user-supplied over HTTP: allowing a
    #: caller to choose the upstream would let an attacker harvest tokens by
    #: pointing us at a host they control.
    server_url: str = Field(alias="SEAFILE_SERVER_URL")

    mode: Mode = Mode.safe_write

    #: Credential for stdio transport only (single-user, from the client's own
    #: config). Ignored over HTTP, where the token arrives per request.
    api_token: str | None = Field(default=None, alias="SEAFILE_API_TOKEN")

    max_upload_mb: int = 50
    max_file_read_kb: int = 512
    max_delete_items: int = 50

    #: Hard ceiling on how many bytes of one file this server will hold in
    #: memory. Distinct from max_file_read_kb, which caps how much *text* comes
    #: back and says nothing about the size of the file behind it: before this
    #: existed, a 2 GB file was downloaded in full and then sliced to 512 KB.
    #: One process serves every user, so this is a safety limit rather than a
    #: tuning knob.
    #:
    #: A file over the limit is not simply refused. Text is returned as a
    #: prefix, flagged truncated; only formats that cannot be parsed from a
    #: prefix at all (PDF, .docx/.xlsx/.pptx) raise, because handing back part
    #: of one produces a parse error rather than partial content.
    max_download_mb: int = 30

    #: A bare seafile_read_file call on a PDF longer than this extracts only
    #: a short preview instead of the whole document (see documents.py). Set
    #: to "all" to always extract the whole document regardless of length.
    pdf_preview_threshold_pages: int | None = DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES

    #: Same idea as pdf_preview_threshold_pages, for PowerPoint slides.
    pptx_preview_threshold_slides: int | None = DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES

    #: Same idea, for Excel workbooks: above this many sheets, a bare call
    #: extracts only the first sheet instead of all of them. Set to "all" to
    #: always extract every sheet regardless of count.
    xlsx_preview_threshold_sheets: int | None = DEFAULT_XLSX_PREVIEW_THRESHOLD_SHEETS

    #: Long edge, in pixels, an image is scaled down to before it is sent as an
    #: image content block. Mainstream vision models downsample anything larger
    #: than roughly this themselves, so sending more costs upload bytes and the
    #: model's tool-output budget without buying detail it can use. A smaller
    #: image is never enlarged to meet it.
    max_image_edge_px: int = DEFAULT_MAX_IMAGE_EDGE_PX

    #: Ceiling on the re-encoded image, before base64 inflates it by a third.
    #: Over it, render_image lowers JPEG quality and then the long edge until it
    #: fits, so this is a guarantee rather than a hope. Distinct from
    #: max_download_mb, which bounds what this server holds: this one bounds
    #: what lands in the model's context, since a tool result goes straight
    #: there.
    max_image_mb: int = DEFAULT_MAX_IMAGE_MB

    #: Whether seafile_read_file may answer with an image content block at all.
    #: Turned off, an image comes back as an ordinary FileContent naming its
    #: format and dimensions and pointing at seafile_get_download_link. That is
    #: the better answer for a deployment whose client cannot render image
    #: blocks, or whose model has no vision: the alternative is a block the host
    #: silently drops, leaving the model with a notice about an image it cannot
    #: see and no way to tell that is what happened.
    image_reads: bool = True

    #: If set, account-mode users outside these domains are rejected.
    #: NoDecode: this is a plain comma-separated list, not JSON.
    allowed_email_domains: Annotated[tuple[str, ...], NoDecode] = ()

    #: Mutating calls per token per minute.
    mutation_rate_limit: int = 30

    http_host: str = "0.0.0.0"
    http_port: int = 8000
    http_path: str = "/mcp"

    request_timeout_s: float = 30.0

    @field_validator("server_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("SEAFILE_SERVER_URL must start with http:// or https://")
        return v

    @field_validator("allowed_email_domains", mode="before")
    @classmethod
    def _split_domains(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(d.strip().lower() for d in v.split(",") if d.strip())
        return v

    @field_validator(
        "pdf_preview_threshold_pages",
        "pptx_preview_threshold_slides",
        "xlsx_preview_threshold_sheets",
        mode="before",
    )
    @classmethod
    def _parse_preview_threshold(cls, v: object) -> object:
        return _parse_all_or_int(v)

    @field_validator(
        "pdf_preview_threshold_pages",
        "pptx_preview_threshold_slides",
        "xlsx_preview_threshold_sheets",
    )
    @classmethod
    def _validate_preview_threshold(cls, v: int | None, info: ValidationInfo) -> int | None:
        return _require_positive_or_none(v, f"SEAFILE_MCP_{info.field_name.upper()}")

    @field_validator("max_image_edge_px", "max_image_mb")
    @classmethod
    def _validate_image_limit(cls, v: int, info: ValidationInfo) -> int:
        # Deliberately not folded into _require_positive_or_none: that one's
        # message offers "all" as an alternative, which is meaningless here.
        if v < 1:
            raise ValueError(
                f"SEAFILE_MCP_{info.field_name.upper()} must be a positive integer"
            )
        return v

    def allows(self, tool_name: str) -> bool:
        """True if `tool_name` should be registered under the active mode."""
        every_mutating = MUTATING_TOOLS[Mode.full]
        if tool_name not in every_mutating:
            return True  # read-only tools are always available
        return tool_name in MUTATING_TOOLS[self.mode]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
