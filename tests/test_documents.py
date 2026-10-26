"""PDF detection, page-range selection, and the default-preview behaviour."""

from __future__ import annotations

import codecs
from io import BytesIO

import pytest

from seafile_mcp import documents

from .conftest import CFB_MAGIC_BYTES, ENCRYPTED_MANIFEST, GERMAN_TEXT
from .conftest import odf_bytes as _odf_bytes
from .conftest import odp_bytes as _odp_bytes
from .conftest import ods_bytes as _ods_bytes
from .conftest import odt_bytes as _odt_bytes
from .conftest import docx_with_paragraphs as _docx_with_paragraphs
from .conftest import encrypted_pdf as _encrypted_pdf
from .conftest import pdf_with_pages as _pdf_with_pages
from .conftest import pptx_with_slides as _pptx_with_slides
from .conftest import animated_gif_bytes as _animated_gif_bytes
from .conftest import exif_rotated_jpeg as _exif_rotated_jpeg
from .conftest import image_bytes as _image_bytes
from .conftest import multipage_tiff as _multipage_tiff
from .conftest import oversized_png_header as _oversized_png_header
from .conftest import xlsx_with_sheets as _xlsx_with_sheets


# --------------------------------------------------------------------------- #
# is_pdf
# --------------------------------------------------------------------------- #


def test_is_pdf_true_for_pdf_bytes():
    assert documents.is_pdf(_pdf_with_pages(1))


def test_is_pdf_false_for_plain_text():
    assert not documents.is_pdf(b"just a text file, not a pdf")


def test_is_pdf_ignores_filename_only_checks_bytes():
    # A real PDF has a %PDF- header regardless of what a caller named it.
    assert documents.is_pdf(_pdf_with_pages(1)[:1024])


# --------------------------------------------------------------------------- #
# check_page_range
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("start,end", [(0, None), (-1, None), (None, 0), (None, -5)])
def test_check_page_range_rejects_non_positive(start, end):
    with pytest.raises(ValueError, match="1 or greater"):
        documents.check_page_range(start, end)


def test_check_page_range_rejects_inverted_range():
    with pytest.raises(ValueError, match="before start_page"):
        documents.check_page_range(10, 5)


def test_check_page_range_accepts_valid_and_absent_bounds():
    documents.check_page_range(None, None)
    documents.check_page_range(1, None)
    documents.check_page_range(None, 5)
    documents.check_page_range(3, 3)


# --------------------------------------------------------------------------- #
# extract_pdf_text: basic extraction
# --------------------------------------------------------------------------- #


def test_extracts_real_text_with_no_range():
    result = documents.extract_pdf_text(_pdf_with_pages(1, texted=True))
    assert "Page 1" in result.text
    assert "[page 1]" in result.text
    assert result.page_count == 1
    assert result.first_page == 1
    assert result.last_page == 1
    assert result.auto_previewed is False


def test_blank_page_with_no_text_returns_placeholder_not_error():
    result = documents.extract_pdf_text(_pdf_with_pages(1, texted=False))
    assert "no extractable text" in result.text
    assert result.page_count == 1


def test_malformed_pdf_raises_value_error():
    with pytest.raises(ValueError, match="Could not parse this PDF"):
        documents.extract_pdf_text(b"%PDF-1.4\nnot actually a valid pdf")


def test_encrypted_pdf_raises_password_protected():
    with pytest.raises(ValueError, match="password-protected"):
        documents.extract_pdf_text(_encrypted_pdf(3))


def test_encrypted_pdf_reports_password_before_page_overflow():
    """Ordering matters: page count is unknowable until decrypted."""
    with pytest.raises(ValueError, match="password-protected"):
        documents.extract_pdf_text(_encrypted_pdf(3), start_page=99)


# --------------------------------------------------------------------------- #
# extract_pdf_text: default-range / auto-preview behaviour
# --------------------------------------------------------------------------- #


def test_short_document_returns_whole_document_by_default():
    data = _pdf_with_pages(documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES, texted=True)
    result = documents.extract_pdf_text(data)
    assert result.auto_previewed is False
    assert result.first_page == 1
    assert result.last_page == documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES
    assert result.page_count == documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES


def test_long_document_is_auto_previewed_by_default():
    total = documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES + 5
    data = _pdf_with_pages(total, texted=True)
    result = documents.extract_pdf_text(data)
    assert result.auto_previewed is True
    assert result.first_page == 1
    assert result.last_page == documents._PDF_PREVIEW_PAGES
    assert result.page_count == total
    assert "Page 1" in result.text
    assert f"Page {documents._PDF_PREVIEW_PAGES + 1}" not in result.text


def test_explicit_range_on_long_document_overrides_auto_preview():
    total = documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES + 20
    data = _pdf_with_pages(total, texted=True)
    result = documents.extract_pdf_text(data, start_page=10, end_page=12)
    assert result.auto_previewed is False
    assert result.first_page == 10
    assert result.last_page == 12
    assert result.page_count == total
    assert "Page 10" in result.text
    assert "Page 13" not in result.text


def test_lone_start_page_reads_to_the_end_not_preview():
    total = documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES + 5
    data = _pdf_with_pages(total, texted=True)
    result = documents.extract_pdf_text(data, start_page=total - 1)
    assert result.auto_previewed is False
    assert result.first_page == total - 1
    assert result.last_page == total


def test_custom_threshold_overrides_the_default():
    data = _pdf_with_pages(10, texted=True)
    result = documents.extract_pdf_text(data, preview_threshold_pages=5)
    assert result.auto_previewed is True
    assert result.last_page == documents._PDF_PREVIEW_PAGES
    assert result.page_count == 10


def test_none_threshold_disables_preview_regardless_of_length():
    total = documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES + 50
    data = _pdf_with_pages(total, texted=True)
    result = documents.extract_pdf_text(data, preview_threshold_pages=None)
    assert result.auto_previewed is False
    assert result.first_page == 1
    assert result.last_page == total
    assert f"Page {total}" in result.text


