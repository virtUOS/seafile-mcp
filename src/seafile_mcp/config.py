"""Deployment configuration.

Only SEAFILE_SERVER_URL is required. Everything else has a safe default; in
particular the server starts in ``safe_write`` mode, which never registers the
delete tool at all.
"""

from __future__ import annotations

import json
from enum import Enum
from urllib.parse import urlsplit
from functools import lru_cache
from typing import Annotated, Any

from pydantic import (
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
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


#: Defaults for the OCR settings. LightOnOCR's own recommendations, since it is
#: the model this was built against: a 1540 px long edge (2048 for the
#: LightOnOCR-3 0.8B/4B models, which an admin sets explicitly) and a
#: temperature of 0.1, which its authors prefer to greedy decoding.
DEFAULT_OCR_MAX_EDGE_PX = 1540
DEFAULT_OCR_TEMPERATURE = 0.1

#: Ceiling on SEAFILE_MCP_OCR_MAX_EDGE_PX. A rendered page is held in memory
#: for every request in flight, so this bounds what OCR can allocate at once:
#: 4096 x 4096 RGB is 48 MB, times SEAFILE_MCP_OCR_CONCURRENCY.
MAX_OCR_EDGE_PX = 4096

#: Request-body keys SEAFILE_MCP_OCR_EXTRA_BODY may not set, and where each one
#: comes from instead. model and the two sampling limits have settings of their
#: own; messages is the image this server sends; stream must stay off because
#: the response is parsed as one JSON document.
_OCR_RESERVED_KEYS = {
    "model": "SEAFILE_MCP_OCR_MODEL",
    "messages": "this server (it is the page image)",
    "stream": "this server (responses are read whole, never streamed)",
    "max_tokens": "SEAFILE_MCP_OCR_MAX_TOKENS",
    "temperature": "SEAFILE_MCP_OCR_TEMPERATURE",
}


#: What SEAFILE_MCP_DEBUG can name. Each area is one module's logger,
#: ``seafile_mcp.<area>``, so a module that wants debug lines needs only
#: ``logging.getLogger(__name__)`` — and its name added here.
DEBUG_AREAS = ("auth", "client", "ocr", "server")


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

    #: Same idea as pdf_preview_threshold_pages, for PowerPoint and
    #: OpenDocument (.odp) slides — one deck-length budget, not two.
    pptx_preview_threshold_slides: int | None = DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES

    #: Same idea, for Excel workbooks and OpenDocument (.ods)
    #: spreadsheets: above this many sheets, a bare call
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

    #: OCR, off unless both ocr_url and ocr_model are set. When on, the
    #: seafile_ocr_file tool is registered; when off it does not exist. The
    #: endpoint is any OpenAI-compatible chat-completions API serving a vision
    #: model — vLLM serving LightOnOCR is the reference. Deployment-fixed for
    #: the same reason server_url is: a caller who could choose it could send
    #: every page they can read to a host of their own.
    ocr_url: str | None = None
    ocr_model: str | None = None
    #: Sent as a Bearer token to the OCR service only. Never a Seafile
    #: credential, and never logged: SecretStr keeps it out of repr too.
    ocr_api_key: SecretStr | None = None
    #: Text sent alongside each page image. LightOnOCR takes none (an empty
    #: prompt is its plain transcription mode); olmOCR and general vision
    #: models need an instruction here.
    ocr_prompt: str = ""
    ocr_max_edge_px: int = DEFAULT_OCR_MAX_EDGE_PX
    #: Pages one seafile_ocr_file call may cover. Bounds GPU time per call and
    #: how long a client waits, since every page costs seconds.
    ocr_max_pages: int = 10
    #: OCR requests in flight across *every* user of this process, so one
    #: caller's long document cannot flood the OCR service.
    ocr_concurrency: int = 4
    #: Per page, not per call.
    ocr_timeout_s: float = 120.0
    #: Per page. Bounds a model that starts repeating itself; a page that
    #: reaches it is flagged in the result rather than silently cut.
    ocr_max_tokens: int = 4096
    ocr_temperature: float = DEFAULT_OCR_TEMPERATURE
    #: JSON object merged into every request body, for model-specific knobs
    #: (LightOnOCR-3 wants {"chat_template_kwargs": {"enable_thinking": false}}).
    #: NoDecode: parsed by _parse_ocr_extra_body, which can say what is wrong
    #: and lets an empty value mean "nothing".
    ocr_extra_body: Annotated[dict[str, Any], NoDecode] = Field(default_factory=dict)
    #: Don't send pages that look blank (the empty back of a duplex scan, a
    #: solid colour) to the OCR model. A heuristic that errs towards sending;
    #: this is its off switch. A caller can still force a page through.
    ocr_skip_blank_pages: bool = True

    #: Areas to log at DEBUG (see DEBUG_AREAS), comma-separated, or "all".
    #: Empty — the default, and the production setting — means none. Debug
    #: lines carry decisions, counts, sizes, timings and paths: never file
    #: content, never a transcription, never a token, key or download link.
    #: NoDecode: a plain comma-separated list, not JSON.
    debug: Annotated[tuple[str, ...], NoDecode] = ()

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

    @field_validator("ocr_url", "ocr_model", "ocr_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # docker-compose forwards every variable as ${VAR:-}, so a deployment
        # that never mentions OCR still passes empty strings for all of it.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("ocr_url")
    @classmethod
    def _check_ocr_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("SEAFILE_MCP_OCR_URL must start with http:// or https://")
        parts = urlsplit(v)
        if parts.username is not None or parts.password is not None:
            # A URL is logged wherever URLs are logged. The key has a setting
            # of its own, sent as a header and never written anywhere.
            raise ValueError(
                "SEAFILE_MCP_OCR_URL must not contain credentials; put the key in "
                "SEAFILE_MCP_OCR_API_KEY instead."
            )
        return v

    @field_validator("ocr_max_pages", "ocr_concurrency", "ocr_max_tokens")
    @classmethod
    def _validate_ocr_count(cls, v: int, info: ValidationInfo) -> int:
        if v < 1:
            raise ValueError(
                f"SEAFILE_MCP_{info.field_name.upper()} must be a positive integer"
            )
        return v

    @field_validator("ocr_max_edge_px")
    @classmethod
    def _validate_ocr_edge(cls, v: int) -> int:
        if v < 1:
            raise ValueError("SEAFILE_MCP_OCR_MAX_EDGE_PX must be a positive integer")
        if v > MAX_OCR_EDGE_PX:
            raise ValueError(
                f"SEAFILE_MCP_OCR_MAX_EDGE_PX must be at most {MAX_OCR_EDGE_PX}: "
                f"each page in flight is held in memory at that size"
            )
        return v

    @field_validator("ocr_timeout_s")
    @classmethod
    def _validate_ocr_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("SEAFILE_MCP_OCR_TIMEOUT_S must be greater than 0")
        return v

    @field_validator("ocr_temperature")
    @classmethod
    def _validate_ocr_temperature(cls, v: float) -> float:
        if v < 0:
            raise ValueError("SEAFILE_MCP_OCR_TEMPERATURE must not be negative")
        return v

    @field_validator("ocr_extra_body", mode="before")
    @classmethod
    def _parse_ocr_extra_body(cls, v: object) -> object:
        if isinstance(v, str):
            if not v.strip():
                return {}
            try:
                v = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"SEAFILE_MCP_OCR_EXTRA_BODY must be a JSON object, e.g. "
                    f'{{"top_p": 0.9}}: {exc}'
                ) from None
        if not isinstance(v, dict):
            raise ValueError(
                'SEAFILE_MCP_OCR_EXTRA_BODY must be a JSON object, e.g. {"top_p": 0.9}'
            )
        for key, owner in _OCR_RESERVED_KEYS.items():
            if key in v:
                raise ValueError(
                    f"SEAFILE_MCP_OCR_EXTRA_BODY may not set {key!r}; that comes "
                    f"from {owner}."
                )
        return v

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, v: object) -> object:
        if isinstance(v, str):
            v = tuple(a.strip().lower() for a in v.split(",") if a.strip())
        unknown = [a for a in v if a not in DEBUG_AREAS and a != "all"]  # type: ignore[union-attr]
        if unknown:
            # Refused rather than ignored: a typo would otherwise mean "no
            # logs", with nothing to say why.
            raise ValueError(
                f"SEAFILE_MCP_DEBUG names unknown area(s) {', '.join(unknown)}; "
                f"valid areas are {', '.join(DEBUG_AREAS)}, or all."
            )
        return v

    @model_validator(mode="after")
    def _ocr_needs_url_and_model(self) -> "Settings":
        # Refused rather than treated as "off": an admin who set one of the two
        # meant to turn OCR on, and a server that silently runs without it would
        # let them believe it is on.
        if (self.ocr_url is None) != (self.ocr_model is None):
            missing = "SEAFILE_MCP_OCR_MODEL" if self.ocr_model is None else "SEAFILE_MCP_OCR_URL"
            raise ValueError(
                f"OCR is half-configured: set {missing} as well, or unset both "
                f"SEAFILE_MCP_OCR_URL and SEAFILE_MCP_OCR_MODEL to run without OCR."
            )
        return self

    @property
    def ocr_enabled(self) -> bool:
        """True if seafile_ocr_file should be registered."""
        return self.ocr_url is not None

    def allows(self, tool_name: str) -> bool:
        """True if `tool_name` should be registered under the active mode."""
        every_mutating = MUTATING_TOOLS[Mode.full]
        if tool_name not in every_mutating:
            return True  # read-only tools are always available
        return tool_name in MUTATING_TOOLS[self.mode]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
