"""Development and test provider. It never moves money and refuses to exist in production.

Signatures: X-Mock-Signature = "sha256=" + HMAC_SHA256(secret, raw_body). This mirrors the shape of
a real webhook check so the verification path is exercised, but it is NOT a real provider.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.domain_errors import (
    AuthenticationError,
    UnsupportedOperationError,
    ValidationDomainError,
)
from app.integrations.payments.base import PaymentIntent, ProviderEvent, ProviderStatus

EVENT_TYPES = frozenset({"payment.succeeded", "payment.failed"})
SIGNATURE_HEADER = "x-mock-signature"


class MockPaymentProvider:
    name = "mock"

    def __init__(self, *, webhook_secret: str, app_env: str) -> None:
        if app_env == "production":
            raise RuntimeError("The mock payment provider must never run in production.")
        if len(webhook_secret) < 32:
            raise ValueError("Mock webhook secret must be at least 32 characters.")
        self._secret = webhook_secret.encode("utf-8")
        self._by_key: dict[str, str] = {}
        self._status: dict[str, ProviderStatus] = {}
        self.create_calls = 0

    async def create_payment(
        self, *, idempotency_key: str, amount_tiyin: int, currency: str, description: str
    ) -> PaymentIntent:
        self.create_calls += 1
        # Same key, same transaction: the provider-side idempotency the contract requires.
        tx = self._by_key.get(idempotency_key)
        if tx is None:
            tx = f"mock_{uuid.uuid4().hex}"
            self._by_key[idempotency_key] = tx
            self._status[tx] = ProviderStatus.PENDING
        return PaymentIntent(
            provider_transaction_id=tx,
            checkout_url=f"https://mock-pay.invalid/checkout/{tx}",
            status=self._status[tx],
        )

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> ProviderEvent:
        # Header names are case-insensitive; normalise here rather than trusting the caller.
        normalised = {name.lower(): value for name, value in headers.items()}
        received = normalised.get(SIGNATURE_HEADER, "")
        expected = "sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
            raise AuthenticationError("Invalid payment webhook signature.")
        return _parse_event(body)

    async def get_payment_status(self, provider_transaction_id: str) -> ProviderStatus:
        return self._status.get(provider_transaction_id, ProviderStatus.PENDING)

    async def request_refund(self, *, provider_transaction_id: str, amount_tiyin: int) -> None:
        raise UnsupportedOperationError("Refunds are not implemented in the mock provider.")

    # Test and development helper: move a transaction to a status, as a real provider would.
    def set_status(self, provider_transaction_id: str, status: ProviderStatus) -> None:
        self._status[provider_transaction_id] = status


def _parse_event(body: bytes) -> ProviderEvent:
    try:
        data: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ValidationDomainError("Webhook body must be JSON.") from None
    if not isinstance(data, dict):
        raise ValidationDomainError("Webhook body must be an object.")

    def text(field: str, limit: int) -> str:
        value = data.get(field)
        if not isinstance(value, str) or not 1 <= len(value) <= limit:
            raise ValidationDomainError(f"Webhook field '{field}' is invalid.")
        return value

    amount = data.get("amount_tiyin")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValidationDomainError("Webhook field 'amount_tiyin' must be a positive integer.")
    event_type = text("type", 64)
    if event_type not in EVENT_TYPES:
        raise ValidationDomainError("Unsupported payment event type.")
    return ProviderEvent(
        event_id=text("event_id", 128),
        event_type=event_type,
        provider_transaction_id=text("provider_transaction_id", 128),
        amount_tiyin=amount,
        currency=text("currency", 3),
        occurred_at=datetime.now(UTC),
    )
