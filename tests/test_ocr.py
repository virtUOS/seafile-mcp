"""OCR: the engine that talks to an OpenAI-compatible endpoint, and the page
orchestration around it (which pages to send, how many at once, what to say)."""

from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO

import httpx
import pytest
import respx
from PIL import Image as PILImage
from pypdf import PdfWriter

from seafile_mcp import ocr
from seafile_mcp.config import Settings

from .conftest import (
    SERVER,
    animated_gif_bytes,
    encrypted_pdf,
    image_bytes,
    multipage_tiff,
    oversized_png_header,
    pdf_with_pages,
    png_bytes,
    scanned_pdf,
    word_image,
)

OCR_URL = "http://gpu.test:8010/v1"
COMPLETIONS = f"{OCR_URL}/chat/completions"
PNG = image_bytes(10, 10, "PNG")


def _settings(**kw) -> Settings:
    kw.setdefault("ocr_url", OCR_URL)
    kw.setdefault("ocr_model", "lightonai/LightOnOCR-2-1B")
    return Settings(SEAFILE_SERVER_URL=SERVER, **kw)


def _completion(text: str | None = "transcribed", finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish,
                }
            ]
        },
    )


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def _png_size(png: bytes) -> tuple[int, int]:
    return PILImage.open(BytesIO(png)).size


# --------------------------------------------------------------------------- #
# The engine: request shape
# --------------------------------------------------------------------------- #


async def test_the_request_is_one_png_data_url_and_no_prompt_by_default():
    """LightOnOCR's plain mode is the image with no text part at all."""
    engine = ocr.OpenAICompatibleOcr(_settings())
    with respx.mock:
        route = respx.post(COMPLETIONS).mock(return_value=_completion())
        await engine.recognize(PNG)

    body = _sent(route)
    assert body["model"] == "lightonai/LightOnOCR-2-1B"
    [message] = body["messages"]
    assert message["role"] == "user"
    [part] = message["content"]
    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


async def test_a_configured_prompt_follows_the_image():
    engine = ocr.OpenAICompatibleOcr(_settings(ocr_prompt="Transcribe this page."))
    with respx.mock:
        route = respx.post(COMPLETIONS).mock(return_value=_completion())
        await engine.recognize(PNG)

    content = _sent(route)["messages"][0]["content"]
    assert [p["type"] for p in content] == ["image_url", "text"]
    assert content[1]["text"] == "Transcribe this page."


async def test_sampling_settings_and_extra_body_are_sent():
    engine = ocr.OpenAICompatibleOcr(
        _settings(
            ocr_temperature="0.2",
            ocr_max_tokens="1000",
            ocr_extra_body='{"chat_template_kwargs": {"enable_thinking": false}}',
        )
    )
    with respx.mock:
        route = respx.post(COMPLETIONS).mock(return_value=_completion())
        await engine.recognize(PNG)

    body = _sent(route)
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 1000
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body.get("stream") in (None, False)


async def test_the_api_key_is_sent_as_bearer_only_when_set():
    with respx.mock:
        route = respx.post(COMPLETIONS).mock(return_value=_completion())
        await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)
        assert "authorization" not in route.calls.last.request.headers

        await ocr.OpenAICompatibleOcr(_settings(ocr_api_key="sk-1")).recognize(PNG)
        assert route.calls.last.request.headers["authorization"] == "Bearer sk-1"


# --------------------------------------------------------------------------- #
# The engine: reading the answer
# --------------------------------------------------------------------------- #


async def test_string_content_is_returned_trimmed():
    with respx.mock:
        respx.post(COMPLETIONS).mock(return_value=_completion("\n# Title\n\nBody\n"))
        page = await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert page.text == "# Title\n\nBody"
    assert page.hit_token_limit is False


async def test_content_given_as_parts_is_joined():
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "first "},
                            {"type": "text", "text": "second"},
                        ]
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    )
    with respx.mock:
        respx.post(COMPLETIONS).mock(return_value=response)
        page = await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert page.text == "first second"


async def test_null_content_is_empty_text():
    with respx.mock:
        respx.post(COMPLETIONS).mock(return_value=_completion(None))
        page = await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert page.text == ""


async def test_stopping_at_the_token_limit_is_flagged():
    """A looping model fills max_tokens; the caller has to know the page may
    be cut short or repetitive rather than receive it as a clean result."""
    with respx.mock:
        respx.post(COMPLETIONS).mock(return_value=_completion("a a a", finish="length"))
        page = await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert page.hit_token_limit is True


