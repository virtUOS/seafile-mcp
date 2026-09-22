"""Document text extraction for ``seafile_read_file``.

Seafile libraries in this deployment mix PDFs, Word, Excel, and PowerPoint
files. Decoding any of these as UTF-8 (the plain-text path) silently
"succeeds" with mojibake instead of an error. This module extracts real text
from each format instead: PDF/Word/PowerPoint/Excel each get their own
``is_X``/``extract_X_text`` pair, content-sniffed on the downloaded bytes
rather than trusted from the caller-supplied filename. There is no OCR: any
format's scanned/image-only or otherwise textless content comes back as an
explicit placeholder, never silent garbage.

Image files are the one format here that does not become text at all. They are
decoded, downscaled and re-encoded by ``render_image`` so the tool can hand the
model the picture itself as an MCP image block — the only way an agent with no
sandbox and no filesystem can see one. Before this existed a .png fell through
to the plain-text path and came back as half a megabyte of U+FFFD, which is the
exact failure this module was written to stop.

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
from PIL import Image as PILImage
from PIL import ImageOps
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
# Images
# --------------------------------------------------------------------------- #

#: Default for Settings.max_image_edge_px. Mainstream vision models downsample
#: anything whose long edge is bigger than roughly this before they look at it,
#: so sending more pixels costs upload bytes and the model's tool-output budget
#: without buying any detail it can actually use.
DEFAULT_MAX_IMAGE_EDGE_PX = 1568

#: Default for Settings.max_image_mb, applied to the *re-encoded* image before
#: base64 inflates it by a third. A tool result goes straight into a model's
#: context, so this bounds the context cost of one call as much as the bytes.
DEFAULT_MAX_IMAGE_MB = 5

#: Ceiling on decoded pixels, checked against the header before anything is
#: decoded. A 2 MB PNG can declare 60000x60000, which is ~14 GB as RGBA, so
#: max_download_mb does not bound this at all: the compressed size of a
#: decompression bomb says nothing about its decoded size. 50 MP is comfortably
#: past any real camera (a 24 MP frame is 24 MP) and costs ~200 MB decoded,
#: which one shared process can survive.
MAX_IMAGE_PIXELS = 50_000_000

#: Formats Pillow is permitted to identify. Without an allow-list Pillow will
#: recognise text beginning "P3" as a PPM and "%!PS" as EPS, so a plain file
#: could be routed into the image path by a decoder far more eager than the
#: magic-number table below. Only formats is_image actually sniffs are listed.
_PILLOW_FORMATS = ("PNG", "JPEG", "JPEG2000", "GIF", "BMP", "WEBP", "TIFF", "ICO")

#: JPEG attempts, in order, when the encoded result is over the byte cap: a
#: bounded ladder rather than a loop, each step cheap next to the decode.
#: Quality stops at 60 and then the long edge halves instead, because below
#: that the ringing starts eating the small text in a scan or a screenshot,
#: which is usually the thing the model was asked to read.
_JPEG_LADDER: tuple[tuple[int, int], ...] = (
    (85, 1),
    (72, 1),
    (60, 1),
    (72, 2),
    (60, 4),
)

IMAGE_EXTRACTION_NOTICE = (
    " This file is an image. It is attached to this result as an image you can "
    "see directly, not as text, and no OCR was performed on it."
)


def _is_bmp(head: bytes) -> bool:
    """True for a BMP header.

    "BM" alone is two bytes and matches plenty of ordinary text, which matters
    more than it looks: ``requires_complete_file`` consults ``is_image``, so a
    false positive would make an oversized text file starting "BM" get refused
    outright instead of returned as a readable prefix. The BMP header's two
    reserved 16-bit fields at offsets 6-10 are zero in every real BMP, which
    takes the false-positive rate from plausible to negligible.
    """
    return len(head) >= 14 and head[:2] == b"BM" and head[6:10] == b"\x00\x00\x00\x00"


#: Magic numbers for the raster formats this server renders, as (offset, magic)
#: pairs. Sniffed on content, never on the filename, like every other format in
#: this module. WebP and BMP need more than a fixed pair and are checked
#: separately in is_image.
_IMAGE_MAGIC: tuple[tuple[int, bytes], ...] = (
    (0, b"\x89PNG\r\n\x1a\n"),                      # PNG
    (0, b"\xff\xd8\xff"),                           # JPEG
    (0, b"GIF87a"),                                 # GIF
    (0, b"GIF89a"),
    (0, b"II\x2a\x00"),                             # TIFF, little-endian
    (0, b"MM\x00\x2a"),                             # TIFF, big-endian
    (0, b"\x00\x00\x01\x00"),                       # ICO
    (0, b"\x00\x00\x00\x0cjP  \r\n\x87\n"),         # JPEG 2000
    (0, b"\xffO\xffQ"),                             # JPEG 2000 codestream
    (4, b"ftypavif"),                               # AVIF
    (4, b"ftypavis"),
    (4, b"ftypheic"),                               # HEIF/HEIC
    (4, b"ftypheix"),
    (4, b"ftypmif1"),
    (4, b"ftypmsf1"),
)


def is_image(data: bytes) -> bool:
    """True if the bytes are a raster image this server can render.

    Checked on content, never on the filename/extension. Magic numbers only,
    with no Pillow involved, because ``requires_complete_file`` calls this on
    the first kilobyte of a download that has not finished arriving. Every
    signature here fits well inside ``SNIFF_BYTES``.
    """
    for offset, magic in _IMAGE_MAGIC:
        if data[offset : offset + len(magic)] == magic:
            return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return _is_bmp(data)


class RenderedImage(NamedTuple):
    """An image re-encoded for a model to look at.

    ``data`` is raw bytes, deliberately not base64: the caller hands them to
    ``fastmcp.utilities.types.Image``, which does the encoding. Base64-ing them
    here would double-encode.

    ``image_format`` is the MIME suffix ("jpeg"/"png"), never "jpg" — that
    would produce the invalid type ``image/jpg``.

    The source dimensions are kept alongside the sent ones so the caller can
    tell the model what it is *not* seeing. A 4000px scan shown at 1568px may
    have lost the fine print, and "I cannot read this label" and "this label is
    blank" are different answers.
    """

    data: bytes
    image_format: str
    width: int
    height: int
    source_format: str
    source_width: int
    source_height: int
    downscaled: bool
    frames: int


def _has_alpha(img: PILImage.Image) -> bool:
    """True if an RGBA image has at least one pixel that is not fully opaque."""
    if img.mode != "RGBA":
        return False
    return img.getchannel("A").getextrema()[0] < 255


def _flatten(img: PILImage.Image) -> PILImage.Image:
    """Reduce whatever colour mode Pillow opened to RGB, or RGBA if it matters.

    These all decode fine and then fail to re-encode: JPEG cannot hold alpha,
    a CMYK JPEG written by Pillow renders inverted in most viewers, and 16-bit
    and bilevel modes have no JPEG representation at all. Alpha is kept only
    when some pixel actually uses it, because keeping it forces PNG output and
    a photograph as PNG is several times larger for no gain.
    """
    if img.mode == "RGB":
        return img
    if img.mode == "RGBA":
        return img if _has_alpha(img) else img.convert("RGB")
    if img.mode in ("P", "PA", "LA", "La"):
        converted = img.convert("RGBA")
        return converted if _has_alpha(converted) else converted.convert("RGB")
    # L, 1, I, I;16, F, CMYK, YCbCr. The 16-bit and float modes are scientific
    # TIFF territory and clip rather than rescale; that is lossy, and said so
    # in the notice rather than papered over with autocontrast.
    return img.convert("RGB")


def _encode(img: PILImage.Image, max_bytes: int) -> tuple[bytes, str, PILImage.Image]:
    """Encode under ``max_bytes``, returning the bytes, format, and final image.

    PNG when the image has real transparency: JPEG would composite it, and
    compositing dark text on a transparent background onto black makes a
    screenshot unreadable. Everything else is JPEG, which is 5-15x smaller than
    PNG for photographs and scans and is accepted everywhere.

    The image comes back too because a ladder step may have resized it, and the
    caller reports the dimensions actually sent.
    """
    if _has_alpha(img):
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            return data, "png", img
        # Too big with transparency intact. Compositing onto white loses less
        # than refusing, and image_notice says it happened.
        flat = PILImage.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.getchannel("A"))
        img = flat

    if img.mode != "RGB":
        img = img.convert("RGB")

    data = b""
    candidate = img
    for quality, divisor in _JPEG_LADDER:
        candidate = img
        if divisor > 1:
            candidate = img.copy()
            candidate.thumbnail(
                (max(img.width // divisor, 1), max(img.height // divisor, 1)),
                PILImage.Resampling.LANCZOS,
            )
        buf = BytesIO()
        candidate.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            break
    return data, "jpeg", candidate


def render_image(
    data: bytes,
    max_edge: int = DEFAULT_MAX_IMAGE_EDGE_PX,
    max_bytes: int = DEFAULT_MAX_IMAGE_MB * 1024 * 1024,
) -> RenderedImage:
    """Decode, downscale and re-encode an image so a model can look at it.

    Raises ValueError on anything that cannot be decoded, rather than returning
    something broken — the same contract as every other extractor here, and
    what lets ``_handle_errors`` turn it into a clean ToolError.

    Blocking and CPU-bound, so callers run it in a worker thread: decoding a
    24 MP JPEG is a few hundred milliseconds of C, and one process serves every
    user of this deployment. Pillow releases the GIL in decode and resize, so a
    thread genuinely helps here.

    The order of operations is load-bearing:

    1. identify from the header only — ``PILImage.open`` is lazy, so the size
       is known before any buffer is allocated and a bomb is refused for free;
    2. ``draft`` lets libjpeg DCT-scale during decode, so a 24 MP photo never
       materialises at full size just to be shrunk afterwards;
    3. EXIF orientation, because phones record the sensor's rotation rather
       than the photo's and an upright scan decoded sideways is unreadable;
    4. colour mode flattened (see ``_flatten``);
    5. downscaled to ``max_edge`` on the long side, never enlarged — upscaling
       adds bytes and invents no detail.
    """
    try:
        img = PILImage.open(BytesIO(data), formats=_PILLOW_FORMATS)
    except PILImage.DecompressionBombError as exc:
        # Pillow's own global ceiling, which it applies while parsing the
        # header. Caught separately so the caller gets the size explanation
        # rather than a generic "could not be decoded".
        raise ValueError(
            f"This image declares more pixels than this server will decode "
            f"({exc}). Use seafile_get_download_link to fetch it directly."
        ) from None
    except Exception as exc:
        raise ValueError(
            f"This file looks like an image but could not be decoded: {exc}"
        ) from None

    source_format = img.format or "unknown"
    # The header's dimensions, which are what the bomb check must use — it has
    # to run before anything is decoded. They are the *stored* raster, so for an
    # EXIF-rotated photo the axes are swapped relative to how it is meant to be
    # seen; the reported source size is taken again after exif_transpose below,
    # since the caller quotes it to the model and "3000x4000" for a picture the
    # model is looking at in landscape is just wrong.
    header_width, header_height = img.size
    frames = getattr(img, "n_frames", 1)

    # Checked against the header, before a pixel is decoded. Not done by
    # raising PILImage.MAX_IMAGE_PIXELS: that global is shared with python-pptx
    # in this same process, and mutating it from a worker thread would let two
    # concurrent reads restore each other's value.
    if header_width * header_height > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"This image is {header_width}x{header_height} "
            f"({header_width * header_height / 1e6:.0f} megapixels), past what "
            f"this server will decode. Use seafile_get_download_link to fetch "
            f"it directly."
        )

    # JPEG/JPEG2000 only, ignored by every other format. Must precede load().
    img.draft(None, (max_edge, max_edge))

    try:
        img = ImageOps.exif_transpose(img) or img
        source_width, source_height = img.size
        img = _flatten(img)
    except PILImage.DecompressionBombError as exc:
        # Backstop: Pillow's own global guard, for a format that reports its
        # size only once decoding starts.
        raise ValueError(f"Refused to decode this image: {exc}") from None
    except Exception as exc:
        raise ValueError(
            f"This file looks like an image but could not be decoded: {exc}"
        ) from None

    downscaled = max(img.size) > max_edge
    if downscaled:
        img.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)

    encoded, image_format, final = _encode(img, max_bytes)

    return RenderedImage(
        data=encoded,
        image_format=image_format,
        width=final.width,
        height=final.height,
        source_format=source_format,
        source_width=source_width,
        source_height=source_height,
        downscaled=downscaled or final.size != img.size,
        frames=frames,
    )


def image_notice(path: str, x: RenderedImage) -> str:
    """State what the model is looking at, and what it is not.

    Says explicitly when the image was shrunk, because "I cannot read the
    serial number in this photo" and "this photo has no serial number" are
    different answers and only this server knows which one applies.
    """
    parts = [
        f" {path} is a {x.source_format} image, "
        f"{x.source_width}x{x.source_height} pixels."
    ]
    if x.downscaled:
        parts.append(
            f" It was downscaled to {x.width}x{x.height} to fit this server's "
            f"limits, so small print in it may be illegible or misread — say so "
            f"rather than guessing at it."
        )
    else:
        parts.append(" It is shown at full size.")
    if x.frames > 1:
        parts.append(
            f" The source is animated with {x.frames} frames; only the first "
            f"is shown."
        )
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Legacy binary / encrypted Office files (.doc/.xls/.ppt or password-protected)
# --------------------------------------------------------------------------- #

#: The OLE2/Compound File Binary magic number. Shared by legacy pre-2007
#: .doc/.xls/.ppt and by password-protected modern .docx/.xlsx/.pptx (which
#: MS-OFFCRYPTO wraps in the same container) — the two cannot be told apart
#: from these bytes alone.
_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


#: How many leading bytes are enough to recognise a container format.
SNIFF_BYTES = 1024

#: Magic numbers for formats whose index lives at the *end* of the file, so a
#: prefix of one cannot be parsed at all: a PDF's cross-reference table, and the
#: ZIP central directory that .docx/.xlsx/.pptx depend on. OLE2 (legacy binary
#: Office, and password-protected OOXML) is in the same position.
_ZIP_MAGIC = b"PK\x03\x04"


def requires_complete_file(head: bytes) -> bool:
    """True if this format cannot be read from a prefix of its bytes.

    Used to decide whether a file too large for the download cap can still be
    served partially. Plain text can: the first N bytes of a log or Markdown
    file are exactly the first N bytes of its text. A PDF or an OOXML document
    cannot, because the structure needed to find anything sits at the end, so
    handing back a prefix would produce a parse error rather than partial
    content.

    Images are in the same position for a different reason: a prefix of a PNG
    or JPEG is not a smaller picture, it is an undecodable fragment, and half
    an image is of no more use to a model than half a PDF.

    Deliberately magic-number only: this runs on whatever has arrived so far,
    which is far too little for ``is_docx`` and friends to open the archive.
    """
    return (
        b"%PDF-" in head[:SNIFF_BYTES]
        or head[:4] == _ZIP_MAGIC
        or head[:8] == _CFB_MAGIC
        or is_image(head)
    )


def is_legacy_or_encrypted_office(data: bytes) -> bool:
    """True if the bytes are an OLE2/CFB container.

    Checked on content, never on the filename/extension. A True result means
    "not readable by this server, either because it's a legacy pre-2007
    binary Office format or because it's password-protected" — not a
    specific diagnosis of which.
    """
    return data[:8] == _CFB_MAGIC


__all__ = [
    "requires_complete_file",
    "SNIFF_BYTES",
    "is_pdf",
    "is_docx",
    "is_pptx",
    "is_xlsx",
    "is_legacy_or_encrypted_office",
    "is_image",
    "RenderedImage",
    "render_image",
    "image_notice",
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
    "IMAGE_EXTRACTION_NOTICE",
    "DEFAULT_MAX_IMAGE_EDGE_PX",
    "DEFAULT_MAX_IMAGE_MB",
    "MAX_IMAGE_PIXELS",
]
