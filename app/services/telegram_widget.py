"""Telegram Login Widget verification, for the web admin panel.

This is NOT the Mini App initData scheme. The widget signs with a key derived from the bot token:
  secret = SHA256(bot_token)
  check  = "\n".join(f"{k}={v}" for sorted fields, excluding "hash")
  hash   = HMAC_SHA256(secret, check).hexdigest()
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime

from app.core.domain_errors import AuthenticationError

FUTURE_SKEW_SECONDS = 60
_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class WidgetIdentity:
    telegram_user_id: int
    first_name: str
    username: str | None
    auth_date: int


def _invalid() -> AuthenticationError:
    return AuthenticationError("Invalid Telegram login data.")


def verify_widget_login(
    data: dict[str, object], *, bot_token: str, max_age_seconds: int, now: datetime
) -> WidgetIdentity:
    received = data.get("hash")
    if not isinstance(received, str) or _HASH.fullmatch(received) is None:
        raise _invalid()
    fields = {k: str(v) for k, v in data.items() if k != "hash" and v is not None}
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthenticationError("Telegram login data signature is invalid.")

    try:
        auth_date = int(fields["auth_date"])
        user_id = int(fields["id"])
    except (KeyError, ValueError):
        raise _invalid() from None
    if user_id <= 0:
        raise _invalid()
    now_ts = int(now.timestamp())
    if auth_date > now_ts + FUTURE_SKEW_SECONDS:
        raise _invalid()
    if now_ts - auth_date > max_age_seconds:
        raise AuthenticationError("Telegram login data has expired. Log in again.")
    username = fields.get("username")
    return WidgetIdentity(
        telegram_user_id=user_id,
        first_name=(fields.get("first_name") or "Admin")[:128],
        username=username[:64] if username else None,
        auth_date=auth_date,
    )