# --------------------------------------------------------------------------- #
# extract_pdf_text: explicit-range edge cases
# --------------------------------------------------------------------------- #


def test_end_page_past_the_end_clamps_silently():
    data = _pdf_with_pages(3, texted=True)
    result = documents.extract_pdf_text(data, start_page=1, end_page=999)
    assert result.last_page == 3
    assert result.auto_previewed is False


def test_start_page_past_the_end_raises_with_real_count():
    data = _pdf_with_pages(3, texted=True)
    with pytest.raises(ValueError, match="has 3 page"):
        documents.extract_pdf_text(data, start_page=99)


# --------------------------------------------------------------------------- #
# page_notice
# --------------------------------------------------------------------------- #


def test_page_notice_whole_document():
    result = documents.extract_pdf_text(_pdf_with_pages(3, texted=True))
    notice = documents.page_notice(result)
    assert "3 page(s), all of which were extracted" in notice


def test_page_notice_explicit_partial_range():
    data = _pdf_with_pages(20, texted=True)
    result = documents.extract_pdf_text(data, start_page=5, end_page=6)
    notice = documents.page_notice(result)
    assert "20 page(s)" in notice
    assert "pages 5-6" in notice
    assert "by default" not in notice


def test_page_notice_auto_preview_states_true_total_and_how_to_continue():
    total = documents.DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES + 10
    data = _pdf_with_pages(total, texted=True)
    result = documents.extract_pdf_text(data)
    notice = documents.page_notice(result)
    assert f"{total} page(s)" in notice
    assert "by default because it is long" in notice
    assert "table of contents" in notice
    assert f"end_page={total}" in notice


# --------------------------------------------------------------------------- #
# is_docx / is_pptx / is_xlsx / is_legacy_or_encrypted_office
# --------------------------------------------------------------------------- #


def test_is_docx_true_for_docx_bytes():
    assert documents.is_docx(_docx_with_paragraphs(["hello"]))


def test_is_docx_false_for_pdf_and_plain_text():
    assert not documents.is_docx(_pdf_with_pages(1))
    assert not documents.is_docx(b"just a text file")


def test_is_pptx_true_for_pptx_bytes():
    assert documents.is_pptx(_pptx_with_slides(["hello"]))


def test_is_pptx_false_for_docx():
    assert not documents.is_pptx(_docx_with_paragraphs(["hello"]))


def test_is_xlsx_true_for_xlsx_bytes():
    assert documents.is_xlsx(_xlsx_with_sheets({"Sheet1": [["a", "b"]]}))


def test_is_xlsx_false_for_pptx():
    assert not documents.is_xlsx(_pptx_with_slides(["hello"]))


def test_is_legacy_or_encrypted_office_true_for_cfb_magic():
    assert documents.is_legacy_or_encrypted_office(CFB_MAGIC_BYTES)


def test_is_legacy_or_encrypted_office_false_for_modern_office_and_text():
    assert not documents.is_legacy_or_encrypted_office(_docx_with_paragraphs(["hi"]))
    assert not documents.is_legacy_or_encrypted_office(_pdf_with_pages(1))
    assert not documents.is_legacy_or_encrypted_office(b"just a text file")


# --------------------------------------------------------------------------- #
# extract_docx_text
# --------------------------------------------------------------------------- #


def test_extract_docx_text_returns_paragraphs_in_order():
    data = _docx_with_paragraphs(["First paragraph.", "Second paragraph."])
    text = documents.extract_docx_text(data)
    assert "First paragraph." in text
    assert "Second paragraph." in text
    assert text.index("First paragraph.") < text.index("Second paragraph.")


def test_extract_docx_text_appends_tables_separately():
    data = _docx_with_paragraphs(
        ["Intro paragraph."], tables=[[["h1", "h2"], ["v1", "v2"]]]
    )
    text = documents.extract_docx_text(data)
    assert "Intro paragraph." in text
    assert "Tables:" in text
    assert "h1\th2" in text
    assert "v1\tv2" in text
    assert text.index("Intro paragraph.") < text.index("Tables:")


def test_extract_docx_text_empty_document_returns_placeholder():
    data = _docx_with_paragraphs([])
    text = documents.extract_docx_text(data)
    assert "no extractable text" in text


def test_extract_docx_text_malformed_raises_value_error():
    with pytest.raises(ValueError, match="Could not parse this Word document"):
        documents.extract_docx_text(b"not a docx at all")


# --------------------------------------------------------------------------- #
# extract_pptx_text: basic extraction and malformed input
# --------------------------------------------------------------------------- #


def test_extract_pptx_text_extracts_slide_titles_with_labels():
    data = _pptx_with_slides(["Slide One", "Slide Two"])
    result = documents.extract_pptx_text(data)
    assert "[slide 1]" in result.text
    assert "Slide One" in result.text
    assert "[slide 2]" in result.text
    assert "Slide Two" in result.text
    assert result.slide_count == 2
    assert result.auto_previewed is False


def test_extract_pptx_text_malformed_raises_value_error():
    with pytest.raises(ValueError, match="Could not parse this PowerPoint file"):
        documents.extract_pptx_text(b"not a pptx at all")


# --------------------------------------------------------------------------- #
# extract_pptx_text: auto-preview behaviour (mirrors PDF matrix)
# --------------------------------------------------------------------------- #


def test_short_deck_returns_all_slides_by_default():
    total = documents.DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES
    data = _pptx_with_slides([f"Slide {i + 1}" for i in range(total)])
    result = documents.extract_pptx_text(data)
    assert result.auto_previewed is False
    assert result.first_slide == 1
    assert result.last_slide == total
    assert result.slide_count == total


