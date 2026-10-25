from typing import Any

import pytest

from app.core.domain_errors import (
    AuthenticationError,
    UnsupportedOperationError,
    ValidationDomainError,
)
from app.integrations.payments.base import ProviderStatus
from app.integrations.payments.mock_provider import MockPaymentProvider
from app.models.enums import PaymentStatus
from app.services.payment_service import payment_move_allowed
from tests.support.payments import MOCK_SECRET, event, signed


def _provider() -> MockPaymentProvider:
    return MockPaymentProvider(webhook_secret=MOCK_SECRET, app_env="test")


def test_mock_provider_refuses_to_exist_in_production() -> None:
    with pytest.raises(RuntimeError):
        MockPaymentProvider(webhook_secret=MOCK_SECRET, app_env="production")


def test_mock_provider_requires_a_strong_secret() -> None:
    with pytest.raises(ValueError):
        MockPaymentProvider(webhook_secret="short", app_env="test")


def test_valid_signature_is_accepted() -> None:
    body, headers = signed(event("evt-1", "mock_tx"))
    parsed = _provider().verify_webhook(headers=headers, body=body)
    assert parsed.event_id == "evt-1" and parsed.amount_tiyin == 110_000_000


def test_tampered_body_fails_the_signature() -> None:
    body, headers = signed(event("evt-1", "mock_tx"))
    with pytest.raises(AuthenticationError):
        _provider().verify_webhook(headers=headers, body=body.replace(b"110000000", b"1"))


def test_missing_or_wrong_signature_is_rejected() -> None:
    body, _ = signed(event("evt-1", "mock_tx"))
    with pytest.raises(AuthenticationError):
        _provider().verify_webhook(headers={}, body=body)
    other_body, other_headers = signed(event("evt-1", "mock_tx"), secret="z" * 32)
    with pytest.raises(AuthenticationError):
        _provider().verify_webhook(headers=other_headers, body=other_body)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: {**e, "amount_tiyin": True},
        lambda e: {**e, "amount_tiyin": -5},
        lambda e: {**e, "amount_tiyin": "110000000"},
        lambda e: {**e, "type": "payment.refunded"},
        lambda e: {k: v for k, v in e.items() if k != "event_id"},
        lambda e: {**e, "event_id": "x" * 200},
    ],
)
def test_malformed_signed_events_are_rejected(mutate: Any) -> None:
    body, headers = signed(mutate(event("evt-2", "mock_tx")))
    with pytest.raises(ValidationDomainError):
        _provider().verify_webhook(headers=headers, body=body)


async def test_create_is_idempotent_on_the_key() -> None:
    provider = _provider()
    first = await provider.create_payment(
        idempotency_key="k-1", amount_tiyin=1, currency="UZS", description="d"
    )
    again = await provider.create_payment(
        idempotency_key="k-1", amount_tiyin=1, currency="UZS", description="d"
    )
    other = await provider.create_payment(
        idempotency_key="k-2", amount_tiyin=1, currency="UZS", description="d"
    )
    assert first.provider_transaction_id == again.provider_transaction_id
    assert other.provider_transaction_id != first.provider_transaction_id


async def test_status_reflects_what_the_provider_reports() -> None:
    provider = _provider()
    intent = await provider.create_payment(
        idempotency_key="k-3", amount_tiyin=1, currency="UZS", description="d"
    )
    assert await provider.get_payment_status(intent.provider_transaction_id) is ProviderStatus.PENDING
    provider.set_status(intent.provider_transaction_id, ProviderStatus.SUCCEEDED)
    assert await provider.get_payment_status(intent.provider_transaction_id) is ProviderStatus.SUCCEEDED


async def test_refunds_are_explicitly_unsupported() -> None:
    with pytest.raises(UnsupportedOperationError):
        await _provider().request_refund(provider_transaction_id="x", amount_tiyin=1)


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (PaymentStatus.PENDING, PaymentStatus.REQUIRES_VERIFICATION, True),
        (PaymentStatus.REQUIRES_VERIFICATION, PaymentStatus.CONFIRMED, True),
        (PaymentStatus.REQUIRES_VERIFICATION, PaymentStatus.FAILED, True),
        (PaymentStatus.CONFIRMED, PaymentStatus.FAILED, False),
        (PaymentStatus.FAILED, PaymentStatus.CONFIRMED, False),
        (PaymentStatus.CONFIRMED, PaymentStatus.REQUIRES_VERIFICATION, False),
    ],
)
def test_payment_status_moves_are_explicit(
    current: PaymentStatus, target: PaymentStatus, allowed: bool
) -> None:
    assert payment_move_allowed(current, target) is allowed
