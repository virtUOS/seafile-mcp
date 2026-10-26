"""OCR: transcribing scanned PDF pages and images with a separate model.

OCR exists only when an admin has pointed the server at an OCR model
(``SEAFILE_MCP_OCR_URL``/``SEAFILE_MCP_OCR_MODEL``). Then ``seafile_read_file``
transcribes a PDF's scanned pages in the same call that reads its typed ones,
so one call is always enough, and ``seafile_ocr_file`` is registered for
direct use: an image's text, a garbled text layer, a file the agent already
knows is a scan. Both go through :class:`OcrService`. The model is reached over
the OpenAI-compatible chat-completions API, which is what vLLM, SGLang, Ollama
and LM Studio all serve, so LightOnOCR, olmOCR, dots.ocr, DeepSeek-OCR or a
general vision model are a configuration change rather than a code change. A
different protocol would be a second :class:`OcrEngine`; nothing else here
would change.

Five things are load-bearing:

**The OCR service gets its own HTTP client.** It is never the Seafile client,
so a Seafile token cannot reach the OCR host by any path: nothing here ever
holds one. The only credential sent is the deployment's own OCR API key.

**Pages with a text layer, and blank pages, are not sent.** OCR costs GPU
time and seconds per page; a typed page is already exact. ``documents.
has_text_layer`` decides it in both tools, so they always send the same pages.
``_looks_blank`` keeps the empty backs of duplex scans away from the model,
erring towards sending. seafile_ocr_file can force both through, for a
copier's "searchable PDF" whose hidden text is junk.

**seafile_read_file never fails because OCR did.** A failed page is a result,
marked in the text; the typed pages around it still come back.

**PDFium is not thread-safe**, not even across different documents, and
pypdfium2 adds no lock of its own. Every PDFium call — open, render, close —
holds ``_PDFIUM_LOCK``, and everything PDFium allocated is closed explicitly
under it, so no garbage-collector finalizer runs a PDFium call later from
whichever thread happens to trigger it. Removing the lock "for speed" corrupts
the heap the first time two users render at once.

**Nothing is cached and no transcription is logged.** A transcription is
library content; the only per-process state is the semaphore and the outage
cool-down in :class:`OcrService`, which hold none. Calling again on the same
page transcribes it again.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from io import BytesIO
from typing import Any, NamedTuple, Protocol

import httpx
import pypdfium2 as pdfium
from PIL import Image as PILImage
from PIL import ImageChops, ImageFilter

from . import documents
from .config import Settings
from .models import SeafileMCPError

logger = logging.getLogger(__name__)


class OcrError(SeafileMCPError):
    """The OCR service failed or could not be reached.

    Messages go to the model, so they never name the OCR host: its address is
    internal infrastructure, and the details are in the server log instead.
    """


class OcrUnavailable(OcrError):
    """The OCR service as a whole is down, hung or overloaded — not one page.

    Timeouts, refused or dropped connections, and 429/502/503/504. Unlike a
    page-level failure it says the next page will fail the same way, so
    :class:`OcrService` stops sending for the rest of the call and for a short
    cool-down after it.
    """


#: HTTP statuses that describe the service, not the page that was sent.
_OUTAGE_STATUSES = frozenset({429, 502, 503, 504})


class OcrPage(NamedTuple):
    """What the OCR model made of one page."""

    text: str
    #: The model stopped because it reached max_tokens, not because the page
    #: ended: the end may be missing, or the model may have been looping.
    hit_token_limit: bool = False
    #: From the response's ``usage``, when the server reports it. The prompt
    #: count is mostly the page image — what has to fit vLLM's --max-model-len
    #: alongside max_tokens. For the debug trail only.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


class OcrEngine(Protocol):
    """Anything that can turn one page image into text."""

    async def recognize(self, png: bytes) -> OcrPage: ...


#: Called after each page sent to the engine, with (pages done, pages to do).
Progress = Callable[[int, int], Awaitable[None]]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

_client: httpx.AsyncClient | None = None

#: Connecting is quick or it is not going to happen; only the answer is slow.
_CONNECT_TIMEOUT_S = 10.0

_UNREADABLE = "The OCR service returned an answer this server could not read."


def get_ocr_client() -> httpx.AsyncClient:
    """The OCR service's own client. Never the Seafile one; see module doc.

    ``follow_redirects=False`` for the same reason as in ``_http.py``: a
    redirect would send a page image of someone's file wherever it pointed.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            follow_redirects=False, headers={"User-Agent": "seafile-mcp"}
        )
    return _client


