"""Guard rails.

The controls that actually constrain what an assistant can do here are, in order
of strength:

1. **The token itself.** A read-only library token cannot write, and no library
   token can delete, move or copy — Seafile enforces that upstream, not us.
2. **Deployment mode.** Tools outside :data:`Settings.mode` are never registered,
   so the model cannot call what it cannot see.
3. **Blast-radius caps.** Item counts, root protection, path normalisation, rate
   limits.

A model-supplied ``confirm``/``operation_id`` is explicitly *not* in that list.
It guards against an accidental one-shot delete, not against instructions
injected into file content — anything the model can set, injected text can set
too. It is a usability control and is documented as one.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass

from .config import get_settings
from .models import Credentials, SafetyError

logger = logging.getLogger("seafile_mcp.audit")

# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_TOKEN_PATTERNS = (
    # "Authorization: Token <x>" / "Bearer <x>", however it got into a message
    re.compile(r"\b(?:Token|Bearer)\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    # bare Seafile-style tokens (40 hex chars) and other long opaque blobs
    re.compile(r"\b[0-9a-f]{40}\b"),
)

_REDACTED = "<redacted>"

#: Used to render tracebacks inside the filter, before a handler can emit them raw.
_EXC_FORMATTER = logging.Formatter()


def redact(text: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrub credentials from every log record, including tracebacks.

    Applied to the root logger so a stray ``logger.debug(response.text)`` or an
    exception carrying a URL cannot leak a live token into ``docker logs``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        # A traceback is not formatted until the handler runs, so record.exc_text
        # is still empty here. Format it now and cache the redacted version:
        # logging.Formatter reuses exc_text when present, so the raw traceback
        # never gets rendered.
        if record.exc_info and not record.exc_text:
            record.exc_text = _EXC_FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_redaction() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

_mutations: dict[str, list[float]] = {}
_WINDOW_S = 60.0


def check_mutation_rate(creds: Credentials) -> None:
    """Cap mutations per token per minute, bounding a runaway or injected loop."""
    limit = get_settings().mutation_rate_limit
    if limit <= 0:
        return
    key = _key(creds)
    now = time.monotonic()
    recent = [t for t in _mutations.get(key, []) if now - t < _WINDOW_S]
    if len(recent) >= limit:
        raise SafetyError(
            f"Rate limit reached: more than {limit} write operations in a minute. "
            "Slow down, or check whether something is looping."
        )
    recent.append(now)
    _mutations[key] = recent


def _key(creds: Credentials) -> str:
    import hashlib

    return hashlib.sha256(creds.token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Two-step delete confirmation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PendingDelete:
    owner: str
    repo_id: str | None
    path: str
    expires_at: float


_pending: dict[str, PendingDelete] = {}
_PENDING_TTL_S = 300.0


def stage_delete(creds: Credentials, repo_id: str | None, path: str) -> str:
    _sweep()
    operation_id = secrets.token_urlsafe(12)
    _pending[operation_id] = PendingDelete(
        owner=_key(creds),
        repo_id=repo_id,
        path=path,
        expires_at=time.monotonic() + _PENDING_TTL_S,
    )
    return operation_id


def consume_delete(
    creds: Credentials, operation_id: str, repo_id: str | None, path: str
) -> None:
    """Validate and burn a staged delete.

    Bound to the requesting token so one caller can never redeem another's
    pending operation, and single-use so an id cannot be replayed.
    """
    _sweep()
    pending = _pending.pop(operation_id, None)
    if pending is None:
        raise SafetyError(
            "Unknown or expired operation_id. Call seafile_delete without one "
            "first to see what would be removed, then confirm."
        )
    if pending.owner != _key(creds):
        raise SafetyError("This operation_id does not belong to the current session.")
    if pending.expires_at < time.monotonic():
        raise SafetyError("This confirmation expired. Request the deletion again.")
    if pending.repo_id != repo_id or pending.path != path:
        raise SafetyError(
            "The confirmed operation does not match this request "
            f"(staged {pending.path!r}, asked to delete {path!r})."
        )


def _sweep() -> None:
    now = time.monotonic()
    for key in [k for k, v in _pending.items() if v.expires_at < now]:
        _pending.pop(key, None)


def reset_state() -> None:
    """Test hook."""
    _pending.clear()
    _mutations.clear()


# --------------------------------------------------------------------------- #
# Elicitation
# --------------------------------------------------------------------------- #


async def try_confirm(ctx: object, message: str) -> bool | None:
    """Ask the human directly, if this client can.

    Returns True/False when the user actually answered, or None when the client
    does not support elicitation (LibreChat, today) — in which case the caller
    falls back to the two-step flow.
    """
    elicit = getattr(ctx, "elicit", None)
    if elicit is None:
        return None
    try:
        result = await elicit(message, response_type=None)
    except Exception:
        return None
    action = getattr(result, "action", None)
    if action == "accept":
        return True
    if action in ("decline", "cancel"):
        return False
    return None


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def audit(
    tool: str,
    creds: Credentials,
    *,
    repo_id: str | None = None,
    path: str | None = None,
    outcome: str = "ok",
) -> None:
    """Record a mutating call. Never records the token itself."""
    logger.info(
        "audit tool=%s mode=%s token=%s repo=%s path=%s outcome=%s",
        tool,
        creds.mode.value,
        _key(creds)[:12],
        repo_id or "-",
        path or "-",
        outcome,
    )
