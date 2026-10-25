"""The buyer sees where to transfer money: MIDA's own account, and the deal number as the reference."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.payments.manual_provider import ManualBankProvider
from app.main import create_app
from tests.integration.test_admin_ops import TOTAL, _deal, _h, _login  # noqa: F401
from tests.support.telegram import TEST_BOT_TOKEN

BUYER, SELLER = 9601, 9602
COMPANY_ACCOUNT = "20208000900000000001"


def _client(db_url: str, *, configured: bool) -> TestClient:
    kwargs: dict[str, Any] = {}
    if configured:
        kwargs = {
            "manual_payment_bank": "Hamkor bank",
            "manual_payment_account": COMPANY_ACCOUNT,
            "manual_payment_recipient": "MIDA MCHJ",
        }
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        manual_payments=True,
        **kwargs,
    )
    return TestClient(create_app(settings, payment_provider=ManualBankProvider()))


@pytest.fixture
def configured(db_url: str) -> Iterator[TestClient]:
    with _client(db_url, configured=True) as c:
        yield c


@pytest.fixture
def unconfigured(db_url: str) -> Iterator[TestClient]:
    with _client(db_url, configured=False) as c:
        yield c


def _start(api: TestClient, buyer: dict[str, Any], deal_id: str) -> dict[str, Any]:
    r = api.post(
        f"/api/v1/deals/{deal_id}/payments", headers={**_h(buyer), "Idempotency-Key": f"p-{uuid.uuid4().hex}"}
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def test_buyer_gets_company_account_and_deal_number_as_reference(configured: TestClient, db_url: str) -> None:
    buyer = _login(configured, BUYER)
    _login(configured, SELLER)
    deal = _deal(configured, buyer)
    body = _start(configured, buyer, deal["id"])
    ins = body["instructions"]
    assert ins["account_number"] == COMPANY_ACCOUNT
    assert ins["bank_name"] == "Hamkor bank"
    assert ins["recipient_name"] == "MIDA MCHJ"
    assert ins["reference"] == f"MIDA {deal['deal_number']}"
    assert ins["amount_tiyin"] == TOTAL and ins["currency"] == "UZS"


def test_no_instructions_when_the_account_is_not_configured(unconfigured: TestClient, db_url: str) -> None:
    buyer = _login(unconfigured, BUYER)
    _login(unconfigured, SELLER)
    deal = _deal(unconfigured, buyer)
    assert _start(unconfigured, buyer, deal["id"])["instructions"] is None


def test_instructions_are_not_repeated_in_other_payment_views(configured: TestClient, db_url: str) -> None:
    buyer = _login(configured, BUYER)
    _login(configured, SELLER)
    deal = _deal(configured, buyer)
    _start(configured, buyer, deal["id"])
    listing = configured.get(f"/api/v1/deals/{deal['id']}/payments", headers=_h(buyer)).json()
    assert all(p.get("instructions") is None for p in listing["data"])