def test_long_deck_is_auto_previewed_by_default():
    total = documents.DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES + 5
    data = _pptx_with_slides([f"Slide {i + 1}" for i in range(total)])
    result = documents.extract_pptx_text(data)
    assert result.auto_previewed is True
    assert result.first_slide == 1
    assert result.last_slide == documents._PPTX_PREVIEW_SLIDES
    assert result.slide_count == total
    assert "Slide 1" in result.text
    assert f"Slide {documents._PPTX_PREVIEW_SLIDES + 1}" not in result.text


def test_explicit_slide_range_overrides_auto_preview():
    total = documents.DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES + 20
    data = _pptx_with_slides([f"Slide {i + 1}" for i in range(total)])
    result = documents.extract_pptx_text(data, start_slide=10, end_slide=12)
    assert result.auto_previewed is False
    assert result.first_slide == 10
    assert result.last_slide == 12
    assert "Slide 10" in result.text
    assert "Slide 13" not in result.text


def test_custom_pptx_threshold_overrides_the_default():
    data = _pptx_with_slides([f"Slide {i + 1}" for i in range(10)])
    result = documents.extract_pptx_text(data, preview_threshold_slides=5)
    assert result.auto_previewed is True
    assert result.last_slide == documents._PPTX_PREVIEW_SLIDES
    assert result.slide_count == 10


def test_none_pptx_threshold_disables_preview_regardless_of_length():
    total = documents.DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES + 50
    data = _pptx_with_slides([f"Slide {i + 1}" for i in range(total)])
    result = documents.extract_pptx_text(data, preview_threshold_slides=None)
    assert result.auto_previewed is False
    assert result.last_slide == total


def test_start_slide_past_the_end_raises_with_real_count():
    data = _pptx_with_slides(["Slide 1", "Slide 2", "Slide 3"])
    with pytest.raises(ValueError, match="has 3 slide"):
        documents.extract_pptx_text(data, start_slide=99)


def test_slide_notice_auto_preview_states_true_total_and_how_to_continue():
    total = documents.DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES + 10
    data = _pptx_with_slides([f"Slide {i + 1}" for i in range(total)])
    result = documents.extract_pptx_text(data)
    notice = documents.slide_notice(result)
    assert f"{total} slide(s)" in notice
    assert "by default because it is long" in notice
    assert f"end_slide={total}" in notice


# --------------------------------------------------------------------------- #
# extract_xlsx_text
# --------------------------------------------------------------------------- #


def test_extract_xlsx_text_extracts_all_sheets_under_threshold():
    data = _xlsx_with_sheets(
        {"Sheet1": [["a", "b"], [1, 2]], "Sheet2": [["c", "d"]]}
    )
    result = documents.extract_xlsx_text(data)
    assert result.sheet_names == ("Sheet1", "Sheet2")
    assert result.extracted_sheets == ("Sheet1", "Sheet2")
    assert result.auto_previewed is False
    assert "[sheet: Sheet1]" in result.text
    assert "a\tb" in result.text
    assert "1\t2" in result.text
    assert "[sheet: Sheet2]" in result.text
    assert "c\td" in result.text


def test_extract_xlsx_text_previews_first_sheet_when_over_threshold():
    sheets = {f"Sheet{i}": [[i]] for i in range(1, 8)}
    data = _xlsx_with_sheets(sheets)
    result = documents.extract_xlsx_text(data, preview_threshold_sheets=5)
    assert result.auto_previewed is True
    assert result.extracted_sheets == ("Sheet1",)
    assert result.sheet_names == tuple(sheets.keys())
    assert "[sheet: Sheet1]" in result.text
    assert "[sheet: Sheet2]" not in result.text


def test_extract_xlsx_text_selects_requested_sheet_by_name():
    data = _xlsx_with_sheets({"Sheet1": [[1]], "Sheet2": [["only", "this"]]})
    result = documents.extract_xlsx_text(data, sheet_name="Sheet2")
    assert result.extracted_sheets == ("Sheet2",)
    assert result.auto_previewed is False
    assert "only\tthis" in result.text
    assert "[sheet: Sheet1]" not in result.text


def test_extract_xlsx_text_unknown_sheet_name_lists_real_names():
    data = _xlsx_with_sheets({"Sheet1": [[1]], "Sheet2": [[2]]})
    with pytest.raises(ValueError, match="Sheet1, Sheet2"):
        documents.extract_xlsx_text(data, sheet_name="Nope")


def test_extract_xlsx_text_malformed_raises_value_error():
    with pytest.raises(ValueError, match="Could not parse this Excel file"):
        documents.extract_xlsx_text(b"not an xlsx at all")


def test_extract_xlsx_text_formula_reads_computed_value_not_formula_text():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = 1
    ws["A2"] = "=A1+1"
    buf = BytesIO()
    wb.save(buf)

    result = documents.extract_xlsx_text(buf.getvalue())
    # No cached value exists (never opened in Excel/LibreOffice), so with
    # data_only=True the formula cell reads as empty, not as the formula text.
    assert "=A1+1" not in result.text


def test_sheet_notice_lists_all_sheet_names_when_previewed():
    sheets = {f"Sheet{i}": [[i]] for i in range(1, 8)}
    data = _xlsx_with_sheets(sheets)
    result = documents.extract_xlsx_text(data, preview_threshold_sheets=5)
    notice = documents.sheet_notice(result)
    assert all(name in notice for name in sheets)
    assert "Only the first sheet" in notice
    assert "sheet_name" in notice


# --------------------------------------------------------------------------- #
# is_image
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP"])
def test_is_image_true_for_every_supported_raster_format(fmt):
    assert documents.is_image(_image_bytes(8, 8, fmt))


def test_is_image_false_for_documents_and_plain_text():
    assert not documents.is_image(_pdf_with_pages(1))
    assert not documents.is_image(_docx_with_paragraphs(["hello"]))
    assert not documents.is_image(CFB_MAGIC_BYTES)
    assert not documents.is_image(b"just a text file")