# --------------------------------------------------------------------------- #
# The engine: failures never name the OCR host
# --------------------------------------------------------------------------- #


async def test_a_timeout_says_how_long_and_not_where():
    with respx.mock:
        respx.post(COMPLETIONS).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(ocr.OcrError) as info:
            await ocr.OpenAICompatibleOcr(_settings(ocr_timeout_s="45")).recognize(PNG)

    assert "45 s" in str(info.value)
    assert "gpu.test" not in str(info.value)


async def test_an_unreachable_service_does_not_leak_its_address():
    with respx.mock:
        respx.post(COMPLETIONS).mock(side_effect=httpx.ConnectError("refused gpu.test"))
        with pytest.raises(ocr.OcrError, match="unreachable") as info:
            await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert "gpu.test" not in str(info.value)


async def test_an_error_status_is_reported_without_the_body():
    with respx.mock:
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(500, text="internal detail at gpu.test")
        )
        with pytest.raises(ocr.OcrError, match="500") as info:
            await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert "internal detail" not in str(info.value)
    assert "gpu.test" not in str(info.value)


async def test_a_redirect_is_not_followed():
    """Following it would send a page image of someone's file wherever the
    Location header points."""
    with respx.mock:
        respx.post(COMPLETIONS).mock(
            return_value=httpx.Response(307, headers={"Location": "https://evil.test/x"})
        )
        evil = respx.post("https://evil.test/x").mock(return_value=_completion())
        with pytest.raises(ocr.OcrError, match="307"):
            await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert not evil.called


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"object": "error"}),
    ],
)
async def test_an_unreadable_answer_is_an_ocr_error(response):
    with respx.mock:
        respx.post(COMPLETIONS).mock(return_value=response)
        with pytest.raises(ocr.OcrError, match="could not read"):
            await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)


# --------------------------------------------------------------------------- #
# Orchestration, with a stand-in engine
# --------------------------------------------------------------------------- #


class FakeEngine:
    """Records what it was sent. ``fail_on``/``limit_on`` are 1-indexed call
    numbers; run with concurrency 1 to make call order equal page order."""

    def __init__(
        self,
        text: str = "transcribed",
        *,
        fail_on: tuple[int, ...] = (),
        limit_on: tuple[int, ...] = (),
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.text = text
        self.fail_on = fail_on
        self.limit_on = limit_on
        self.delay = delay
        self.error = error
        self.calls: list[bytes] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def recognize(self, png: bytes) -> ocr.OcrPage:
        self.calls.append(png)
        n = len(self.calls)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            if n in self.fail_on:
                raise ocr.OcrError("The OCR service returned HTTP 500.")
            return ocr.OcrPage(self.text, n in self.limit_on)
        finally:
            self.in_flight -= 1


def _service(engine: FakeEngine, **kw) -> ocr.OcrService:
    return ocr.OcrService(engine, _settings(**kw))


async def test_a_scanned_pdf_sends_every_page():
    engine = FakeEngine()
    result = await _service(engine).transcribe(scanned_pdf(3), None, None, False)

    assert len(engine.calls) == 3
    assert result.ocr_pages == (1, 2, 3)
    assert "[page 2] (OCR)\ntranscribed" in result.text


async def test_pages_with_a_text_layer_are_not_sent():
    """The point of the feature's cost model: a typed page costs no GPU time."""
    engine = FakeEngine()
    data = scanned_pdf(4, typed=(1, 3))

    result = await _service(engine).transcribe(data, None, None, False)

    assert len(engine.calls) == 2
    assert result.ocr_pages == (2, 4)
    assert result.text_layer_pages == (1, 3)
    assert "[page 1]\nPage 1" in result.text
    assert "[page 2] (OCR)\ntranscribed" in result.text


async def test_a_fully_typed_pdf_sends_nothing_and_still_returns_its_text():
    engine = FakeEngine()
    result = await _service(engine).transcribe(
        pdf_with_pages(3, texted=True), None, None, False
    )

    assert engine.calls == []
    assert "Page 3" in result.text
    assert result.ocr_pages == ()


async def test_force_ocr_sends_pages_that_have_a_text_layer():
    """For a copier's 'searchable PDF', whose hidden text layer is often junk."""
    engine = FakeEngine()
    data = scanned_pdf(4, typed=(1, 3, 4))

    result = await _service(engine).transcribe(data, None, None, True)

    assert len(engine.calls) == 4
    assert result.text_layer_pages == ()
    assert "Page 1" not in result.text


async def test_pages_reach_the_engine_rendered_at_the_configured_edge():
    engine = FakeEngine()
    await _service(engine, ocr_max_edge_px="800").transcribe(
        scanned_pdf(1), None, None, False
    )

    assert _png_size(engine.calls[0]) == (800, 800)


async def test_a_landscape_page_keeps_its_aspect_ratio():
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=100)
    ink = DecodedStreamObject()
    ink.set_data(b"0 g 20 20 260 40 re f")
    page[NameObject("/Contents")] = writer._add_object(ink)
    buf = BytesIO()
    writer.write(buf)

    engine = FakeEngine()
    await _service(engine, ocr_max_edge_px="900").transcribe(
        buf.getvalue(), None, None, False
    )

    assert _png_size(engine.calls[0]) == (900, 300)


