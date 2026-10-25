"""Opaque cursors: the payload carries the sort, so a cursor cannot be reused with another."""

from __future__ import annotations

import base64
import json
from typing import Any

from app.core.domain_errors import ValidationDomainError

MAX_CURSOR_LENGTH = 512


def _invalid() -> ValidationDomainError:
    return ValidationDomainError("Invalid pagination cursor.")


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> dict[str, Any]:
    if not token or len(token) > MAX_CURSOR_LENGTH:
        raise _invalid()
    padded = token + "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise _invalid() from None
    if not isinstance(payload, dict):
        raise _invalid()
    return payload