def test_is_image_false_for_text_that_merely_starts_with_bm():
    # "BM" is the whole BMP signature, which is two bytes and matches ordinary
    # prose. A false positive here is not cosmetic: requires_complete_file
    # consults is_image, so an oversized text file starting "BM" would be
    # refused outright instead of returned as a usable prefix.
    assert not documents.is_image(b"BMW cars, and other things, at length" * 4)


def test_is_image_false_for_a_netpbm_header():
    # Pillow will happily identify "P3" as PPM if it is allowed to try, which
    # is why render_image passes an explicit formats allow-list. The sniffer
    # must not route this here in the first place.
    assert not documents.is_image(b"P3\n4 4\n255\n0 0 0 255 255 255\n")


def test_is_image_recognises_an_image_from_its_first_kilobyte_only():
    # requires_complete_file calls this on a prefix of a download in flight.
    whole = _image_bytes(500, 500, "PNG")
    assert documents.is_image(whole[: documents.SNIFF_BYTES])


# --------------------------------------------------------------------------- #
# requires_complete_file, for images
# --------------------------------------------------------------------------- #


def test_requires_complete_file_true_for_images():
    assert documents.requires_complete_file(_image_bytes(8, 8, "PNG"))
    assert documents.requires_complete_file(_image_bytes(8, 8, "JPEG"))


def test_requires_complete_file_still_false_for_plain_text():
    assert not documents.requires_complete_file(b"a log line\nanother\n")


# --------------------------------------------------------------------------- #
# render_image
# --------------------------------------------------------------------------- #


def test_render_image_downscales_a_large_image_and_keeps_its_aspect_ratio():
    result = documents.render_image(_image_bytes(4000, 3000, "JPEG"))

    assert result.downscaled
    assert max(result.width, result.height) == documents.DEFAULT_MAX_IMAGE_EDGE_PX
    assert result.source_width, result.source_height == (4000, 3000)
    assert abs(result.width / result.height - 4000 / 3000) < 0.01


def test_render_image_never_enlarges_a_small_image():
    result = documents.render_image(_image_bytes(40, 30, "PNG"))

    assert not result.downscaled
    assert (result.width, result.height) == (40, 30)


def test_render_image_reports_the_format_as_a_valid_mime_suffix():
    # "jpg" would produce the invalid media type image/jpg.
    result = documents.render_image(_image_bytes(40, 30, "JPEG"))
    assert result.image_format in ("jpeg", "png")
    assert result.image_format != "jpg"


def test_render_image_keeps_transparency_as_png():
    result = documents.render_image(_image_bytes(60, 60, "PNG", mode="RGBA"))
    assert result.image_format == "png"


def test_render_image_converts_a_photograph_to_jpeg():
    result = documents.render_image(_image_bytes(300, 200, "PNG"))
    assert result.image_format == "jpeg"


def test_render_image_handles_cmyk_and_greyscale_sources():
    # Both decode fine and then have no JPEG representation until converted;
    # a CMYK JPEG written straight back out renders inverted in most viewers.
    for mode, fmt in (("CMYK", "JPEG"), ("L", "PNG"), ("1", "PNG")):
        result = documents.render_image(_image_bytes(50, 50, fmt, mode=mode))
        assert result.data


def test_render_image_applies_exif_orientation():
    # Orientation 6 means "rotate 90° to display", so the axes swap.
    result = documents.render_image(_exif_rotated_jpeg(100, 40, orientation=6))

    assert (result.width, result.height) == (40, 100)
    assert (result.source_width, result.source_height) == (40, 100)


def test_render_image_sends_one_frame_of_an_animation_and_says_how_many():
    result = documents.render_image(_animated_gif_bytes(4))

    assert result.frames == 4
    assert "only the first" in documents.image_notice("/a.gif", result)


def test_render_image_stays_under_the_byte_cap():
    # Random pixels defeat every compressor, so this actually exercises the
    # quality/size ladder rather than compressing away to nothing.
    cap = 20_000
    result = documents.render_image(_image_bytes(1200, 1200, "PNG", noise=True), 1568, cap)

    assert len(result.data) <= cap
    assert result.downscaled


def test_render_image_refuses_a_decompression_bomb_without_decoding_it():
    with pytest.raises(ValueError, match="megapixels"):
        documents.render_image(_oversized_png_header(9000, 9000))


def test_render_image_raises_value_error_on_undecodable_bytes():
    # ValueError, not OSError: _handle_errors in server.py turns ValueError
    # into a clean ToolError and lets anything else escape as a 500.
    with pytest.raises(ValueError, match="could not be decoded"):
        documents.render_image(b"\x89PNG\r\n\x1a\n" + b"not really a png")


# --------------------------------------------------------------------------- #
# image_notice
# --------------------------------------------------------------------------- #


def test_image_notice_names_the_path_format_and_source_size():
    result = documents.render_image(_image_bytes(4000, 3000, "JPEG"))
    notice = documents.image_notice("/photos/plan.jpg", result)

    assert "/photos/plan.jpg" in notice
    assert "JPEG" in notice
    assert "4000x3000" in notice


def test_image_notice_warns_about_lost_detail_only_when_downscaled():
    big = documents.render_image(_image_bytes(4000, 3000, "JPEG"))
    small = documents.render_image(_image_bytes(40, 30, "PNG"))

    assert "downscaled" in documents.image_notice("/a.jpg", big)
    assert "rather than guessing" in documents.image_notice("/a.jpg", big)
    assert "full size" in documents.image_notice("/b.png", small)
    assert "downscaled" not in documents.image_notice("/b.png", small)


# --------------------------------------------------------------------------- #
# decode_text
# --------------------------------------------------------------------------- #


def test_decode_text_reads_plain_utf8_unchanged():
    result = documents.decode_text(GERMAN_TEXT.encode("utf-8"))
    assert result.text == GERMAN_TEXT
    assert (result.codec, result.from_bom, result.replacements) == (
        documents.DEFAULT_TEXT_CODEC,
        False,
        0,
    )