async def test_a_bare_call_covers_at_most_max_pages():
    engine = FakeEngine()
    result = await _service(engine, ocr_max_pages="5").transcribe(
        scanned_pdf(12), None, None, False
    )

    assert (result.first_page, result.last_page, result.page_count) == (1, 5, 12)
    assert result.clamped is True
    assert len(engine.calls) == 5


async def test_an_explicit_range_is_clamped_to_max_pages():
    engine = FakeEngine()
    result = await _service(engine, ocr_max_pages="5").transcribe(
        scanned_pdf(20), 3, 12, False
    )

    assert (result.first_page, result.last_page) == (3, 7)
    assert result.clamped is True


async def test_a_range_within_the_cap_is_honoured_exactly():
    engine = FakeEngine()
    result = await _service(engine).transcribe(scanned_pdf(20), 4, 6, False)

    assert (result.first_page, result.last_page, result.clamped) == (4, 6, False)
    assert len(engine.calls) == 3


async def test_a_start_past_the_end_is_an_error():
    with pytest.raises(ValueError, match="past the end"):
        await _service(FakeEngine()).transcribe(scanned_pdf(3), 9, None, False)


async def test_a_password_protected_pdf_is_refused():
    with pytest.raises(ValueError, match="password-protected"):
        await _service(FakeEngine()).transcribe(encrypted_pdf(2), None, None, False)


async def test_a_file_that_is_neither_pdf_nor_image_is_refused():
    with pytest.raises(ValueError, match="PDFs and images"):
        await _service(FakeEngine()).transcribe(b"plain text", None, None, False)


async def test_a_failed_page_keeps_the_pages_that_worked():
    """Nothing is cached, so discarding nine good pages over one timeout would
    make the caller pay for all ten again."""
    engine = FakeEngine(fail_on=(2,))
    result = await _service(engine, ocr_concurrency="1").transcribe(
        scanned_pdf(3), None, None, False
    )

    assert result.ocr_pages == (1, 3)
    assert [p for p, _ in result.failed_pages] == [2]
    assert "HTTP 500" in result.failed_pages[0][1]
    assert "[page 1] (OCR)\ntranscribed" in result.text
    assert "[page 2] (OCR failed" in result.text


async def test_every_page_failing_is_an_error():
    engine = FakeEngine(fail_on=(1, 2))
    with pytest.raises(ocr.OcrError, match="HTTP 500"):
        await _service(engine).transcribe(scanned_pdf(2), None, None, False)


async def test_a_bug_is_not_mistaken_for_a_failed_page():
    engine = FakeEngine(error=RuntimeError("bug"))
    with pytest.raises(RuntimeError, match="bug"):
        await _service(engine).transcribe(scanned_pdf(2), None, None, False)


async def test_concurrency_is_bounded_across_callers():
    """The cap is process-wide: two users' documents share it."""
    engine = FakeEngine(delay=0.02)
    service = _service(engine, ocr_concurrency="2")

    await asyncio.gather(
        service.transcribe(scanned_pdf(4), None, None, False),
        service.transcribe(scanned_pdf(4), None, None, False),
    )

    assert len(engine.calls) == 8
    assert engine.max_in_flight == 2


async def test_progress_is_reported_after_each_page():
    seen: list[tuple[int, int]] = []

    async def progress(done: int, total: int) -> None:
        seen.append((done, total))

    await _service(FakeEngine()).transcribe(
        scanned_pdf(3, typed=(2,)), None, None, False, progress
    )

    assert seen == [(1, 2), (2, 2)]


async def test_pages_that_hit_the_token_limit_are_recorded():
    engine = FakeEngine(limit_on=(1,))
    result = await _service(engine, ocr_concurrency="1").transcribe(
        scanned_pdf(2), None, None, False
    )

    assert result.token_limited_pages == (1,)


