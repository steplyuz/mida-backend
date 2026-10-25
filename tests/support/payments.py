"""Builds signed mock-provider webhooks, independently of the application's verifier."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

MOCK_SECRET = "m" * 32


def signed(body: dict[str, Any], secret: str = MOCK_SECRET) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Mock-Signature": f"sha256={digest}", "Content-Type": "application/json"}


def event(
    event_id: str, tx: str, *, kind: str = "payment.succeeded", amount: int = 110_000_000
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "type": kind,
        "provider_transaction_id": tx,
        "amount_tiyin": amount,
        "currency": "UZS",
    }