def test_decode_text_strips_a_utf8_bom():
    # Left in place, the U+FEFF ends up inside the first CSV header cell and
    # every match against that column name fails for an invisible reason.
    result = documents.decode_text("Datum\tWert\n".encode("utf-8-sig"))
    assert result.text == "Datum\tWert\n"
    assert not result.text.startswith("﻿")
    assert result.from_bom


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_decode_text_reads_utf16_as_text_not_as_nul_padding(encoding):
    # The failure this replaces decoded without raising and looked like text:
    # replacements for the BOM, then "P\x00r\x00ü\x00..." all the way down.
    raw = GERMAN_TEXT.encode(encoding)
    if encoding == "utf-16-le":
        raw = codecs.BOM_UTF16_LE + raw
    elif encoding == "utf-16-be":
        raw = codecs.BOM_UTF16_BE + raw

    result = documents.decode_text(raw)

    assert result.text == GERMAN_TEXT
    assert "\x00" not in result.text
    assert (result.codec, result.replacements) == ("utf-16", 0)


def test_decode_text_reads_utf32_as_utf32_not_as_utf16():
    # The silent collision: UTF-32LE's FF FE 00 00 starts with the whole of
    # UTF-16LE's FF FE, so a shortest-first BOM scan decodes this without
    # raising and returns nonsense.
    result = documents.decode_text(GERMAN_TEXT.encode("utf-32"))

    assert result.text == GERMAN_TEXT
    assert result.codec == "utf-32"
    assert "\x00" not in result.text


def test_decode_text_counts_what_a_cp1252_export_lost():
    raw = GERMAN_TEXT.encode("cp1252")
    expected = len([c for c in GERMAN_TEXT if ord(c) > 127])

    result = documents.decode_text(raw)

    assert result.replacements == expected
    assert "Prüfungsordnung" not in result.text


def test_decode_text_does_not_count_a_replacement_char_the_file_really_holds():
    # U+FFFD is a legal character and EF BF BD is valid UTF-8, so a count taken
    # from a replace-decoded string means corruption only once strict decoding
    # has actually failed.
    result = documents.decode_text("café �".encode("utf-8"))

    assert result.replacements == 0
    assert result.text == "café �"


def test_decode_text_keeps_the_declared_codec_when_the_file_breaks_it():
    # An odd-length UTF-16 body, i.e. one cut mid-code-unit: the BOM is not a
    # lie, the file is truncated. Falling back to another codec here would be
    # exactly the guess this module refuses to make, so the codec stands and
    # the damage is counted instead.
    result = documents.decode_text(codecs.BOM_UTF16_LE + b"abc")

    assert result.codec == "utf-16"
    assert result.replacements >= 1


def test_decode_text_handles_an_empty_file():
    assert documents.decode_text(b"") == ("", documents.DEFAULT_TEXT_CODEC, False, 0)


def test_decode_notice_is_silent_for_a_file_that_decoded_cleanly():
    assert documents.decode_notice(documents.decode_text(b"fine"), 0) == ""


def test_decode_notice_tells_a_model_not_to_quote_the_damage():
    notice = documents.decode_notice(
        documents.decode_text(GERMAN_TEXT.encode("cp1252")), 5
    )
    assert "do not quote" in notice
    assert "U+FFFD" in notice
    assert "Windows-1252" in notice


def test_decode_notice_reports_damage_that_is_elsewhere_in_the_file():
    # A cp1252 CSV whose first page is ASCII headers would otherwise page
    # cleanly and turn to mojibake several calls later with no warning.
    notice = documents.decode_notice(
        documents.decode_text(GERMAN_TEXT.encode("cp1252")), 0
    )
    assert "elsewhere in this file" in notice
    assert "decoded cleanly" in notice


# --------------------------------------------------------------------------- #
# page_text / check_text_range
# --------------------------------------------------------------------------- #


def test_page_text_returns_everything_when_it_fits():
    page = documents.page_text("short", None, None, 100)
    assert (page.text, page.next_offset, page.total_chars) == ("short", None, 5)


def test_page_text_walks_to_the_end_exactly_once():
    # The property that matters: no gap, no overlap, nothing unreachable.
    text = "".join(chr(0x41 + i % 26) for i in range(1000))
    pages, offset = [], 0
    while offset is not None:
        page = documents.page_text(text, offset, None, 128)
        pages.append(page.text)
        offset = page.next_offset

    assert "".join(pages) == text
    assert len(pages) == 8


def test_page_text_clamps_limit_to_the_server_cap():
    page = documents.page_text("x" * 5000, None, 99999, 1024)
    assert len(page.text) == 1024


def test_page_text_never_manufactures_a_replacement_at_the_cap():
    # Slicing bytes and then decoding cut a multi-byte character in half and
    # produced a U+FFFD that was never in the file.
    text = documents.decode_text(("a" * 9 + "ü" * 10).encode("utf-8")).text
    page = documents.page_text(text, None, None, 12)
    assert "�" not in page.text


def test_page_text_offset_past_the_end_names_the_real_length():
    with pytest.raises(ValueError, match="is past the end"):
        documents.page_text("abc", 99, None, 100)


def test_page_text_offset_zero_on_empty_text_is_not_an_error():
    assert documents.page_text("", 0, None, 100).text == ""


@pytest.mark.parametrize(
    "offset,limit,message",
    [(-1, None, "cannot be negative"), (None, 0, "1 or greater")],
)
def test_check_text_range_rejects_impossible_bounds(offset, limit, message):
    with pytest.raises(ValueError, match=message):
        documents.check_text_range(offset, limit)


def test_continuation_notice_names_the_offset_to_call_back_with():
    page = documents.page_text("x" * 100, 0, None, 40)
    notice = documents.continuation_notice(page)
    assert "offset=40" in notice
    assert "of 100" in notice


# --------------------------------------------------------------------------- #
# is_odt / is_ods / is_odp
# --------------------------------------------------------------------------- #