async def test_an_empty_transcription_says_so_rather_than_vanishing():
    result = await _service(FakeEngine(text="")).transcribe(
        scanned_pdf(1), None, None, False
    )

    assert "[page 1] (OCR)\n(no text recognised on this page)" in result.text
    assert result.no_text_pages == (1,)
    assert result.blank_pages == ()


# --------------------------------------------------------------------------- #
# Orchestration: images
# --------------------------------------------------------------------------- #


async def test_a_single_image_is_one_unlabelled_page():
    engine = FakeEngine()
    result = await _service(engine).transcribe(image_bytes(40, 30, noise=True), None, None, False)

    assert len(engine.calls) == 1
    assert result.text == "transcribed"
    assert result.source == "PNG"


async def test_a_large_image_is_downscaled_for_ocr():
    engine = FakeEngine()
    await _service(engine).transcribe(image_bytes(3080, 1540, "JPEG", noise=True), None, None, False)

    assert _png_size(engine.calls[0]) == (1540, 770)


async def test_a_small_image_is_never_enlarged():
    engine = FakeEngine()
    await _service(engine).transcribe(image_bytes(100, 50, noise=True), None, None, False)

    assert _png_size(engine.calls[0]) == (100, 50)


async def test_the_pages_of_a_tiff_are_pages():
    """A multi-page scan saved as TIFF must not be cut to its first page."""
    engine = FakeEngine()
    data = multipage_tiff([(30, 20), (40, 50), (60, 10)])

    result = await _service(engine, ocr_concurrency="1").transcribe(
        data, None, None, False
    )

    assert [_png_size(c) for c in engine.calls] == [(30, 20), (40, 50), (60, 10)]
    assert result.page_count == 3
    assert "[page 3] (OCR)\ntranscribed" in result.text


async def test_a_page_range_selects_tiff_pages():
    engine = FakeEngine()
    data = multipage_tiff([(30, 20), (40, 50), (60, 10)])

    await _service(engine).transcribe(data, 2, 2, False)

    assert [_png_size(c) for c in engine.calls] == [(40, 50)]


async def test_an_animation_sends_its_first_frame_only_and_says_so():
    engine = FakeEngine()
    result = await _service(engine).transcribe(animated_gif_bytes(5), None, None, False)

    assert len(engine.calls) == 1
    assert result.animation_frames == 5
    assert "only the first" in ocr.ocr_notice(result)


