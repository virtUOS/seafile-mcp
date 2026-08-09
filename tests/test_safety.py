"""Guard rails: redaction, rate limiting, and the two-step delete confirmation."""

from __future__ import annotations

import logging

import pytest

from seafile_mcp import safety
from seafile_mcp.config import Settings
from seafile_mcp.models import Credentials, SafetyError, TokenMode

SENTINEL = "0123456789abcdef0123456789abcdef01234567"  # 40 hex, Seafile-shaped
ALICE = Credentials(token=SENTINEL, mode=TokenMode.account)
BOB = Credentials(token="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", mode=TokenMode.account)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Token {SENTINEL}",
        f"Bearer {SENTINEL}",
        f"bare {SENTINEL} in a sentence",
        f"https://seafile.test/f/{SENTINEL}/",
    ],
)
def test_redact_removes_tokens(text):
    assert SENTINEL not in safety.redact(text)


def test_sentinel_token_never_reaches_the_log(caplog):
    """The plan's explicit requirement: a live token must not land in docker logs."""
    logger = logging.getLogger("seafile_mcp.test")
    logger.addFilter(safety.RedactingFilter())

    with caplog.at_level(logging.DEBUG):
        logger.info("calling upstream with Token %s", SENTINEL)
        logger.warning("url=https://seafile.test/api2/x?auth=%s", SENTINEL)

    assert SENTINEL not in caplog.text


def test_sentinel_is_redacted_from_tracebacks(caplog):
    logger = logging.getLogger("seafile_mcp.test.exc")
    logger.addFilter(safety.RedactingFilter())

    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError(f"upstream rejected Token {SENTINEL}")
        except RuntimeError:
            logger.exception("request failed")

    assert SENTINEL not in caplog.text


def test_audit_records_the_operation_but_not_the_token(caplog):
    with caplog.at_level(logging.INFO, logger="seafile_mcp.audit"):
        safety.audit("seafile_delete", ALICE, repo_id="r1", path="/x.txt")

    assert "seafile_delete" in caplog.text
    assert "/x.txt" in caplog.text
    assert SENTINEL not in caplog.text


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_mutation_rate_limit_trips(monkeypatch):
    monkeypatch.setattr(
        safety, "get_settings", lambda: Settings(SEAFILE_SERVER_URL="https://x.test", mutation_rate_limit=3)
    )
    for _ in range(3):
        safety.check_mutation_rate(ALICE)
    with pytest.raises(SafetyError, match="Rate limit"):
        safety.check_mutation_rate(ALICE)


def test_rate_limit_is_per_token(monkeypatch):
    monkeypatch.setattr(
        safety, "get_settings", lambda: Settings(SEAFILE_SERVER_URL="https://x.test", mutation_rate_limit=2)
    )
    safety.check_mutation_rate(ALICE)
    safety.check_mutation_rate(ALICE)
    safety.check_mutation_rate(BOB)  # unaffected by Alice's usage
    with pytest.raises(SafetyError):
        safety.check_mutation_rate(ALICE)


# --------------------------------------------------------------------------- #
# Two-step delete
# --------------------------------------------------------------------------- #


def test_staged_delete_round_trip():
    op = safety.stage_delete(ALICE, "r1", "/x.txt")
    safety.consume_delete(ALICE, op, "r1", "/x.txt")


def test_operation_id_cannot_be_replayed():
    op = safety.stage_delete(ALICE, "r1", "/x.txt")
    safety.consume_delete(ALICE, op, "r1", "/x.txt")
    with pytest.raises(SafetyError, match="Unknown or expired"):
        safety.consume_delete(ALICE, op, "r1", "/x.txt")


def test_operation_id_is_bound_to_its_owner():
    """Bob must not be able to redeem an operation staged by Alice."""
    op = safety.stage_delete(ALICE, "r1", "/x.txt")
    with pytest.raises(SafetyError, match="does not belong"):
        safety.consume_delete(BOB, op, "r1", "/x.txt")


def test_operation_id_is_bound_to_its_path():
    """A confirmation for one path must not authorise deleting another."""
    op = safety.stage_delete(ALICE, "r1", "/harmless.txt")
    with pytest.raises(SafetyError, match="does not match"):
        safety.consume_delete(ALICE, op, "r1", "/important.txt")


def test_unknown_operation_id_is_rejected():
    with pytest.raises(SafetyError, match="Unknown or expired"):
        safety.consume_delete(ALICE, "made-up", "r1", "/x.txt")


def test_expired_operation_id_is_rejected(monkeypatch):
    op = safety.stage_delete(ALICE, "r1", "/x.txt")
    real = safety.time.monotonic
    monkeypatch.setattr(safety.time, "monotonic", lambda: real() + 10_000)
    with pytest.raises(SafetyError):
        safety.consume_delete(ALICE, op, "r1", "/x.txt")


# --------------------------------------------------------------------------- #
# Elicitation fallback
# --------------------------------------------------------------------------- #


async def test_try_confirm_returns_none_when_client_cannot_elicit():
    """LibreChat today: no elicitation, so we must fall back rather than fail."""

    class NoElicit:
        pass

    assert await safety.try_confirm(NoElicit(), "delete?") is None


async def test_try_confirm_returns_none_when_elicitation_raises():
    class Raises:
        async def elicit(self, *a, **kw):
            raise RuntimeError("not supported")

    assert await safety.try_confirm(Raises(), "delete?") is None


@pytest.mark.parametrize(
    "action,expected", [("accept", True), ("decline", False), ("cancel", False)]
)
async def test_try_confirm_reports_the_users_answer(action, expected):
    class Result:
        def __init__(self, a):
            self.action = a

    class Client:
        async def elicit(self, *a, **kw):
            return Result(action)

    assert await safety.try_confirm(Client(), "delete?") is expected
