"""Document text extraction for ``seafile_read_file``.

Seafile libraries in this deployment mix PDFs, Word, Excel, and PowerPoint
files. Decoding any of these as UTF-8 (the plain-text path) silently
"succeeds" with mojibake instead of an error. This module extracts real text
from each format instead: PDF/Word/PowerPoint/Excel each get their own
``is_X``/``extract_X_text`` pair, content-sniffed on the downloaded bytes
rather than trusted from the caller-supplied filename. There is no OCR: any
format's scanned/image-only or otherwise textless content comes back as an
explicit placeholder, never silent garbage.

PDF pages and PowerPoint slides are read in pieces rather than parsed
cover-to-cover on every call, since extracting a unit's text is the
expensive step and a bare call auto-previews the first few units of a long
document/deck (see ``extract_pdf_text``/``extract_pptx_text``). Excel
workbooks are chunked by sheet instead, since a sheet — not a page/slide
index — is the natural addressable unit: a bare call on a workbook with many
sheets previews just the first, and a specific one can be requested by name
(see ``extract_xlsx_text``).

A password-protected ``.docx``/``.pptx``/``.xlsx`` isn't a ZIP archive at
all — it's wrapped in the same OLE2/CFB container legacy pre-2007
``.doc``/``.xls``/``.ppt`` binaries use. ``is_legacy_or_encrypted_office``
sniffs that container so callers can raise a clear error instead of falling
through to mojibake, but it cannot tell the two cases apart from the magic
bytes alone.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from typing import NamedTuple

import openpyxl
from docx import Document as DocxDocument
from pptx import Presentation
from pypdf import PdfReader

# --------------------------------------------------------------------------- #
# Shared range-resolution helper (PDF pages, PowerPoint slides)
# --------------------------------------------------------------------------- #


def check_page_range(
    start_page: int | None,
    end_page: int | None,
    *,
    start_name: str = "start_page",
    end_name: str = "end_page",
) -> None:
    """Validate numbered-range bounds that need no document. Raises ValueError.

    Called before downloading the file, so an unsatisfiable request never
    pulls a large file for nothing. ``start_name``/``end_name`` let callers
    reuse this for PowerPoint's ``start_slide``/``end_slide`` — the
    validation rules (1-indexed, positive, non-inverted) are identical
    regardless of what is being counted.
    """
    for name, value in ((start_name, start_page), (end_name, end_page)):
        if value is not None and value < 1:
            raise ValueError(
                f"{name} is 1-indexed and must be 1 or greater; got {value}."
            )
    if start_page is not None and end_page is not None and end_page < start_page:
        raise ValueError(
            f"{end_name} ({end_page}) is before {start_name} ({start_page}); "
            f"the range is inclusive."
        )


def _resolve_numbered_range(
    total: int,
    start: int | None,
    end: int | None,
    preview_threshold: int | None,
    preview_size: int,
    *,
    noun: str,
    unit: str,
    param_name: str,
) -> tuple[int, int, bool]:
    """Decide which 1-indexed, inclusive range of units to extract.

    Shared by ``extract_pdf_text`` (pages) and ``extract_pptx_text``
    (slides): if both bounds are omitted and the document exceeds
    ``preview_threshold`` units, only the first ``preview_size`` are
    selected (``auto_previewed=True``); otherwise the explicit or full range
    is used. ``preview_threshold=None`` disables auto-preview entirely.
    """
    auto_previewed = (
        start is None
        and end is None
        and preview_threshold is not None
        and total > preview_threshold
    )

    if auto_previewed:
        first, last = 1, min(preview_size, total)
    else:
        first = start or 1
        if first > total:
            raise ValueError(
                f"This {noun} has {total} {unit}(s); {param_name}={first} is "
                f"past the end."
            )
        last = min(end, total) if end is not None else total

    return first, last, auto_previewed


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

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

    first, last, auto_previewed = _resolve_numbered_range(
        total,
        start_page,
        end_page,
        preview_threshold_pages,
        _PDF_PREVIEW_PAGES,
        noun="PDF",
        unit="page",
        param_name="start_page",
    )

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


# --------------------------------------------------------------------------- #
# Word (.docx)
# --------------------------------------------------------------------------- #

DOCX_EXTRACTION_NOTICE = (
    " This file was a Word document; paragraph text was extracted. Any "
    "tables are listed afterward as tab-separated rows rather than inline "
    "where they appear in the document, and no formatting or images are "
    "preserved."
)


def is_docx(data: bytes) -> bool:
    """True if the downloaded bytes are a Word (.docx) document.

    Checked on content, never on the filename/extension. A .docx is a ZIP
    archive; ``word/document.xml`` is the part that identifies it as Word
    specifically, as opposed to a .pptx/.xlsx or an unrelated ZIP.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return "word/document.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def extract_docx_text(data: bytes) -> str:
    """Extract all paragraph and table text from a Word document.

    No chunking: the whole document is extracted and left to the caller's
    existing size cap, same as a plain-text file. Word stores no page
    boundaries in the file itself (pagination is computed at render time),
    so there is no natural unit to chunk by.
    """
    try:
        doc = DocxDocument(BytesIO(data))
    except Exception as exc:  # docx.opc.exceptions.PackageNotFoundError and friends
        raise ValueError(f"Could not parse this Word document: {exc}") from None

    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    body = "\n".join(paragraphs)

    tables_text = []
    for i, table in enumerate(doc.tables, start=1):
        rows = ["\t".join(cell.text for cell in row.cells) for row in table.rows]
        tables_text.append(f"[table {i}]\n" + "\n".join(rows))

    if tables_text:
        body = (body + "\n\n" if body else "") + "Tables:\n\n" + "\n\n".join(tables_text)

    return body or "This Word document contains no extractable text."