def test_is_odt_ods_odp_recognise_their_own_format_only():
    odt, ods, odp = (
        _odt_bytes("<text:p>x</text:p>"),
        _ods_bytes({"S": [["a"]]}),
        _odp_bytes(["x"]),
    )
    assert (documents.is_odt(odt), documents.is_ods(odt), documents.is_odp(odt)) == (
        True, False, False,
    )
    assert (documents.is_odt(ods), documents.is_ods(ods), documents.is_odp(ods)) == (
        False, True, False,
    )
    assert (documents.is_odt(odp), documents.is_ods(odp), documents.is_odp(odp)) == (
        False, False, True,
    )


def test_is_odt_false_for_ooxml_pdf_and_plain_text():
    # .docx and .odt are both zips; only the mimetype entry tells them apart.
    for other in (
        _docx_with_paragraphs(["hi"]),
        _pptx_with_slides(["hi"]),
        _xlsx_with_sheets({"S": [["a"]]}),
        _pdf_with_pages(1),
        CFB_MAGIC_BYTES,
        b"just a text file",
    ):
        assert not documents.is_odt(other)
        assert not documents.is_ods(other)
        assert not documents.is_odp(other)


def test_is_odt_accepts_a_template_but_not_a_drawing():
    assert documents.is_odt(
        _odf_bytes(
            "application/vnd.oasis.opendocument.text-template",
            "<office:body><office:text/></office:body>",
        )
    )
    # .odg has no paragraph flow worth extracting, so it keeps falling through.
    graphics = _odf_bytes(
        "application/vnd.oasis.opendocument.graphics",
        "<office:body><office:drawing/></office:body>",
    )
    assert not documents.is_odt(graphics)
    assert not documents.is_odp(graphics)


def test_is_odt_accepts_a_package_that_was_unzipped_and_zipped_again():
    # Re-zipping loses the first-entry/stored requirement, and LibreOffice
    # still opens the result, so refusing it would refuse over a property with
    # no bearing on whether we can read it.
    assert documents.is_odt(_odt_bytes("<text:p>x</text:p>", mimetype_first=False))


def test_is_odt_false_for_a_zip_with_no_mimetype_entry():
    assert not documents.is_odt(
        _odt_bytes("<text:p>x</text:p>", include_mimetype=False)
    )


def test_requires_complete_file_is_already_true_for_odf():
    # ODF is a zip, so an oversized one was already refused before this format
    # was understood at all. Asserted so nobody "adds" it and changes that.
    assert documents.requires_complete_file(_odt_bytes("<text:p>x</text:p>"))


# --------------------------------------------------------------------------- #
# extract_odt_text
# --------------------------------------------------------------------------- #


def test_extract_odt_text_marks_headings_by_outline_level():
    text = documents.extract_odt_text(
        _odt_bytes(
            '<text:h text:outline-level="1">Prüfungsordnung</text:h>'
            '<text:h text:outline-level="2">§ 1</text:h>'
            "<text:p>Absatz.</text:p>"
        )
    )
    assert "# Prüfungsordnung" in text
    assert "## § 1" in text
    assert "Absatz." in text


def test_extract_odt_text_clamps_a_silly_outline_level():
    text = documents.extract_odt_text(
        _odt_bytes('<text:h text:outline-level="9">Tief</text:h>')
    )
    assert text.startswith("###### Tief")


def test_extract_odt_text_keeps_tables_where_they_appear():
    # The .docx path hoists tables to the end; here we walk the body ourselves,
    # so the paragraph that introduces a table stays next to it.
    text = documents.extract_odt_text(
        _odt_bytes(
            "<text:p>Vor der Tabelle.</text:p>"
            '<table:table table:name="M"><table:table-row>'
            "<table:table-cell><text:p>Modul</text:p></table:table-cell>"
            "</table:table-row></table:table>"
            "<text:p>Nach der Tabelle.</text:p>"
        )
    )
    assert text.index("Vor der Tabelle.") < text.index("Modul")
    assert text.index("Modul") < text.index("Nach der Tabelle.")


def test_extract_odt_text_does_not_leak_an_xml_comment():
    # lxml puts comments in the tree as elements. Their text is invisible in
    # LibreOffice, so it has no business reaching a model's context.
    text = documents.extract_odt_text(
        _odt_bytes("<text:p>Sichtbar<!-- versteckte Anweisung --></text:p>")
    )
    assert "Sichtbar" in text
    assert "versteckte" not in text


def test_extract_odt_text_expands_spaces_tabs_and_breaks():
    text = documents.extract_odt_text(
        _odt_bytes(
            '<text:p>A<text:s text:c="3"/>B<text:tab/>C<text:line-break/>D</text:p>'
        )
    )
    assert text == "A   B\tC\nD"


def test_extract_odt_text_clamps_an_absurd_space_run():
    # " " * an attacker-chosen integer is a one-attribute memory bomb.
    text = documents.extract_odt_text(
        _odt_bytes('<text:p>A<text:s text:c="99999999"/>B</text:p>')
    )
    assert len(text) < 2000


def test_extract_odt_text_placeholder_for_an_empty_document():
    assert "no extractable text" in documents.extract_odt_text(_odt_bytes(""))


# --------------------------------------------------------------------------- #
# extract_ods_text
# --------------------------------------------------------------------------- #


def test_extract_ods_text_renders_sheets_as_tab_separated_rows():
    result = documents.extract_ods_text(
        _ods_bytes({"Noten": [["Modul", "LP"], ["Bio", "6"]]})
    )
    assert result.sheet_names == ("Noten",)
    assert "[sheet: Noten]" in result.text
    assert "Modul\tLP" in result.text


def test_extract_ods_text_previews_the_first_sheet_of_many():
    sheets = {f"S{i}": [["x"]] for i in range(1, 8)}
    result = documents.extract_ods_text(_ods_bytes(sheets), None, 5)

    assert result.auto_previewed
    assert result.extracted_sheets == ("S1",)
    assert len(result.sheet_names) == 7
    assert "S7" in documents.ods_sheet_notice(result)


