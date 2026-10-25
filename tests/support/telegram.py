"""Builds correctly signed Telegram initData in tests, implemented independently of the app code."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

TEST_BOT_TOKEN = "123456:TEST-BOT-TOKEN-FOR-UNIT-TESTS"


def make_fields(
    *,
    telegram_user_id: int = 1001,
    first_name: str = "Ali",
    username: str | None = "ali_test",
    auth_date: int | None = None,
    photo_url: str | None = None,
) -> dict[str, str]:
    user: dict[str, object] = {"id": telegram_user_id, "first_name": first_name}
    if username is not None:
        user["username"] = username
    if photo_url is not None:
        user["photo_url"] = photo_url
    return {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAHtest",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }


def sign(fields: dict[str, str], bot_token: str = TEST_BOT_TOKEN) -> str:
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def valid_init_data(**overrides: object) -> str:
    fields = make_fields(**overrides)  # type: ignore[arg-type]
    return sign(fields)