async def test_transparency_is_flattened_onto_white():
    """Dark text on a transparent background is the usual screenshot; on black
    it would be invisible to the OCR model."""
    from PIL import ImageDraw

    img = PILImage.new("RGBA", (60, 40), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((10, 10, 50, 20), fill=(0, 0, 0, 255))
    buf = BytesIO()
    img.save(buf, "PNG")

    engine = FakeEngine()
    await _service(engine).transcribe(buf.getvalue(), None, None, False)

    sent = PILImage.open(BytesIO(engine.calls[0]))
    assert sent.mode == "RGB"
    assert sent.getpixel((2, 2)) == (255, 255, 255)
    assert sent.getpixel((30, 15)) == (0, 0, 0)


async def test_an_image_bomb_is_refused():
    with pytest.raises(ocr.OcrError, match="megapixels"):
        await _service(FakeEngine()).transcribe(
            oversized_png_header(9000, 9000), None, None, False
        )


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #


async def _notice_for(data: bytes, *args, engine: FakeEngine | None = None, **kw) -> str:
    result = await _service(engine or FakeEngine(), **kw).transcribe(data, *args)
    return ocr.ocr_notice(result)


def test_the_ocr_notice_says_this_is_a_machine_transcription():
    assert "machine transcription" in ocr.OCR_NOTICE
    assert "misread" in ocr.OCR_NOTICE


async def test_the_notice_states_the_true_total_and_the_range_covered():
    notice = await _notice_for(scanned_pdf(12), None, None, False, ocr_max_pages="5")
    assert "12 page(s)" in notice
    assert "pages 1-5" in notice
    assert "start_page=6" in notice


async def test_the_notice_names_text_layer_pages_and_how_to_force_them():
    notice = await _notice_for(
        scanned_pdf(4, typed=(1, 3, 4)), None, None, False
    )
    assert "pages 1, 3-4" in notice
    assert "force_ocr=true" in notice


async def test_the_notice_says_when_nothing_needed_ocr():
    notice = await _notice_for(pdf_with_pages(2, texted=True), None, None, False)
    assert "None of these pages needed OCR" in notice
    assert "force_ocr=true" in notice


async def test_the_notice_lists_failed_pages_and_how_to_retry():
    notice = await _notice_for(
        scanned_pdf(3),
        None,
        None,
        False,
        engine=FakeEngine(fail_on=(2,)),
        ocr_concurrency="1",
    )
    assert "page 2" in notice
    assert "start_page=2" in notice


async def test_the_notice_warns_about_token_limited_pages():
    notice = await _notice_for(
        scanned_pdf(1), None, None, False, engine=FakeEngine(limit_on=(1,))
    )
    assert "output limit" in notice


async def test_a_page_whose_text_layer_cannot_be_read_is_sent_to_ocr(monkeypatch):
    """Damaged pages are common in scans, and OCR is exactly what they need: a
    parser error on one page must not sink the whole call."""
    from pypdf import PageObject

    real = PageObject.extract_text

    def flaky(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        if "Page 2" in text:
            raise KeyError("/Font")
        return text

    monkeypatch.setattr(PageObject, "extract_text", flaky)
    engine = FakeEngine()

    result = await _service(engine).transcribe(
        pdf_with_pages(3, texted=True), None, None, False
    )

    assert result.ocr_pages == (2,)
    assert result.text_layer_pages == (1, 3)


def test_a_closed_pdf_is_never_reopened_by_a_late_render():
    """A render queued in a worker thread can outlive a cancelled call. If it
    reopened the document after close(), nothing would close it again and a
    GC finalizer would make the PDFium call later, outside the lock."""
    pages = ocr._PdfPages(scanned_pdf(2), 100)
    pages.render(1, False)
    pages.close()

    with pytest.raises(ValueError, match="closed"):
        pages.render(2, False)
    assert pages._doc is None


# --------------------------------------------------------------------------- #
# The blank-page check
# --------------------------------------------------------------------------- #


def test_an_empty_page_looks_blank():
    assert ocr._looks_blank(word_image("")) is True


def test_scanner_speckle_alone_still_looks_blank():
    """Isolated dark pixels are what a scanner leaves on an empty page; they
    touch no other ink, so they are not counted."""
    assert ocr._looks_blank(word_image("", speckles=600)) is True


@pytest.mark.parametrize("word", ["Ja", "3"])
def test_one_small_word_is_never_judged_blank(word):
    """The threshold errs towards sending: skipping a page with a word on it
    loses content, sending an empty one only wastes a request."""
    assert ocr._looks_blank(word_image(word, size=15)) is False


def test_white_text_on_a_dark_slide_is_not_blank():
    assert ocr._looks_blank(word_image("Ja", inverted=True)) is False


def test_a_solid_colour_looks_blank():
    from PIL import Image as PILImage

    assert ocr._looks_blank(PILImage.new("RGB", (400, 300), (40, 90, 160))) is True


async def test_blank_pdf_pages_are_not_sent():
    engine = FakeEngine()
    result = await _service(engine).transcribe(pdf_with_pages(3), None, None, False)

    assert engine.calls == []
    assert result.blank_pages == (1, 2, 3)
    assert "[page 2] (appears blank; not sent to OCR)" in result.text


async def test_force_ocr_sends_blank_pages_too():
    engine = FakeEngine()
    await _service(engine).transcribe(pdf_with_pages(2), None, None, True)

    assert len(engine.calls) == 2


async def test_the_blank_check_can_be_switched_off():
    engine = FakeEngine()
    await _service(engine, ocr_skip_blank_pages="false").transcribe(
        pdf_with_pages(2), None, None, False
    )

    assert len(engine.calls) == 2


async def test_a_blank_image_is_not_sent():
    engine = FakeEngine()
    result = await _service(engine).transcribe(
        png_bytes(word_image("")), None, None, False
    )

    assert engine.calls == []
    assert "appears blank" in result.text


async def test_an_image_with_a_word_is_sent():
    engine = FakeEngine()
    await _service(engine).transcribe(png_bytes(word_image("Ja")), None, None, False)

    assert len(engine.calls) == 1


async def test_the_notice_says_blank_pages_were_skipped_and_how_to_force():
    notice = await _notice_for(pdf_with_pages(2), None, None, False)
    assert "pages 1-2" in notice
    assert "blank" in notice
    assert "force_ocr=true" in notice


# --------------------------------------------------------------------------- #
# Outages: fail fast within a call, then cool down for everyone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "failure",
    [
        {"side_effect": httpx.ReadTimeout("hung")},
        {"side_effect": httpx.ConnectTimeout("no route")},
        {"side_effect": httpx.ConnectError("refused")},
        {"return_value": httpx.Response(502)},
        {"return_value": httpx.Response(503)},
        {"return_value": httpx.Response(504)},
        {"return_value": httpx.Response(429)},
    ],
)
async def test_a_service_level_failure_is_an_outage(failure):
    with respx.mock:
        respx.post(COMPLETIONS).mock(**failure)
        with pytest.raises(ocr.OcrUnavailable):
            await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)