# --------------------------------------------------------------------------- #
# PowerPoint (.pptx)
# --------------------------------------------------------------------------- #

#: Default for extract_pptx_text's ``preview_threshold_slides`` argument, and
#: for Settings.pptx_preview_threshold_slides.
DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES = 20

#: How many slides a preview contains, for decks over the threshold above.
_PPTX_PREVIEW_SLIDES = 3

PPTX_EXTRACTION_NOTICE = (
    " This file was a PowerPoint presentation; visible slide text was "
    "extracted (no speaker notes, layout, images, or embedded objects)."
)

_PLACEHOLDER_NO_TEXT_PPTX_TEMPLATE = (
    "Slides {first}-{last} of this presentation contain no extractable text "
    "(they may be image-only or use unsupported shape types)."
)


def is_pptx(data: bytes) -> bool:
    """True if the downloaded bytes are a PowerPoint (.pptx) presentation.

    Checked on content, never on the filename/extension.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return "ppt/presentation.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


class PptxExtract(NamedTuple):
    text: str
    slide_count: int
    first_slide: int
    last_slide: int
    auto_previewed: bool


def extract_pptx_text(
    data: bytes,
    start_slide: int | None = None,
    end_slide: int | None = None,
    preview_threshold_slides: int | None = DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES,
) -> PptxExtract:
    """Extract visible text from the given slide range (1-indexed, inclusive).

    Mirrors :func:`extract_pdf_text`'s auto-preview behaviour: a bare call on
    a deck longer than ``preview_threshold_slides`` returns only the first
    :data:`_PPTX_PREVIEW_SLIDES` slides (``auto_previewed=True``); a short
    deck, or any explicit range, is unaffected. ``preview_threshold_slides=None``
    disables auto-preview entirely.
    """
    try:
        prs = Presentation(BytesIO(data))
        slides = list(prs.slides)
        total = len(slides)
    except Exception as exc:
        raise ValueError(f"Could not parse this PowerPoint file: {exc}") from None

    first, last, auto_previewed = _resolve_numbered_range(
        total,
        start_slide,
        end_slide,
        preview_threshold_slides,
        _PPTX_PREVIEW_SLIDES,
        noun="PowerPoint file",
        unit="slide",
        param_name="start_slide",
    )

    slide_texts = []
    for slide in slides[first - 1 : last]:
        parts = [
            shape.text_frame.text
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        slide_texts.append("\n".join(parts))

    joined = "\n\n".join(
        f"[slide {first + i}]\n{slide_text}"
        for i, slide_text in enumerate(slide_texts)
        if slide_text
    )
    text = joined or _PLACEHOLDER_NO_TEXT_PPTX_TEMPLATE.format(first=first, last=last)

    return PptxExtract(
        text=text,
        slide_count=total,
        first_slide=first,
        last_slide=last,
        auto_previewed=auto_previewed,
    )


def slide_notice(x: PptxExtract) -> str:
    """One sentence for FileContent.notice describing what was extracted.

    Mirrors :func:`page_notice`: always states the deck's true total slide
    count alongside any partial-range explanation.
    """
    if (x.first_slide, x.last_slide) == (1, x.slide_count):
        return (
            f" This presentation has {x.slide_count} slide(s), all of which "
            f"were extracted."
        )
    if x.auto_previewed:
        return (
            f" This presentation has {x.slide_count} slide(s); only the first "
            f"{x.last_slide} were extracted by default because it is long. Call "
            f"this tool again with start_slide/end_slide set to a specific "
            f"range, or end_slide={x.slide_count} to read the rest."
        )
    return (
        f" This presentation has {x.slide_count} slide(s); slides "
        f"{x.first_slide}-{x.last_slide} were extracted. Call this tool again "
        f"with start_slide/end_slide to read other slides."
    )


# --------------------------------------------------------------------------- #
# Excel (.xlsx)
# --------------------------------------------------------------------------- #

#: Default for extract_xlsx_text's ``preview_threshold_sheets`` argument, and
#: for Settings.xlsx_preview_threshold_sheets. A workbook with more sheets
#: than this gets only its first sheet on a bare call, instead of all of them.
DEFAULT_XLSX_PREVIEW_THRESHOLD_SHEETS = 5

XLSX_EXTRACTION_NOTICE = (
    " This file was an Excel workbook; cell values were extracted as "
    "tab-separated rows (formulas show their last-saved computed value, not "
    "the formula text; no formatting, charts, or images)."
)

_PLACEHOLDER_NO_TEXT_XLSX_TEMPLATE = "Sheet '{sheet}' of this workbook contains no data."


def is_xlsx(data: bytes) -> bool:
    """True if the downloaded bytes are an Excel (.xlsx) workbook.

    Checked on content, never on the filename/extension.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return "xl/workbook.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


