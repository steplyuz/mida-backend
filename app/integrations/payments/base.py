"""Payment provider contract. The application depends only on this module, never on a vendor SDK.

Ground rules every implementation must follow:
- create_payment is idempotent on idempotency_key: same key, same provider transaction.
- verify_webhook authenticates the raw body BEFORE any field is trusted.
- Nothing here moves money on its own. A provider reports facts; MIDA decides what they mean.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ProviderStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    provider_transaction_id: str
    checkout_url: str | None
    status: ProviderStatus


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    event_id: str
    event_type: str  # "payment.succeeded" | "payment.failed"
    provider_transaction_id: str
    amount_tiyin: int
    currency: str
    occurred_at: datetime


class PaymentProvider(Protocol):
    name: str

    async def create_payment(
        self, *, idempotency_key: str, amount_tiyin: int, currency: str, description: str
    ) -> PaymentIntent: ...

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> ProviderEvent: ...

    async def get_payment_status(self, provider_transaction_id: str) -> ProviderStatus: ...

    async def request_refund(self, *, provider_transaction_id: str, amount_tiyin: int) -> None: ...
