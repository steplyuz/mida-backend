from __future__ import annotations

from app.core.config import Settings
from app.integrations.payments.base import PaymentProvider
from app.integrations.payments.manual_provider import ManualBankProvider
from app.integrations.payments.mock_provider import MockPaymentProvider


def build_payment_provider(settings: Settings) -> PaymentProvider | None:
    """Manual bank transfers take precedence. The mock is for development and tests only."""
    if settings.manual_payments:
        return ManualBankProvider()  # type: ignore[return-value]
    if not settings.mock_payment_provider:
        return None
    if settings.app_env == "production":
        raise RuntimeError("MOCK_PAYMENT_PROVIDER cannot be enabled in production.")
    if settings.mock_payment_webhook_secret is None:
        raise RuntimeError("MOCK_PAYMENT_WEBHOOK_SECRET is required when the mock provider is enabled.")
    return MockPaymentProvider(
        webhook_secret=settings.mock_payment_webhook_secret.get_secret_value(),
        app_env=settings.app_env,
    )