def test_extract_ods_text_selects_one_sheet_by_name():
    result = documents.extract_ods_text(
        _ods_bytes({"A": [["1"]], "B": [["2"]]}), "B", 5
    )
    assert result.extracted_sheets == ("B",)
    assert "2" in result.text and "[sheet: A]" not in result.text


def test_extract_ods_text_unknown_sheet_lists_the_real_names():
    with pytest.raises(ValueError, match="no sheet named"):
        documents.extract_ods_text(_ods_bytes({"A": [["1"]]}), "Nope", 5)


def test_extract_ods_text_repeats_a_populated_cell():
    result = documents.extract_ods_text(
        _ods_bytes(
            raw='<table:table table:name="S"><table:table-row>'
            "<table:table-cell><text:p>LP</text:p></table:table-cell>"
            '<table:table-cell table:number-columns-repeated="2">'
            "<text:p>6</text:p></table:table-cell>"
            "</table:table-row></table:table>"
        )
    )
    assert "LP\t6\t6" in result.text


def test_extract_ods_text_drops_the_padding_libreoffice_writes():
    # LibreOffice pads the last row out to column 16384 and the sheet out to
    # row 1048576 with repeat counts. Expanding those literally turns a
    # two-cell spreadsheet into gigabytes.
    result = documents.extract_ods_text(
        _ods_bytes(
            raw='<table:table table:name="S">'
            "<table:table-row><table:table-cell><text:p>A</text:p></table:table-cell>"
            '<table:table-cell table:number-columns-repeated="16384"/></table:table-row>'
            '<table:table-row table:number-rows-repeated="1048573">'
            "<table:table-cell/></table:table-row></table:table>"
        )
    )
    assert result.text == "[sheet: S]\nA"


def test_extract_ods_text_keeps_the_column_position_of_a_leading_gap():
    result = documents.extract_ods_text(
        _ods_bytes(
            raw='<table:table table:name="S"><table:table-row>'
            '<table:table-cell table:number-columns-repeated="2"/>'
            "<table:table-cell><text:p>Summe</text:p></table:table-cell>"
            "</table:table-row></table:table>"
        )
    )
    assert "\t\tSumme" in result.text


def test_extract_ods_text_falls_back_to_a_typed_value_with_no_paragraph():
    result = documents.extract_ods_text(
        _ods_bytes(
            raw='<table:table table:name="S"><table:table-row>'
            '<table:table-cell office:value-type="float" office:value="42"/>'
            "</table:table-row></table:table>"
        )
    )
    assert "42" in result.text


# --------------------------------------------------------------------------- #
# extract_odp_text
# --------------------------------------------------------------------------- #


def test_extract_odp_text_labels_each_slide():
    result = documents.extract_odp_text(_odp_bytes(["Eins", "Zwei"]))
    assert result.slide_count == 2
    assert "[slide 1]\nEins" in result.text
    assert "[slide 2]\nZwei" in result.text


def test_extract_odp_text_does_not_count_the_master_page_as_a_slide():
    # A real presentation carries a draw:master-page sibling of the slides;
    # counting it would invent a slide nobody wrote.
    assert documents.extract_odp_text(_odp_bytes(["Nur eine"])).slide_count == 1


def test_extract_odp_text_leaves_out_speaker_notes():
    # The .pptx path cannot reach them and PPTX_EXTRACTION_NOTICE promises
    # their absence; two formats making the same promise should keep it alike.
    result = documents.extract_odp_text(_odp_bytes(["Sichtbar"], notes="GEHEIM"))
    assert "Sichtbar" in result.text
    assert "GEHEIM" not in result.text


def test_extract_odp_text_previews_a_long_deck():
    result = documents.extract_odp_text(
        _odp_bytes([f"F{i}" for i in range(1, 26)]), None, None, 20
    )
    assert result.auto_previewed
    assert result.last_slide == 3
    assert "25 slide(s)" in documents.odp_slide_notice(result)


def test_extract_odp_text_honours_an_explicit_range():
    result = documents.extract_odp_text(
        _odp_bytes([f"F{i}" for i in range(1, 26)]), 4, 5, 20
    )
    assert not result.auto_previewed
    assert (result.first_slide, result.last_slide) == (4, 5)
    assert "F4" in result.text and "F1" not in result.text


def test_extract_odp_text_start_slide_past_the_end_names_the_total():
    with pytest.raises(ValueError, match="has 2 slide"):
        documents.extract_odp_text(_odp_bytes(["a", "b"]), 9, None, None)


# --------------------------------------------------------------------------- #
# OpenDocument: encrypted, malformed and hostile input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "extract,build",
    [
        (documents.extract_odt_text, lambda **kw: _odt_bytes("<text:p>x</text:p>", **kw)),
        (documents.extract_ods_text, lambda **kw: _ods_bytes({"S": [["a"]]}, **kw)),
        (documents.extract_odp_text, lambda **kw: _odp_bytes(["x"], **kw)),
    ],
)
def test_a_password_protected_odf_says_so(extract, build):
    # is_legacy_or_encrypted_office sniffs the OLE2 container OOXML encryption
    # uses; ODF uses none, so without this check the file reaches the parser as
    # ciphertext and is reported as merely corrupt.
    with pytest.raises(ValueError, match="password-protected"):
        extract(build(manifest=ENCRYPTED_MANIFEST))


def test_a_file_merely_named_encryption_data_is_not_password_protected():
    manifest = (
        '<?xml version="1.0"?><manifest:manifest '
        'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
        '<manifest:file-entry manifest:full-path="pics/encryption-data.png"/>'
        "</manifest:manifest>"
    )
    assert documents.extract_odt_text(
        _odt_bytes("<text:p>Hallo</text:p>", manifest=manifest)
    ) == "Hallo"


def test_an_unparseable_content_xml_raises_rather_than_returning_garbage():
    with pytest.raises(ValueError, match="Could not parse"):
        documents.extract_odt_text(
            _odt_bytes("", content_override=b"\x00\x01 not xml at all")
        )