@pytest.mark.parametrize("status", [400, 500])
async def test_a_page_level_failure_is_not_an_outage(status):
    """A bad request or a crash on one image says nothing about the next page."""
    with respx.mock:
        respx.post(COMPLETIONS).mock(return_value=httpx.Response(status))
        with pytest.raises(ocr.OcrError) as info:
            await ocr.OpenAICompatibleOcr(_settings()).recognize(PNG)

    assert not isinstance(info.value, ocr.OcrUnavailable)


def _down() -> ocr.OcrUnavailable:
    return ocr.OcrUnavailable("The OCR service did not answer within 120 s.")


def _frozen(service: ocr.OcrService, start: float = 1000.0) -> list[float]:
    """Give the service a clock the test moves by hand."""
    now = [start]
    service._clock = lambda: now[0]
    return now


async def test_an_outage_stops_the_rest_of_the_call():
    """Otherwise every page waits out its own timeout: ten pages behind a hung
    server is ten timeouts, and the client gives up long before."""
    engine = FakeEngine(error=_down())
    service = _service(engine, ocr_concurrency="1")

    results = await service.transcribe_pdf_pages(scanned_pdf(5), [1, 2, 3, 4, 5])

    assert len(engine.calls) == 1
    assert {r.status for r in results.values()} == {"failed"}
    assert all("120 s" in r.reason for r in results.values())


async def test_an_outage_costs_at_most_the_requests_already_in_flight():
    engine = FakeEngine(error=_down(), delay=0.01)
    service = _service(engine, ocr_concurrency="2")

    await service.transcribe_pdf_pages(scanned_pdf(6), [1, 2, 3, 4, 5, 6])

    # At most: a page still rendering when the first answer arrives sees the
    # cool-down and is never sent.
    assert 1 <= len(engine.calls) <= 2


async def test_after_an_outage_ocr_is_not_tried_until_it_cools_down():
    engine = FakeEngine(error=_down())
    service = _service(engine)
    now = _frozen(service)
    await service.transcribe_pdf_pages(scanned_pdf(1), [1])
    engine.error = None

    during = await service.transcribe_pdf_pages(scanned_pdf(2), [1, 2])

    assert len(engine.calls) == 1
    assert "temporarily unavailable" in during[1].reason
    assert "120 s" in during[1].reason  # says why
    assert "60 s" in during[1].reason  # and when to try again

    now[0] += 61
    after = await service.transcribe_pdf_pages(scanned_pdf(2), [1, 2])

    assert len(engine.calls) == 3
    assert {r.status for r in after.values()} == {"ocr"}


async def test_the_cool_down_covers_every_caller_of_the_process():
    """One user's timeout spares the next user the same wait, through either
    tool."""
    engine = FakeEngine(error=_down())
    service = _service(engine)
    _frozen(service)
    await service.transcribe_pdf_pages(scanned_pdf(1), [1])
    engine.error = None

    with pytest.raises(ocr.OcrError, match="temporarily unavailable"):
        await service.transcribe(png_bytes(word_image("Ja")), None, None, False)
    assert len(engine.calls) == 1


async def test_a_page_level_failure_does_not_start_a_cool_down():
    engine = FakeEngine(fail_on=(1,))
    service = _service(engine, ocr_concurrency="1")

    results = await service.transcribe_pdf_pages(scanned_pdf(3), [1, 2, 3])

    assert len(engine.calls) == 3
    assert [r.status for _, r in sorted(results.items())] == ["failed", "ocr", "ocr"]


async def test_blank_pages_are_still_recognised_during_an_outage():
    """The blank check is local; an outage is no reason to mislabel an empty
    page as a failed one."""
    engine = FakeEngine(error=_down())
    service = _service(engine)
    _frozen(service)
    await service.transcribe_pdf_pages(scanned_pdf(1), [1])

    results = await service.transcribe_pdf_pages(pdf_with_pages(1), [1])

    assert results[1].status == "blank"