async def close_ocr_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _auth_headers(settings: Settings) -> dict[str, str]:
    if settings.ocr_api_key is None:
        return {}
    return {"Authorization": f"Bearer {settings.ocr_api_key.get_secret_value()}"}


class OpenAICompatibleOcr:
    """OCR through ``POST {ocr_url}/chat/completions`` with one image per request.

    One page per request because that is what OCR models are trained on, and
    because vLLM batches concurrent requests itself: many single-page requests
    in flight is its fast path, not a multi-image prompt.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.ocr_url is None or settings.ocr_model is None:
            raise ValueError("OCR is not configured on this server.")
        self._url = f"{settings.ocr_url}/chat/completions"
        self._model = settings.ocr_model
        self._prompt = settings.ocr_prompt
        self._temperature = settings.ocr_temperature
        self._max_tokens = settings.ocr_max_tokens
        self._extra_body = dict(settings.ocr_extra_body)
        self._timeout = settings.ocr_timeout_s
        self._headers = _auth_headers(settings)

    def _body(self, png: bytes) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,"
                    + base64.b64encode(png).decode("ascii")
                },
            }
        ]
        # No text part at all when there is no prompt: that is LightOnOCR's
        # plain transcription mode, and an empty text part is not the same
        # thing to every chat template.
        if self._prompt:
            content.append({"type": "text", "text": self._prompt})
        # extra_body first, so the keys this server owns always win. The config
        # validator already refuses those keys; this is the second lock.
        return {
            **self._extra_body,
            "model": self._model,
            "messages": [{"role": "user", "content": content}],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }

    async def recognize(self, png: bytes) -> OcrPage:
        timeout = httpx.Timeout(
            self._timeout, connect=min(_CONNECT_TIMEOUT_S, self._timeout)
        )
        try:
            resp = await get_ocr_client().post(
                self._url, json=self._body(png), headers=self._headers, timeout=timeout
            )
        except httpx.ConnectTimeout:
            logger.warning("OCR service did not accept a connection in time")
            raise OcrUnavailable(
                "The OCR service is unreachable right now; try again later."
            ) from None
        except httpx.TimeoutException:
            logger.warning("OCR request timed out after %.0f s", self._timeout)
            raise OcrUnavailable(
                f"The OCR service did not answer within {self._timeout:.0f} s."
            ) from None
        except httpx.HTTPError as exc:
            logger.warning("OCR request failed: %s: %s", exc.__class__.__name__, exc)
            raise OcrUnavailable(
                "The OCR service is unreachable right now; try again later."
            ) from None

        if resp.status_code != 200:
            # Its message goes to the log and never to the model: it is the OCR
            # server talking about itself. Never the raw body — see
            # _error_message.
            logger.warning(
                "OCR service returned HTTP %s: %s", resp.status_code, _error_message(resp)
            )
            error = (
                OcrUnavailable if resp.status_code in _OUTAGE_STATUSES else OcrError
            )
            raise error(f"The OCR service returned HTTP {resp.status_code}.")

        try:
            body = resp.json()
            choice = body["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            logger.warning(
                "OCR service returned an unexpected body (%d bytes)", len(resp.content)
            )
            raise OcrError(_UNREADABLE) from None

        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            raise OcrError(_UNREADABLE)
        return OcrPage(
            text.strip(),
            finish_reason == "length",
            _count(usage, "prompt_tokens"),
            _count(usage, "completion_tokens"),
            finish_reason if isinstance(finish_reason, str) else None,
        )


#: A data: URL, or any long unbroken run of base64 — how a page image looks
#: when an error message quotes the request back.
_ECHOED_DATA = re.compile(r"data:[^\s'\",]+|[A-Za-z0-9+/=]{32,}")


def _error_message(resp: httpx.Response) -> str:
    """An OCR server's own one-line explanation of an error, fit for a log.

    Never its raw body: a validation error can echo the request, and the
    request is a page image of someone's file. Takes the human message from
    the usual shapes — OpenAI/vLLM ``message``, ``error.message``, FastAPI
    ``detail`` (a pydantic ``msg``, without its ``input``) — and blanks out
    anything that still looks like image data.
    """
    try:
        body = resp.json()
    except ValueError:
        return "(no error message in the response)"
    message: object = None
    if isinstance(body, dict):
        error = body.get("error")
        detail = body.get("detail")
        message = body.get("message")
        if message is None and isinstance(error, dict):
            message = error.get("message")
        if message is None and isinstance(error, str):
            message = error
        if message is None and isinstance(detail, str):
            message = detail
        if message is None and isinstance(detail, list) and detail:
            first = detail[0]
            message = first.get("msg") if isinstance(first, dict) else None
    if not isinstance(message, str):
        return "(no error message in the response)"
    return _ECHOED_DATA.sub("<data>", message)[:200]


def _count(usage: object, key: str) -> int | None:
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) else None


async def probe_service(settings: Settings) -> str:
    """Check once, at startup, that the OCR service answers and serves the model.

    Warning-only: it never decides whether seafile_ocr_file is registered.
    vLLM takes minutes to load a model, so an OCR host still starting when this
    server boots would otherwise cost OCR until the next restart. Returns
    "ok", "model_missing", "unreachable" or "error", for the startup log.
    """
    try:
        resp = await get_ocr_client().get(
            f"{settings.ocr_url}/models", headers=_auth_headers(settings), timeout=5.0
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "OCR service did not answer at startup (%s). seafile_ocr_file is "
            "registered anyway and will report the service unavailable until it "
            "answers.",
            exc.__class__.__name__,
        )
        return "unreachable"
    if resp.status_code != 200:
        logger.warning(
            "OCR service answered HTTP %s to GET /models at startup; check "
            "SEAFILE_MCP_OCR_URL and SEAFILE_MCP_OCR_API_KEY.",
            resp.status_code,
        )
        return "error"
    try:
        served = [str(m["id"]) for m in resp.json()["data"]]
    except (ValueError, KeyError, TypeError):
        logger.warning("OCR service's GET /models answer is not an OpenAI model list.")
        return "error"
    if settings.ocr_model not in served:
        logger.warning(
            "OCR service does not serve model %r (it serves: %s); OCR calls will "
            "fail until SEAFILE_MCP_OCR_MODEL matches.",
            settings.ocr_model,
            ", ".join(served[:10]) or "nothing",
        )
        return "model_missing"
    logger.info("OCR service reachable and serving %s.", settings.ocr_model)
    return "ok"


# --------------------------------------------------------------------------- #
# Pages: what gets sent
# --------------------------------------------------------------------------- #

_PDFIUM_LOCK = threading.Lock()

#: Levels a pixel must differ from the page's background by to count as ink.
_INK_DELTA = 64

#: A page is blank if fewer ink pixels than this touch another ink pixel.
#: Isolated pixels are scanner speckle and are not counted at all; strokes are
#: connected, so text keeps essentially every pixel. Measured at a 1540 px
#: edge: an 8 pt "Ja" has 60 such pixels, a lone default-font "3" 22, while
#: 600 random specks leave 2. Set low on purpose — skipping a page with a word
#: on it loses content, sending an empty one only wastes a request.
_BLANK_MAX_INK = 16


def _ink_count(img: PILImage.Image) -> int:
    """Ink pixels that touch other ink: the blank check's measure.

    The background is the most common grey level rather than assumed white, so
    an inverted slide — light text on dark — is judged the same way as paper.
    Pure Pillow, all in C: about 20 ms for a 1540 x 2178 page.
    """
    grey = img.convert("L")
    histogram = grey.histogram()
    background = max(range(256), key=histogram.__getitem__)
    ink = grey.point(lambda v: 255 if abs(v - background) > _INK_DELTA else 0)
    # The 3x3 mean of the ink mask is 255/9 (28) for a lone pixel and at
    # least 2*255/9 (57) for one with an ink neighbour; 42 splits them.
    supported = ink.filter(ImageFilter.BoxBlur(1)).point(lambda v: 255 if v >= 42 else 0)
    return ImageChops.multiply(ink, supported).histogram()[255]


def _looks_blank(img: PILImage.Image) -> bool:
    """True if a page has nothing on it worth sending to an OCR model."""
    return _ink_count(img) < _BLANK_MAX_INK


def _png(img: PILImage.Image) -> bytes:
    # PNG rather than JPEG: lossless, so no ringing around small print, and
    # the OCR service is a local network hop, not a model's context window.
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class Rendered(NamedTuple):
    """A page ready to send, or judged blank and not encoded at all."""

    png: bytes
    blank: bool
    width: int
    height: int
    #: _ink_count, or -1 when neither the blank check nor the debug trail
    #: needed it, so it was not measured.
    ink: int


def _encode(img: PILImage.Image, check_blank: bool) -> Rendered:
    """Encode a page, unless it is blank — a blank page is never sent."""
    measure = check_blank or logger.isEnabledFor(logging.DEBUG)
    ink = _ink_count(img) if measure else -1
    if check_blank and ink < _BLANK_MAX_INK:
        return Rendered(b"", True, img.width, img.height, ink)
    return Rendered(_png(img), False, img.width, img.height, ink)


class _PdfPages:
    """A PDF as pages: text layers through pypdf, which is what
    seafile_read_file reads, and pixels through PDFium, which pypdf cannot
    produce."""

    noun = "PDF"
    source = "PDF"
    animation_frames = 1

    def __init__(self, data: bytes, max_edge: int) -> None:
        self._data = data
        self._max_edge = max_edge
        self._reader = documents.open_pdf(data)
        self.count = len(self._reader.pages)
        self._doc: pdfium.PdfDocument | None = None
        self._closed = False

    def text_layers(self, first: int, last: int) -> dict[int, str]:
        found: dict[int, str] = {}
        for n in range(first, last + 1):
            try:
                text = self._reader.pages[n - 1].extract_text()
            except Exception:
                # A page pypdf cannot read text from is, for this purpose, a
                # page without a usable text layer — which is exactly what
                # OCR is for. Damaged pages are common in scans.
                continue
            if documents.has_text_layer(text):
                found[n] = text
        return found

    def render(self, n: int, check_blank: bool) -> Rendered:
        """Render page ``n`` (1-indexed) so its long edge is ``max_edge``.

        A PDF page is vector, so unlike an image it is rendered *at* the size
        the OCR model wants rather than shrunk to it. ``get_size`` already
        accounts for /Rotate, and the long edge is the same either way.
        """
        with _PDFIUM_LOCK:
            if self._closed:
                # A render queued in a worker thread can outlive a cancelled
                # call. Reopening here would leave a document nothing closes,
                # for a GC finalizer to close later outside this lock.
                raise ValueError("This PDF was closed before the page was rendered.")
            try:
                if self._doc is None:
                    self._doc = pdfium.PdfDocument(self._data)
                page = self._doc[n - 1]
                try:
                    width, height = page.get_size()
                    if max(width, height) <= 0:
                        raise ValueError(f"Page {n} of this PDF has no size.")
                    bitmap = page.render(scale=self._max_edge / max(width, height))
                    try:
                        # convert() always copies, so nothing below still points
                        # into a buffer PDFium is about to free.
                        img = bitmap.to_pil().convert("RGB")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            except pdfium.PdfiumError as exc:
                raise ValueError(f"Could not render this PDF: {exc}") from None
        return _encode(img, check_blank)

    def close(self) -> None:
        with _PDFIUM_LOCK:
            self._closed = True
            if self._doc is not None:
                self._doc.close()
                self._doc = None


class _ImagePages:
    """An image as pages: a TIFF's frames are the pages of a multi-page scan;
    any other format is one page, animated or not."""

    noun = "image"

    def __init__(self, data: bytes, max_edge: int) -> None:
        self._data = data
        self._max_edge = max_edge
        self.source, frames = documents.identify_image(data)
        paged = self.source == "TIFF"
        self.count = frames if paged else 1
        self.animation_frames = 1 if paged else frames

    def text_layers(self, first: int, last: int) -> dict[int, str]:
        return {}

    def render(self, n: int, check_blank: bool) -> Rendered:
        """Decode frame ``n`` with every check render_image has, then shrink it
        to ``max_edge`` (never enlarging it) and flatten it onto white."""
        img = documents.decode_image(self._data, self._max_edge, frame=n - 1).image
        if max(img.size) > self._max_edge:
            img.thumbnail((self._max_edge, self._max_edge), PILImage.Resampling.LANCZOS)
        if img.mode == "RGBA":
            # Onto white, not black: dark text on a transparent background is
            # the usual screenshot, and on black it would vanish.
            flat = PILImage.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, mask=img.getchannel("A"))
            img = flat
        elif img.mode != "RGB":
            img = img.convert("RGB")
        return _encode(img, check_blank)

    def close(self) -> None:
        pass


def _resolve_range(
    total: int, start: int | None, end: int | None, max_pages: int, noun: str
) -> tuple[int, int, bool]:
    """(first, last, clamped). A bare call starts at page 1; any range is cut
    to ``max_pages``, and the notice then says where to continue."""
    first = start or 1
    if first > total:
        raise ValueError(
            f"This {noun} has {total} page(s); start_page={first} is past the end."
        )
    last = min(end, total) if end is not None else total
    if last - first + 1 > max_pages:
        return first, first + max_pages - 1, True
    return first, last, False


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


class PageResult(NamedTuple):
    """What became of one page that had no text layer."""

    #: "ocr" (transcribed), "blank" (looked empty, not sent) or "failed".
    status: str
    text: str = ""
    hit_token_limit: bool = False
    reason: str = ""


class OcrResult(NamedTuple):
    text: str
    #: "PDF", or the image format ("TIFF", "PNG", ...).
    source: str
    page_count: int
    first_page: int
    last_page: int
    #: The requested range was longer than max_pages and was cut short.
    clamped: bool
    max_pages: int
    #: Transcribed by the OCR model (including no-text and token-limited ones).
    ocr_pages: tuple[int, ...]
    #: Taken from the PDF's own text layer; never sent to the model.
    text_layer_pages: tuple[int, ...]
    failed_pages: tuple[tuple[int, str], ...]
    token_limited_pages: tuple[int, ...]
    #: Transcribed, but the model found no text on them.
    no_text_pages: tuple[int, ...]
    #: Looked blank, so never sent.
    blank_pages: tuple[int, ...]
    #: More than 1 for an animated (non-TIFF) image, of which only the first
    #: frame was transcribed.
    animation_frames: int
    #: False only for a single-page image, whose text needs no page label.
    labelled: bool
    #: Every page sent towards the model, and what became of it.
    page_results: dict[int, PageResult]


#: After an outage, how long no page is sent at all. Long enough that a read
#: during an outage costs one timeout and the reads after it cost none; short
#: enough that a restarted OCR server is back in use within a minute.
_OUTAGE_COOLDOWN_S = 60.0


class OcrService:
    """OCR for both tools: an engine, the deployment's limits, the one
    semaphore every caller of this process shares, and its outage cool-down.

    ``transcribe`` serves seafile_ocr_file and raises when nothing could be
    read. ``transcribe_pdf_pages`` serves seafile_read_file and never raises
    over an OCR failure: a read must not fail because OCR did.
    """

    def __init__(self, engine: OcrEngine, settings: Settings) -> None:
        self.engine = engine
        self.max_edge = settings.ocr_max_edge_px
        self.max_pages = settings.ocr_max_pages
        self.skip_blank = settings.ocr_skip_blank_pages
        # Process-wide, not per call: it is what stops one user's long document
        # from occupying the OCR service while everyone else waits. Waiters are
        # woken in arrival order, so concurrent users interleave.
        self._slots = asyncio.Semaphore(settings.ocr_concurrency)
        # Process-wide too, and for the same reason: one user's timeout should
        # spare the next user the same wait. It holds when OCR may be tried
        # again and the service's own error message — no user, no content.
        self._clock = time.monotonic
        self._unavailable_until = 0.0
        self._unavailable_why = ""

    def _outage_reason(self) -> str | None:
        """Why a page is not being sent right now, or None if it may be."""
        remaining = self._unavailable_until - self._clock()
        if remaining <= 0:
            return None
        return (
            f"OCR is temporarily unavailable ({self._unavailable_why}), so this "
            f"page was not sent. Try again in about {math.ceil(remaining)} s."
        )

    def _enter_outage(self, why: str) -> None:
        if self._outage_reason() is None:
            logger.warning(
                "OCR service unavailable (%s); sending no OCR requests for %.0f s.",
                why,
                _OUTAGE_COOLDOWN_S,
            )
        self._unavailable_until = self._clock() + _OUTAGE_COOLDOWN_S
        self._unavailable_why = why

    async def _run(
        self,
        pages: _PdfPages | _ImagePages,
        numbers: list[int],
        check_blank: bool,
        progress: Progress | None,
        label: str,
    ) -> dict[int, PageResult]:
        """Render and transcribe exactly ``numbers``, concurrently.

        A failed page is a result, not an exception, so the others survive it:
        nothing is cached, and discarding nine good pages over one timeout
        would make the caller pay for all ten again. A bug or a cancellation
        is re-raised rather than mistaken for a failed page.

        An outage is different: it says every later page would fail the same
        way, after its own full timeout. So the first page to meet one starts
        the cool-down (:meth:`_enter_outage`), and from then on no page is sent
        — not the rest of this call's, not anyone else's. A read during an
        outage costs at most the requests already in flight, one timeout, and
        reads after it none.

        Every step is in the debug trail (SEAFILE_MCP_DEBUG=ocr): counts,
        sizes and timings, never the text that came back.
        """
        done = 0

        async def one(n: int) -> PageResult:
            nonlocal done
            queued = time.monotonic()
            try:
                # Rendered inside the semaphore, so no more pages exist in
                # memory at once than there are requests in flight.
                async with self._slots:
                    started = time.monotonic()
                    try:
                        page_image = await asyncio.to_thread(pages.render, n, check_blank)
                    except ValueError as exc:
                        logger.debug("%s p%d: could not be rendered: %s", label, n, exc)
                        raise
                    logger.debug(
                        "%s p%d: rendered %dx%d in %.2fs (waited %.2fs for a slot), "
                        "ink=%s, png=%s",
                        label,
                        n,
                        page_image.width,
                        page_image.height,
                        time.monotonic() - started,
                        started - queued,
                        page_image.ink if page_image.ink >= 0 else "not measured",
                        "not encoded" if page_image.blank else f"{len(page_image.png)} bytes",
                    )
                    if page_image.blank:
                        # Local, so still true while the service is down.
                        logger.debug(
                            "%s p%d: looks blank (ink %d < %d), not sent",
                            label, n, page_image.ink, _BLANK_MAX_INK,
                        )
                        return PageResult("blank")
                    # Checked after waiting for a slot: meanwhile another page,
                    # of this call or anyone's, may have found the service down.
                    skip = self._outage_reason()
                    if skip is not None:
                        logger.debug("%s p%d: not sent, cool-down active: %s", label, n, skip)
                        return PageResult("failed", reason=skip)
                    sent = time.monotonic()
                    try:
                        page = await self.engine.recognize(page_image.png)
                    except OcrError as exc:
                        outage = isinstance(exc, OcrUnavailable)
                        logger.debug(
                            "%s p%d: failed after %.2fs: %s (%s)",
                            label, n, time.monotonic() - sent, exc,
                            "outage" if outage else "this page only",
                        )
                        if outage:
                            self._enter_outage(str(exc))
                        raise
                    logger.debug(
                        "%s p%d: transcribed %d chars in %.2fs, finish=%s, "
                        "tokens prompt=%s completion=%s",
                        label,
                        n,
                        len(page.text),
                        time.monotonic() - sent,
                        page.finish_reason or "?",
                        page.prompt_tokens if page.prompt_tokens is not None else "?",
                        page.completion_tokens if page.completion_tokens is not None else "?",
                    )
                    return PageResult("ocr", page.text, page.hit_token_limit)
            finally:
                done += 1
                if progress is not None:
                    await progress(done, len(numbers))

        outcomes = await asyncio.gather(*(one(n) for n in numbers), return_exceptions=True)
        results: dict[int, PageResult] = {}
        for n, outcome in zip(numbers, outcomes):
            if isinstance(outcome, (OcrError, ValueError)):
                results[n] = PageResult("failed", reason=str(outcome))
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results[n] = outcome
        return results

    async def transcribe(
        self,
        data: bytes,
        start_page: int | None,
        end_page: int | None,
        force_ocr: bool,
        progress: Progress | None = None,
        *,
        label: str = "file",
    ) -> OcrResult:
        """Transcribe a PDF's or an image's pages, for seafile_ocr_file.

        Pages with a text layer, and pages that look blank, are not sent
        unless ``force_ocr``. Raises ValueError for a file that cannot be read
        at all, and OcrError if every page sent failed and nothing else is
        left to show.
        """
        if documents.is_pdf(data):
            pages: _PdfPages | _ImagePages = await asyncio.to_thread(
                _PdfPages, data, self.max_edge
            )
        elif documents.is_image(data):
            pages = await asyncio.to_thread(_ImagePages, data, self.max_edge)
        else:
            raise ValueError("OCR reads PDFs and images only.")

        try:
            first, last, clamped = _resolve_range(
                pages.count, start_page, end_page, self.max_pages, pages.noun
            )
            text_layers = (
                {} if force_ocr else await asyncio.to_thread(pages.text_layers, first, last)
            )
            wanted = [n for n in range(first, last + 1) if n not in text_layers]
            logger.debug(
                "%s: pages %d-%d of %d%s; text layer on %s; sending %s%s",
                label,
                first,
                last,
                pages.count,
                " (cut to the per-call cap)" if clamped else "",
                pages_or_none(sorted(text_layers)),
                pages_or_none(wanted),
                " (force_ocr)" if force_ocr else "",
            )
            results = await self._run(
                pages, wanted, self.skip_blank and not force_ocr, progress, label
            )
        finally:
            await asyncio.to_thread(pages.close)

        failed = [(n, r.reason) for n, r in sorted(results.items()) if r.status == "failed"]
        if failed and len(failed) == len(results) and not text_layers:
            raise OcrError(f"OCR failed for every page. {failed[0][1]}")

        labelled = pages.noun == "PDF" or pages.count > 1
        transcribed = {n: r for n, r in sorted(results.items()) if r.status == "ocr"}
        return OcrResult(
            text=_compose(first, last, text_layers, results, (), labelled),
            source=pages.source,
            page_count=pages.count,
            first_page=first,
            last_page=last,
            clamped=clamped,
            max_pages=self.max_pages,
            ocr_pages=tuple(transcribed),
            text_layer_pages=tuple(sorted(text_layers)),
            failed_pages=tuple(failed),
            token_limited_pages=tuple(n for n, r in transcribed.items() if r.hit_token_limit),
            no_text_pages=tuple(n for n, r in transcribed.items() if not r.text),
            blank_pages=tuple(n for n, r in sorted(results.items()) if r.status == "blank"),
            animation_frames=pages.animation_frames,
            labelled=labelled,
            page_results=results,
        )

    async def transcribe_pdf_pages(
        self,
        data: bytes,
        numbers: list[int],
        progress: Progress | None = None,
        *,
        label: str = "file",
    ) -> dict[int, PageResult]:
        """Transcribe exactly these pages of a PDF, for seafile_read_file.

        The caller has already decided which pages lack a text layer, with the
        same test seafile_ocr_file uses, and capped how many. Never raises over
        an OCR failure; each page's fate is in its result.
        """
        if not numbers:
            return {}
        pages = await asyncio.to_thread(_PdfPages, data, self.max_edge)
        try:
            return await self._run(pages, numbers, self.skip_blank, progress, label)
        finally:
            await asyncio.to_thread(pages.close)


def pages_or_none(pages: list[int]) -> str:
    """pages_phrase for the debug trail, which also has to say "none"."""
    return documents.pages_phrase(pages) if pages else "no page"


def _compose(
    first: int,
    last: int,
    text_layers: dict[int, str],
    results: dict[int, PageResult],
    deferred: Iterable[int],
    labelled: bool,
) -> str:
    """The page-ordered text, in the one marker vocabulary both tools share.

    An unmarked ``[page N]`` is always a text layer, exact, as seafile_read_file
    has always labelled it; anything that is not exact carries a marker.
    """
    later = set(deferred)
    parts: list[str] = []
    for n in range(first, last + 1):
        if n in text_layers:
            parts.append(f"[page {n}]\n{text_layers[n]}")
        elif n in later:
            parts.append(f"[page {n}] (no text layer; not transcribed in this call)")
        else:
            r = results[n]
            if r.status == "ocr":
                body = r.text or "(no text recognised on this page)"
                parts.append(f"[page {n}] (OCR)\n{body}" if labelled else body)
            elif r.status == "blank":
                parts.append(
                    f"[page {n}] (appears blank; not sent to OCR)"
                    if labelled
                    else "(This image appears blank, so it was not sent to OCR.)"
                )
            else:
                parts.append(
                    f"[page {n}] (OCR failed: {r.reason})"
                    if labelled
                    else f"(OCR failed: {r.reason})"
                )
    return "\n\n".join(parts)


def compose_pdf_text(
    extract: documents.PdfExtract,
    results: dict[int, PageResult],
    deferred: Iterable[int],
) -> str:
    """seafile_read_file's text with its OCR'd pages slotted in.

    Called only when the range has pages without a text layer; otherwise the
    extract's own text stands, byte for byte.
    """
    text_layers = {
        extract.first_page + i: t
        for i, t in enumerate(extract.page_texts)
        if documents.has_text_layer(t)
    }
    return _compose(
        extract.first_page, extract.last_page, text_layers, results, deferred, True
    )


# --------------------------------------------------------------------------- #
# What the model is told
# --------------------------------------------------------------------------- #

#: Follows UNTRUSTED_NOTICE on any result where a page was actually
#: transcribed. The untrusted notice covers who wrote the content; this covers
#: how faithfully it reached the model, which is a separate failure: a
#: confidently misread "8" for "3" in a figure looks exactly like the
#: document's own wording.
OCR_NOTICE = (
    " Text transcribed by OCR is a machine transcription: a separate model "
    "read images of the pages. Characters, numbers, names and table structure "
    "can be misread, so say so when exact wording or figures matter, and never "
    "present it as verified."
)


def _has(pages: list[int], one: str, many: str) -> str:
    return one if len(pages) == 1 else many


def _outcome_sentences(
    where: Callable[[list[int]], str],
    results: dict[int, PageResult],
    *,
    again: str,
    force_with: str,
) -> list[str]:
    """The sentences both tools share about pages that went towards OCR.

    ``again`` is how to retry ("again" for the same tool); ``force_with`` is
    how to force a blank page through, which differs: seafile_read_file has
    no force_ocr of its own.
    """
    by = {
        status: [n for n, r in sorted(results.items()) if r.status == status]
        for status in ("ocr", "blank", "failed")
    }
    no_text = [n for n in by["ocr"] if not results[n].text]
    limited = [n for n in by["ocr"] if results[n].hit_token_limit]
    parts: list[str] = []
    if no_text:
        parts.append(f" The OCR model found no text on {where(no_text)}.")
    if limited:
        parts.append(
            f" The transcription of {where(limited)} stopped at this server's "
            f"per-page output limit, so its end may be missing or the model may "
            f"have started repeating itself."
        )
    if by["blank"]:
        blank = by["blank"]
        parts.append(
            f" Nothing was sent to OCR for {where(blank)}: "
            f"{_has(blank, 'it looks', 'they look')} blank. If there is faint "
            f"content worth reading, call {force_with} with force_ocr=true to "
            f"transcribe {_has(blank, 'it', 'them')} anyway."
        )
    if by["failed"]:
        failed = by["failed"]
        parts.append(
            f" OCR failed for {where(failed)} ({results[failed[0]].reason}) Call "
            f"{again} with start_page={min(failed)} and end_page={max(failed)} to "
            f"retry."
        )
    return parts


def ocr_notice(x: OcrResult, *, again: str = "again") -> str:
    """seafile_ocr_file's account of what was covered and how to continue.

    States the true page total every time, so a capped or partial result can
    never be mistaken for the whole document. ``again`` names the tool to
    continue with, for when this result is returned by another one.
    """

    def where(pages: list[int]) -> str:
        return documents.pages_phrase(pages) if x.labelled else "this image"

    parts: list[str] = []
    if x.labelled:
        what = "This PDF" if x.source == "PDF" else f"This {x.source} file"
        if (x.first_page, x.last_page) == (1, x.page_count):
            parts.append(f" {what} has {x.page_count} page(s), all covered by this call.")
        else:
            covered = documents.pages_phrase(list(range(x.first_page, x.last_page + 1)))
            parts.append(f" {what} has {x.page_count} page(s); this call covered {covered}.")
    else:
        parts.append(f" This file is a {x.source} image.")
    if x.animation_frames > 1:
        parts.append(
            f" It is animated with {x.animation_frames} frames; only the first "
            f"was transcribed."
        )
    if x.ocr_pages and x.labelled:
        parts.append(f" Transcribed by OCR, marked (OCR): {where(list(x.ocr_pages))}.")
    if x.text_layer_pages:
        if not x.ocr_pages and not x.failed_pages and not x.blank_pages:
            parts.append(
                " None of these pages needed OCR: each already has a text layer, "
                "and that text is what you got, at no OCR cost."
            )
        else:
            parts.append(
                f" The text of {where(list(x.text_layer_pages))} was taken from "
                f"the PDF's own text layer rather than OCR'd."
            )
        parts.append(
            f" If that text looks garbled — a copier's 'searchable PDF' often "
            f"hides poor machine text behind the scan — call {again} with "
            f"force_ocr=true to transcribe those pages anyway."
        )
    parts += _outcome_sentences(where, x.page_results, again=again, force_with=again)
    if x.clamped and x.last_page < x.page_count:
        parts.append(
            f" This server covers at most {x.max_pages} pages per call, so this "
            f"call stopped at page {x.last_page}. Call {again} with "
            f"start_page={x.last_page + 1} to continue."
        )
    return "".join(parts)


def read_notice(
    results: dict[int, PageResult], deferred: list[int], max_pages: int
) -> str:
    """seafile_read_file's account of its pages without a text layer."""
    where = documents.pages_phrase
    parts: list[str] = []
    transcribed = [n for n, r in sorted(results.items()) if r.status == "ocr"]
    if transcribed:
        parts.append(
            f" There is no text layer on {where(transcribed)} (likely scanned), so "
            f"{_has(transcribed, 'it was', 'they were')} transcribed by OCR and "
            f"{_has(transcribed, 'is', 'are')} marked (OCR)."
        )
    parts += _outcome_sentences(
        where, results, again="again", force_with="seafile_ocr_file"
    )
    if deferred:
        parts.append(
            f" There is no text layer on {where(deferred)} either, but this server "
            f"transcribes at most {max_pages} pages per call, so "
            f"{_has(deferred, 'it was', 'they were')} not transcribed. Call again "
            f"with start_page={deferred[0]} to continue."
        )
    return "".join(parts)
