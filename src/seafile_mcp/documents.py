"""PDF text extraction for ``seafile_read_file``.

Seafile libraries in this deployment are mostly PDFs, and decoding one as
UTF-8 (the plain-text path) silently "succeeds" with mojibake. This module
extracts the text layer instead, with no OCR: a scanned/image-only PDF comes
back as an explicit placeholder, not an error and not garbage.

Long documents are read in pieces rather than parsed cover-to-cover on every
call — extracting a page's text (decoding its content stream and any
embedded font/CMap tables) is the expensive step, and it is skipped for pages
outside the requested range. See ``extract_pdf_text`` for the default-range
behaviour that makes a bare call cheap even on a long document.
"""

from __future__ import annotations

from io import BytesIO
from typing import NamedTuple

from pypdf import PdfReader

#: Default for extract_pdf_text's ``preview_threshold_pages`` argument, and
#: for Settings.pdf_preview_threshold_pages (SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES).
#: Documents longer than this get a short preview instead of full extraction
#: when the caller specifies no range at all. Short documents are always
#: returned in full on a bare call, regardless of this value. Deployments can
#: override or disable it entirely (see config.py); it is not hardcoded here.
DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES = 15

#: How many pages a preview contains, for documents over the threshold above.
_PDF_PREVIEW_PAGES = 2

#: Appended to FileContent.notice for any PDF read, ahead of the page_notice
#: sentence describing what was actually extracted.
PDF_EXTRACTION_NOTICE = (
    " This file was a PDF; its text layer was extracted (no OCR, no layout, "
    "tables or images)."
)

_PLACEHOLDER_NO_TEXT_TEMPLATE = (
    "Pages {first}-{last} of this PDF contain no extractable text (they may "
    "be scanned images; this server does not do OCR). Other pages may still "
    "contain text."
)


def is_pdf(data: bytes) -> bool:
    """True if the downloaded bytes are a PDF.

    Checked on content, never on the filename/extension — a caller picks the
    path, so the extension is not trustworthy evidence of what the file is.
    """
    return b"%PDF-" in data[:1024]


def check_page_range(start_page: int | None, end_page: int | None) -> None:
    """Validate page bounds that need no document. Raises ValueError.

    Called before downloading the file, so an unsatisfiable request never
    pulls a large PDF for nothing.
    """
    for name, value in (("start_page", start_page), ("end_page", end_page)):
        if value is not None and value < 1:
            raise ValueError(
                f"{name} is 1-indexed and must be 1 or greater; got {value}."
            )
    if start_page is not None and end_page is not None and end_page < start_page:
        raise ValueError(
            f"end_page ({end_page}) is before start_page ({start_page}); "
            f"the range is inclusive."
        )


class PdfExtract(NamedTuple):
    text: str
    page_count: int
    first_page: int
    last_page: int
    auto_previewed: bool


def extract_pdf_text(
    data: bytes,
    start_page: int | None = None,
    end_page: int | None = None,
    preview_threshold_pages: int | None = DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES,
) -> PdfExtract:
    """Extract text from the given page range (1-indexed, inclusive).

    If both ``start_page`` and ``end_page`` are ``None`` and the document has
    more than ``preview_threshold_pages`` pages, only the first
    :data:`_PDF_PREVIEW_PAGES` are extracted (``auto_previewed=True``) rather
    than the whole document — this is what keeps a bare call cheap on a long
    PDF. A short document, or any explicit range, is unaffected.

    ``preview_threshold_pages=None`` disables this entirely: a bare call
    always extracts the whole document, regardless of length. Callers
    reaching this from the MCP server pass the deployment's configured
    ``Settings.pdf_preview_threshold_pages`` here.
    """
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError(
                "This PDF is password-protected and cannot be read without "
                "the password."
            )
        total = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:  # pypdf.errors.PdfReadError and friends
        raise ValueError(f"Could not parse this PDF: {exc}") from None

    auto_previewed = (
        start_page is None
        and end_page is None
        and preview_threshold_pages is not None
        and total > preview_threshold_pages
    )

    if auto_previewed:
        first, last = 1, min(_PDF_PREVIEW_PAGES, total)
    else:
        first = start_page or 1
        if first > total:
            raise ValueError(
                f"This PDF has {total} page(s); start_page={first} is past "
                f"the end."
            )
        last = min(end_page, total) if end_page is not None else total

    pages_text = [reader.pages[i].extract_text() for i in range(first - 1, last)]
    joined = "\n\n".join(
        f"[page {first + i}]\n{page_text}"
        for i, page_text in enumerate(pages_text)
        if page_text
    )
    text = joined or _PLACEHOLDER_NO_TEXT_TEMPLATE.format(first=first, last=last)

    return PdfExtract(
        text=text,
        page_count=total,
        first_page=first,
        last_page=last,
        auto_previewed=auto_previewed,
    )


def page_notice(x: PdfExtract) -> str:
    """One sentence for FileContent.notice describing what was extracted.

    Always states the document's true total page count, in the same
    sentence as any partial-range explanation, so a preview can never be
    mistaken for the whole document.
    """
    if (x.first_page, x.last_page) == (1, x.page_count):
        return f" This PDF has {x.page_count} page(s), all of which were extracted."
    if x.auto_previewed:
        return (
            f" This PDF has {x.page_count} page(s); only the first {x.last_page} "
            f"were extracted by default because it is long. Look for a table of "
            f"contents, index, or section headings in what you just received, and "
            f"use it to identify which pages actually answer the question — then "
            f"call this tool again with start_page/end_page set to that range. If "
            f"nothing in the preview points to a specific range, call again with "
            f"end_page={x.page_count} to read the rest."
        )
    return (
        f" This PDF has {x.page_count} page(s); pages {x.first_page}-{x.last_page} "
        f"were extracted. Call this tool again with start_page/end_page to read "
        f"other pages."
    )


__all__ = [
    "is_pdf",
    "check_page_range",
    "PdfExtract",
    "extract_pdf_text",
    "page_notice",
    "PDF_EXTRACTION_NOTICE",
    "DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES",
]
