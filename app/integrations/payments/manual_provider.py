"""Manual bank transfer. No provider API and no webhook: a person checks the bank statement.

The transaction id is derived from the idempotency key, so a retried start maps to the same payment.
Nothing here moves money. The admin records the bank reference; a second admin approves it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from app.core.domain_errors import AuthenticationError, UnsupportedOperationError
from app.integrations.payments.base import PaymentIntent, ProviderStatus


class ManualBankProvider:
    name = "manual"

    async def create_payment(
        self, *, idempotency_key: str, amount_tiyin: int, currency: str, description: str
    ) -> PaymentIntent:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        return PaymentIntent(
            provider_transaction_id=f"manual_{digest}",
            checkout_url=None,
            status=ProviderStatus.PENDING,
        )

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> object:
        raise AuthenticationError("The manual provider does not accept webhooks.")

    async def get_payment_status(self, provider_transaction_id: str) -> ProviderStatus:
        return ProviderStatus.PENDING

    async def request_refund(self, *, provider_transaction_id: str, amount_tiyin: int) -> None:
        raise UnsupportedOperationError("Refunds are handled manually and are not recorded here.")
