"""Telegram Mini App initData verification.

Algorithm (official Telegram Web Apps docs):
  secret_key     = HMAC_SHA256(key="WebAppData", msg=bot_token)
  check_string   = "\n".join(f"{k}={v}" for k, v in sorted(fields) if k != "hash")
  expected_hash  = HMAC_SHA256(key=secret_key, msg=check_string).hexdigest()

initDataUnsafe on the frontend is never trusted; only this server-side check is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl

from app.core.domain_errors import AuthenticationError

MAX_INIT_DATA_LENGTH = 4096
MAX_FIELDS = 32
MAX_NAME_LENGTH = 128
MAX_PHOTO_URL_LENGTH = 512
FUTURE_SKEW_SECONDS = 60
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,64}")
_SECRET_KEY_SALT = b"WebAppData"


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    telegram_user_id: int
    first_name: str
    last_name: str | None
    username: str | None
    auth_date: int
    photo_url: str | None = None


def _invalid() -> AuthenticationError:
    return AuthenticationError("Invalid Telegram login data.")


def _parse_fields(init_data: str) -> dict[str, str]:
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=MAX_FIELDS)
    except ValueError:
        raise _invalid() from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:  # duplicate keys are ambiguous; reject instead of picking one
            raise _invalid()
        fields[key] = value
    return fields


def _clean_name(value: object, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise _invalid()
        return None
    if not isinstance(value, str):
        raise _invalid()
    cleaned = " ".join(value.split())[:MAX_NAME_LENGTH]
    if not cleaned:
        if required:
            raise _invalid()
        return None
    return cleaned


def _clean_photo_url(value: object) -> str | None:
    """Telegram avatar link. Only plain https URLs are kept; anything else is dropped."""
    if not isinstance(value, str) or not value or len(value) > MAX_PHOTO_URL_LENGTH:
        return None
    if not value.startswith("https://") or any(ch.isspace() for ch in value):
        return None
    return value


def verify_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int,
    now: datetime,
) -> TelegramIdentity:
    if not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        raise _invalid()

    fields = _parse_fields(init_data)
    received_hash = fields.pop("hash", None)
    if received_hash is None or _HASH_PATTERN.fullmatch(received_hash) is None:
        raise _invalid()

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(_SECRET_KEY_SALT, bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    # Signature is checked before any field is parsed, so untrusted content is never interpreted.
    if not hmac.compare_digest(expected_hash, received_hash):
        raise AuthenticationError("Telegram login data signature is invalid.")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError):
        raise _invalid() from None
    now_ts = int(now.timestamp())
    if auth_date > now_ts + FUTURE_SKEW_SECONDS:
        raise _invalid()
    if now_ts - auth_date > max_age_seconds:
        raise AuthenticationError("Telegram login data has expired. Reopen the app and try again.")

    raw_user = fields.get("user")
    if raw_user is None:
        raise _invalid()
    try:
        user_data = json.loads(raw_user)
    except json.JSONDecodeError:
        raise _invalid() from None
    if not isinstance(user_data, dict):
        raise _invalid()

    user_id = user_data.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise _invalid()

    username = user_data.get("username")
    if not (isinstance(username, str) and _USERNAME_PATTERN.fullmatch(username)):
        username = None  # optional and informational; never a basis for authorization

    return TelegramIdentity(
        telegram_user_id=user_id,
        first_name=_clean_name(user_data.get("first_name"), required=True) or "",
        last_name=_clean_name(user_data.get("last_name"), required=False),
        username=username,
        auth_date=auth_date,
        photo_url=_clean_photo_url(user_data.get("photo_url")),
    )
