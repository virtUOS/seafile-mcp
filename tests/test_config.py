"""Settings parsing that isn't exercised elsewhere: the preview-threshold fields."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from seafile_mcp.config import Settings
from seafile_mcp.documents import (
    DEFAULT_MAX_IMAGE_EDGE_PX,
    DEFAULT_MAX_IMAGE_MB,
    DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES,
    DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES,
    DEFAULT_XLSX_PREVIEW_THRESHOLD_SHEETS,
)

SERVER = "https://seafile.test"

#: (settings field name, its default) for the three preview-threshold fields.
#: They all share the same "all"-or-positive-int validators (config.py), so
#: their parsing behaviour is tested once, parametrized, rather than tripled.
THRESHOLD_FIELDS = [
    ("pdf_preview_threshold_pages", DEFAULT_PDF_PREVIEW_THRESHOLD_PAGES),
    ("pptx_preview_threshold_slides", DEFAULT_PPTX_PREVIEW_THRESHOLD_SLIDES),
    ("xlsx_preview_threshold_sheets", DEFAULT_XLSX_PREVIEW_THRESHOLD_SHEETS),
]


@pytest.mark.parametrize("field,default", THRESHOLD_FIELDS)
def test_preview_threshold_defaults_without_hardcoding_in_two_places(field, default):
    settings = Settings(SEAFILE_SERVER_URL=SERVER)
    assert getattr(settings, field) == default


@pytest.mark.parametrize("field,default", THRESHOLD_FIELDS)
def test_preview_threshold_accepts_a_custom_int(field, default):
    settings = Settings(SEAFILE_SERVER_URL=SERVER, **{field: "7"})
    assert getattr(settings, field) == 7


@pytest.mark.parametrize("field,default", THRESHOLD_FIELDS)
@pytest.mark.parametrize("value", ["all", "ALL", " All "])
def test_preview_threshold_all_disables_preview(field, default, value):
    settings = Settings(SEAFILE_SERVER_URL=SERVER, **{field: value})
    assert getattr(settings, field) is None


@pytest.mark.parametrize("field,default", THRESHOLD_FIELDS)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_preview_threshold_rejects_non_positive(field, default, value):
    with pytest.raises(ValidationError, match="positive integer"):
        Settings(SEAFILE_SERVER_URL=SERVER, **{field: value})


# --------------------------------------------------------------------------- #
# Image limits
# --------------------------------------------------------------------------- #

#: (field, default) for the two image limits. Unlike the preview thresholds
#: these take no "all": an unbounded image is exactly what they exist to
#: prevent, so their validator is separate and its message says so.
IMAGE_LIMIT_FIELDS = [
    ("max_image_edge_px", DEFAULT_MAX_IMAGE_EDGE_PX),
    ("max_image_mb", DEFAULT_MAX_IMAGE_MB),
]


@pytest.mark.parametrize("field,default", IMAGE_LIMIT_FIELDS)
def test_image_limit_defaults_without_hardcoding_in_two_places(field, default):
    settings = Settings(SEAFILE_SERVER_URL=SERVER)
    assert getattr(settings, field) == default


@pytest.mark.parametrize("field,default", IMAGE_LIMIT_FIELDS)
def test_image_limit_accepts_a_custom_int(field, default):
    settings = Settings(SEAFILE_SERVER_URL=SERVER, **{field: "800"})
    assert getattr(settings, field) == 800


@pytest.mark.parametrize("field,default", IMAGE_LIMIT_FIELDS)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_image_limit_rejects_non_positive(field, default, value):
    with pytest.raises(ValidationError, match="positive integer"):
        Settings(SEAFILE_SERVER_URL=SERVER, **{field: value})


@pytest.mark.parametrize("value", ["all", "ALL"])
def test_image_limit_does_not_pretend_to_accept_all(value):
    # The preview thresholds take "all"; these do not, and must say so rather
    # than silently coercing it to None and removing the bound entirely.
    with pytest.raises(ValidationError):
        Settings(SEAFILE_SERVER_URL=SERVER, max_image_edge_px=value)


def test_image_reads_defaults_on_and_can_be_switched_off():
    assert Settings(SEAFILE_SERVER_URL=SERVER).image_reads is True
    assert Settings(SEAFILE_SERVER_URL=SERVER, image_reads="false").image_reads is False


# --------------------------------------------------------------------------- #
# Deployment plumbing
# --------------------------------------------------------------------------- #

#: Settings docker-compose deliberately does not forward. server_url has its own
#: required entry; api_token is stdio-only and must never be baked into a shared
#: deployment; the http_* triple is fixed by the compose network and Caddy in
#: front of it, so exposing them would only let an operator break the routing.
_NOT_FORWARDED = {"server_url", "api_token", "http_host", "http_port", "http_path"}


def test_every_setting_is_reachable_in_the_deployment():
    """docker-compose lists env vars one by one, so a new Setting is invisible
    in the container until it is added there.

    This failed silently for max_file_read_kb, mutation_rate_limit and
    request_timeout_s: documented as tunable, and setting them in .env did
    nothing at all.
    """
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    missing = [
        f"SEAFILE_MCP_{name.upper()}"
        for name in sorted(set(Settings.model_fields) - _NOT_FORWARDED)
        if f"SEAFILE_MCP_{name.upper()}:" not in compose
    ]
    assert not missing, f"not forwarded by docker-compose.yml: {missing}"


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #

OCR = {"ocr_url": "https://gpu.example/v1", "ocr_model": "lightonai/LightOnOCR-2-1B"}


def test_ocr_is_off_by_default():
    settings = Settings(SEAFILE_SERVER_URL=SERVER)
    assert settings.ocr_enabled is False
    assert settings.ocr_url is None


def test_ocr_is_on_once_url_and_model_are_set():
    settings = Settings(SEAFILE_SERVER_URL=SERVER, **OCR)
    assert settings.ocr_enabled is True
    assert settings.ocr_model == "lightonai/LightOnOCR-2-1B"


def test_ocr_url_loses_its_trailing_slash():
    settings = Settings(
        SEAFILE_SERVER_URL=SERVER, **{**OCR, "ocr_url": "http://gpu:8010/v1/"}
    )
    assert settings.ocr_url == "http://gpu:8010/v1"


def test_ocr_url_must_be_http():
    with pytest.raises(ValidationError, match="http"):
        Settings(SEAFILE_SERVER_URL=SERVER, **{**OCR, "ocr_url": "gpu:8010/v1"})


@pytest.mark.parametrize("missing", ["ocr_url", "ocr_model"])
def test_half_configured_ocr_refuses_to_start(missing):
    """Setting one without the other is a mistake, and quietly running with OCR
    off would leave the admin believing it is on."""
    given = {k: v for k, v in OCR.items() if k != missing}
    with pytest.raises(ValidationError, match=f"SEAFILE_MCP_{missing.upper()}"):
        Settings(SEAFILE_SERVER_URL=SERVER, **given)


def test_blank_ocr_values_mean_unset():
    """docker-compose forwards every variable as ${VAR:-}, so an operator who
    never mentions OCR still hands the server empty strings."""
    settings = Settings(
        SEAFILE_SERVER_URL=SERVER,
        ocr_url="",
        ocr_model=" ",
        ocr_api_key="",
        ocr_extra_body="",
    )
    assert settings.ocr_enabled is False
    assert settings.ocr_api_key is None
    assert settings.ocr_extra_body == {}


def test_ocr_api_key_never_appears_in_repr():
    settings = Settings(SEAFILE_SERVER_URL=SERVER, ocr_api_key="sk-very-secret", **OCR)
    assert "sk-very-secret" not in repr(settings)
    assert settings.ocr_api_key.get_secret_value() == "sk-very-secret"


def test_ocr_extra_body_is_parsed_from_json():
    settings = Settings(
        SEAFILE_SERVER_URL=SERVER,
        ocr_extra_body='{"chat_template_kwargs": {"enable_thinking": false}}',
        **OCR,
    )
    assert settings.ocr_extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("value", ["{not json", "[1, 2]", '"text"'])
def test_ocr_extra_body_must_be_a_json_object(value):
    with pytest.raises(ValidationError, match="JSON object"):
        Settings(SEAFILE_SERVER_URL=SERVER, ocr_extra_body=value, **OCR)


@pytest.mark.parametrize(
    "key,setting",
    [
        ("model", "SEAFILE_MCP_OCR_MODEL"),
        ("messages", "messages"),
        ("stream", "stream"),
        ("max_tokens", "SEAFILE_MCP_OCR_MAX_TOKENS"),
        ("temperature", "SEAFILE_MCP_OCR_TEMPERATURE"),
    ],
)
def test_ocr_extra_body_cannot_override_what_the_server_owns(key, setting):
    with pytest.raises(ValidationError, match=setting):
        Settings(SEAFILE_SERVER_URL=SERVER, ocr_extra_body=f'{{"{key}": 1}}', **OCR)


@pytest.mark.parametrize(
    "field", ["ocr_max_pages", "ocr_concurrency", "ocr_max_tokens", "ocr_max_edge_px"]
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_ocr_limits_reject_non_positive(field, value):
    with pytest.raises(ValidationError, match="positive integer"):
        Settings(SEAFILE_SERVER_URL=SERVER, **{field: value})


def test_ocr_max_edge_is_capped():
    """One rendered page per concurrent request is held in memory, so an
    unbounded edge is an unbounded allocation."""
    with pytest.raises(ValidationError, match="4096"):
        Settings(SEAFILE_SERVER_URL=SERVER, ocr_max_edge_px="10000")


def test_ocr_timeout_must_be_positive_and_temperature_not_negative():
    with pytest.raises(ValidationError):
        Settings(SEAFILE_SERVER_URL=SERVER, ocr_timeout_s="0")
    with pytest.raises(ValidationError):
        Settings(SEAFILE_SERVER_URL=SERVER, ocr_temperature="-0.1")


def test_ocr_defaults_suit_lightonocr():
    settings = Settings(SEAFILE_SERVER_URL=SERVER)
    assert settings.ocr_prompt == ""
    assert settings.ocr_max_edge_px == 1540
    assert settings.ocr_temperature == 0.1
    assert settings.ocr_max_pages == 10
    assert settings.ocr_concurrency == 4


def test_the_blank_page_check_defaults_on_and_can_be_switched_off():
    assert Settings(SEAFILE_SERVER_URL=SERVER).ocr_skip_blank_pages is True
    settings = Settings(SEAFILE_SERVER_URL=SERVER, ocr_skip_blank_pages="false")
    assert settings.ocr_skip_blank_pages is False


def test_tests_never_see_a_developers_env_file(tmp_path, monkeypatch):
    """Settings reads a .env from the working directory. A developer who
    configures their own deployment there — OCR on, a different page size, an
    API key — must not change what the test suite sees."""
    (tmp_path / ".env").write_text(
        "SEAFILE_MCP_OCR_MAX_EDGE_PX=2048\nSEAFILE_MCP_OCR_API_KEY=from-a-dotenv\n"
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings(SEAFILE_SERVER_URL=SERVER)

    assert settings.ocr_max_edge_px == 1540
    assert settings.ocr_api_key is None


# --------------------------------------------------------------------------- #
# Debug logging
# --------------------------------------------------------------------------- #


def test_debug_logging_is_off_by_default():
    assert Settings(SEAFILE_SERVER_URL=SERVER).debug == ()


def test_debug_names_areas_case_and_space_insensitively():
    settings = Settings(SEAFILE_SERVER_URL=SERVER, debug=" ocr, CLIENT ")
    assert settings.debug == ("ocr", "client")


def test_debug_all_turns_on_every_area():
    assert Settings(SEAFILE_SERVER_URL=SERVER, debug="all").debug == ("all",)


def test_an_unknown_debug_area_refuses_to_start():
    """A typo would otherwise mean "no logs" with no hint why."""
    with pytest.raises(ValidationError, match="auth, client, ocr, server"):
        Settings(SEAFILE_SERVER_URL=SERVER, debug="ocr,orc")



@pytest.mark.parametrize(
    "url", ["http://user:pass@gpu:8000/v1", "https://token@gpu.example/v1"]
)
def test_an_ocr_url_with_credentials_in_it_is_refused(url):
    """Anything in the URL ends up wherever URLs are logged. The key has its
    own setting, which is sent as a header and never logged."""
    with pytest.raises(ValidationError, match="SEAFILE_MCP_OCR_API_KEY"):
        Settings(SEAFILE_SERVER_URL=SERVER, ocr_url=url, ocr_model="m")