def test_an_entity_bomb_is_not_expanded():
    # resolve_entities=False leaves the reference unexpanded, so nesting
    # cannot amplify anything at all rather than merely being rate-limited.
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x ['
        b'<!ENTITY a "AAAAAAAAAA">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
        b'<office:document-content '
        b'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        b'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        b"<office:body><office:text><text:p>&c;</text:p></office:text>"
        b"</office:body></office:document-content>"
    )
    text = documents.extract_odt_text(_odt_bytes("", content_override=bomb))
    assert len(text) < 1000


def test_an_external_entity_cannot_pull_a_local_file_into_the_result():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE x ['
        b'<!ENTITY secret SYSTEM "file:///etc/hostname">]>'
        b'<office:document-content '
        b'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        b'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        b"<office:body><office:text><text:p>&secret;</text:p></office:text>"
        b"</office:body></office:document-content>"
    )
    text = documents.extract_odt_text(_odt_bytes("", content_override=xxe))
    assert "/etc" not in text


# --------------------------------------------------------------------------- #
# PDF text layer: which pages have one (shared with the OCR tool)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text,expected", [("", False), (" \n\t", False), ("x", True)])
def test_has_text_layer_ignores_whitespace(text, expected):
    assert documents.has_text_layer(text) is expected


def test_open_pdf_rejects_a_password_protected_file():
    with pytest.raises(ValueError, match="password-protected"):
        documents.open_pdf(_encrypted_pdf(2))


def test_open_pdf_rejects_garbage():
    with pytest.raises(ValueError, match="Could not parse this PDF"):
        documents.open_pdf(b"%PDF-1.4\nnot actually a valid pdf")


def test_a_scanned_page_inside_a_typed_pdf_is_reported_not_dropped():
    """Before this, a textless page in the middle of a document simply vanished
    from the output: the model saw pages 1 and 3 and no sign of 2."""
    result = documents.extract_pdf_text(_pdf_with_pages(4, texted=True, blank=(2, 4)))

    assert result.empty_pages == (2, 4)
    assert "[page 1]" in result.text and "[page 3]" in result.text


def test_a_fully_typed_pdf_has_no_empty_pages():
    assert documents.extract_pdf_text(_pdf_with_pages(3, texted=True)).empty_pages == ()


def test_empty_pages_are_counted_within_the_selected_range_only():
    data = _pdf_with_pages(6, texted=True, blank=(2, 5))
    result = documents.extract_pdf_text(data, start_page=3, end_page=6)
    assert result.empty_pages == (5,)


def test_the_no_text_placeholder_no_longer_claims_there_is_no_ocr():
    """Whether OCR exists is a deployment fact the notice states; the
    placeholder is a fact about the PDF and must be true either way."""
    result = documents.extract_pdf_text(_pdf_with_pages(1))
    assert "no extractable text" in result.text
    assert "OCR" not in result.text
    assert "OCR" not in documents.PDF_EXTRACTION_NOTICE


@pytest.mark.parametrize(
    "pages,phrase",
    [
        ([3], "page 3"),
        ([1, 2], "pages 1-2"),
        ([1, 3], "pages 1, 3"),
        ([3, 4, 5, 9], "pages 3-5, 9"),
        ([9, 3, 4], "pages 3-4, 9"),
    ],
)
def test_pages_phrase_compresses_runs(pages, phrase):
    assert documents.pages_phrase(pages) == phrase


def test_empty_pages_notice_is_silent_when_every_page_has_text():
    result = documents.extract_pdf_text(_pdf_with_pages(2, texted=True))
    assert documents.empty_pages_notice(result) == ""


def test_empty_pages_notice_says_there_is_no_ocr():
    result = documents.extract_pdf_text(_pdf_with_pages(3, texted=True, blank=(2,)))
    notice = documents.empty_pages_notice(result)
    assert "page 2" in notice
    assert "does not do OCR" in notice
    assert "seafile_ocr_file" not in notice


def test_page_texts_cover_the_range_with_blanks_for_textless_pages():
    result = documents.extract_pdf_text(_pdf_with_pages(3, texted=True, blank=(2,)))
    assert len(result.page_texts) == 3
    assert result.page_texts[1] == ""
    assert "Page 3" in result.page_texts[2]


# --------------------------------------------------------------------------- #
# decode_image / identify_image: the safety steps render_image and OCR share
# --------------------------------------------------------------------------- #


def test_identify_image_reads_format_and_frame_count_from_the_header():
    assert documents.identify_image(_multipage_tiff([(30, 20), (40, 50)])) == ("TIFF", 2)
    assert documents.identify_image(_image_bytes(10, 10, "PNG")) == ("PNG", 1)


def test_identify_image_raises_value_error_on_undecodable_bytes():
    with pytest.raises(ValueError, match="could not be decoded"):
        documents.identify_image(b"\x89PNG\r\n\x1a\n" + b"not really a png")


def test_decode_image_selects_a_tiff_page():
    data = _multipage_tiff([(30, 20), (40, 50), (60, 10)])

    second = documents.decode_image(data, 1568, frame=1)

    assert second.image.size == (40, 50)
    assert (second.source_width, second.source_height) == (40, 50)
    assert second.frames == 3
    assert second.source_format == "TIFF"


def test_decode_image_refuses_a_frame_past_the_end():
    with pytest.raises(ValueError, match="frame"):
        documents.decode_image(_multipage_tiff([(30, 20)]), 1568, frame=3)


def test_decode_image_keeps_the_decompression_bomb_check():
    with pytest.raises(ValueError, match="megapixels"):
        documents.decode_image(_oversized_png_header(9000, 9000), 1568)


def test_decode_image_does_not_resize():
    """Resizing is the caller's choice: render_image and OCR want different
    edges, so decode_image only decodes."""
    decoded = documents.decode_image(_image_bytes(2000, 1000, "PNG"), 1568)
    assert decoded.image.size == (2000, 1000)