class XlsxExtract(NamedTuple):
    text: str
    sheet_names: tuple[str, ...]
    extracted_sheets: tuple[str, ...]
    auto_previewed: bool


def extract_xlsx_text(
    data: bytes,
    sheet_name: str | None = None,
    preview_threshold_sheets: int | None = DEFAULT_XLSX_PREVIEW_THRESHOLD_SHEETS,
) -> XlsxExtract:
    """Extract cell values, chunked by sheet rather than by numbered range.

    A sheet — not a page or slide index — is Excel's natural addressable
    unit, and sheets are named rather than sequential, so this is shaped
    differently from :func:`extract_pdf_text`/:func:`extract_pptx_text`:

    - ``sheet_name`` given: extract exactly that sheet, or raise ``ValueError``
      listing the workbook's real sheet names if it doesn't exist.
    - ``sheet_name`` omitted, workbook has at most ``preview_threshold_sheets``
      sheets: extract all of them (``auto_previewed=False``).
    - ``sheet_name`` omitted, workbook has more: extract only the first sheet
      (``auto_previewed=True``) — the notice lists every sheet name so a
      follow-up call can target a specific one.

    Cells are read with ``data_only=True``: a formula shows its last-saved
    computed value, not the formula text (and reads as empty if the
    workbook was never opened/saved by Excel/LibreOffice since being
    written).
    """
    try:
        wb = openpyxl.load_workbook(BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError(f"Could not parse this Excel file: {exc}") from None

    all_names = tuple(wb.sheetnames)

    if sheet_name is not None:
        if sheet_name not in all_names:
            raise ValueError(
                f"This workbook has no sheet named {sheet_name!r}. Available "
                f"sheets: {', '.join(all_names)}."
            )
        to_extract: tuple[str, ...] = (sheet_name,)
        auto_previewed = False
    elif preview_threshold_sheets is None or len(all_names) <= preview_threshold_sheets:
        to_extract = all_names
        auto_previewed = False
    else:
        to_extract = all_names[:1]
        auto_previewed = True

    rendered = []
    for name in to_extract:
        ws = wb[name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c for c in cells):
                rows.append("\t".join(cells))
        body = "\n".join(rows) if rows else _PLACEHOLDER_NO_TEXT_XLSX_TEMPLATE.format(sheet=name)
        rendered.append(f"[sheet: {name}]\n{body}")

    return XlsxExtract(
        text="\n\n".join(rendered),
        sheet_names=all_names,
        extracted_sheets=to_extract,
        auto_previewed=auto_previewed,
    )


def sheet_notice(x: XlsxExtract) -> str:
    """One sentence for FileContent.notice describing what was extracted.

    Always lists every sheet name, so the model knows what else exists even
    when only one sheet was actually extracted.
    """
    names_list = ", ".join(x.sheet_names)
    if x.auto_previewed:
        return (
            f" This workbook has {len(x.sheet_names)} sheet(s): {names_list}. "
            f"Only the first sheet ('{x.extracted_sheets[0]}') was extracted by "
            f"default because there are many sheets. Call this tool again with "
            f"sheet_name set to one of the names above to read another."
        )
    if len(x.extracted_sheets) == 1 and len(x.sheet_names) > 1:
        return (
            f" This workbook has {len(x.sheet_names)} sheet(s): {names_list}. "
            f"Only sheet '{x.extracted_sheets[0]}' was extracted, as requested. "
            f"Call this tool again with a different sheet_name to read another."
        )
    return (
        f" This workbook has {len(x.sheet_names)} sheet(s): {names_list}, all "
        f"of which were extracted."
    )


# --------------------------------------------------------------------------- #
# Legacy binary / encrypted Office files (.doc/.xls/.ppt or password-protected)
# --------------------------------------------------------------------------- #

#: The OLE2/Compound File Binary magic number. Shared by legacy pre-2007
#: .doc/.xls/.ppt and by password-protected modern .docx/.xlsx/.pptx (which
#: MS-OFFCRYPTO wraps in the same container) — the two cannot be told apart
#: from these bytes alone.
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def is_legacy_or_encrypted_office(data: bytes) -> bool:
    """True if the bytes are an OLE2/CFB container.

    Checked on content, never on the filename/extension. A True result means
    "not readable by this server, either because it's a legacy pre-2007
    binary Office format or because it's password-protected" — not a
    specific diagnosis of which.
    """
    return data[:8] == _CFB_MAGIC


__all__ = [
    "is_pdf",
    "is_docx",
    "is_pptx",
    "is_xlsx",
    "is_legacy_or_encrypted_office",
    "check_page_range",
    "PdfExtract",
    "extract_pdf_text",
    "page_notice",
    "extract_docx_text",
    "PptxExtract",
    "extract_pptx_text",
    "slide_notice",
    "XlsxExtract",
    "extract_xlsx_text",
    "sheet_notice",
    "PDF_EXTRACTION_NOTICE",
    "DOCX_EXTRACTION_NOTICE",
    "PPTX_EXTRACTION_NOTICE",
    "XLSX_EXTRACTION_NOTICE",
    "DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES",
    "DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES",
    "DEFAULT_XLSX_PREVIEW_THRESHOLD_SHEETS",
]
