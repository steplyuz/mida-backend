"""Payment flow over HTTP on a migrated SQLite file, with the mock provider and signed webhooks."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.payments.mock_provider import MockPaymentProvider
from app.main import create_app
from tests.support.payments import MOCK_SECRET, event, signed
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data

BUYER_TG, SELLER_TG, OUTSIDER_TG, ADMIN_TG = 8001, 8002, 8003, 8100
TOTAL_TIYIN = 110_000_000  # 1 000 000 UZS product + 10% fee


def _sqlite(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_url.removeprefix("sqlite+aiosqlite:///")))


@pytest.fixture
def provider() -> MockPaymentProvider:
    return MockPaymentProvider(webhook_secret=MOCK_SECRET, app_env="test")


@pytest.fixture
def api(db_url: str, provider: MockPaymentProvider) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
    )
    with TestClient(create_app(settings, payment_provider=provider)) as client:
        yield client


def _login(client: TestClient, tg: int) -> dict[str, Any]:
    body = client.post(
        "/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=tg)}
    ).json()
    return {"token": body["access_token"], "user_id": body["user"]["id"]}


def _auth(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def _deal(api: TestClient) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    buyer, seller = _login(api, BUYER_TG), _login(api, SELLER_TG)
    response = api.post(
        "/api/v1/deals",
        headers={**_auth(buyer), "Idempotency-Key": f"deal-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "Bike",
            "product_amount_tiyin": 100_000_000,
            "terms_text": "terms",
        },
    )
    assert response.status_code == 201, response.text
    return response.json(), buyer, seller


def _start(api: TestClient, deal: dict[str, Any], who: dict[str, Any], key: str | None = None) -> Any:
    return api.post(
        f"/api/v1/deals/{deal['id']}/payments",
        headers={**_auth(who), "Idempotency-Key": key or f"pay-{uuid.uuid4().hex}"},
    )


def _tx_of(db_url: str, deal_id: str) -> str:
    with _sqlite(db_url) as conn:
        row = conn.execute(
            "SELECT p.provider_transaction_id FROM payments p JOIN deals d ON d.id = p.deal_id "
            "WHERE d.public_id = ? ORDER BY p.id DESC LIMIT 1",
            (deal_id,),
        ).fetchone()
    assert row and row[0]
    return str(row[0])


def _send(
    api: TestClient,
    event_id: str,
    tx: str,
    *,
    kind: str = "payment.succeeded",
    amount: int = TOTAL_TIYIN,
) -> Any:
    body, headers = signed(event(event_id, tx, kind=kind, amount=amount))
    return api.post("/api/v1/payments/webhook/mock", content=body, headers=headers)


# ---------- starting ----------


def test_buyer_starts_payment_and_deal_waits_for_verification(
    api: TestClient, provider: MockPaymentProvider
) -> None:
    deal, buyer, _ = _deal(api)
    response = _start(api, deal, buyer)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "requires_verification"
    assert body["amount_tiyin"] == TOTAL_TIYIN
    assert body["checkout_url"].startswith("https://mock-pay.invalid/")
    assert "provider_transaction_id" not in body
    assert provider.create_calls == 1

    current = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()
    assert current["status"] == "payment_pending_verification"
    assert current["payment_status"] == "requires_verification"


def test_only_the_buyer_can_pay(api: TestClient) -> None:
    deal, _, seller = _deal(api)
    outsider = _login(api, OUTSIDER_TG)
    assert _start(api, deal, seller).status_code == 403
    assert _start(api, deal, outsider).status_code == 404


def test_missing_idempotency_key_is_rejected(api: TestClient) -> None:
    deal, buyer, _ = _deal(api)
    response = api.post(f"/api/v1/deals/{deal['id']}/payments", headers=_auth(buyer))
    assert response.status_code == 400


def test_same_key_replays_the_same_payment_without_a_second_provider_call(
    api: TestClient, provider: MockPaymentProvider
) -> None:
    deal, buyer, _ = _deal(api)
    key = f"replay-{uuid.uuid4().hex}"
    first = _start(api, deal, buyer, key)
    again = _start(api, deal, buyer, key)
    assert first.json()["id"] == again.json()["id"]
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert provider.create_calls == 1


def test_second_payment_while_one_is_in_flight_is_refused(api: TestClient) -> None:
    deal, buyer, _ = _deal(api)
    assert _start(api, deal, buyer).status_code == 201
    response = _start(api, deal, buyer)
    assert response.status_code == 409
    assert response.json()["type"] == "urn:mida:error:invalid-transition"


def test_no_provider_configured_fails_closed(db_url: str) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        log_level="WARNING",
        database_url=db_url,  # type: ignore[call-arg]
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
    )
    with TestClient(create_app(settings)) as client:
        deal, buyer, _ = _deal(client)
        response = _start(client, deal, buyer)
    assert response.status_code == 503


# ---------- provider facts ----------


def test_signed_success_confirms_the_payment_and_the_deal(api: TestClient, db_url: str) -> None:
    deal, buyer, seller = _deal(api)
    _start(api, deal, buyer)
    tx = _tx_of(db_url, deal["id"])
    assert _send(api, "evt-ok-1", tx).json() == {"status": "confirmed"}

    current = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()
    assert current["status"] == "payment_confirmed" and current["payment_status"] == "confirmed"
    status = api.get(f"/api/v1/deals/{deal['id']}/payment-status", headers=_auth(buyer)).json()
    assert status == {"deal_payment_status": "confirmed", "latest_payment_status": "confirmed"}


def test_seller_sees_payments_but_never_the_checkout_link(api: TestClient) -> None:
    deal, buyer, seller = _deal(api)
    _start(api, deal, buyer)
    as_seller = api.get(f"/api/v1/deals/{deal['id']}/payments", headers=_auth(seller)).json()["data"]
    as_buyer = api.get(f"/api/v1/deals/{deal['id']}/payments", headers=_auth(buyer)).json()["data"]
    assert as_seller[0]["checkout_url"] is None and as_seller[0]["status"] == "requires_verification"
    assert as_buyer[0]["checkout_url"].startswith("https://")


def test_duplicate_provider_event_is_applied_once(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _deal(api)
    _start(api, deal, buyer)
    tx = _tx_of(db_url, deal["id"])
    assert _send(api, "evt-dup", tx).json()["status"] == "confirmed"
    assert _send(api, "evt-dup", tx).json()["status"] == "duplicate"
    current = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()
    assert current["status"] == "payment_confirmed" and current["version"] == 4


def test_webhook_without_a_valid_signature_changes_nothing(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _deal(api)
    _start(api, deal, buyer)
    tx = _tx_of(db_url, deal["id"])
    body, headers = signed(event("evt-forged", tx), secret="x" * 32)
    response = api.post("/api/v1/payments/webhook/mock", content=body, headers=headers)
    assert response.status_code == 401
    with _sqlite(db_url) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payment_webhook_events").fetchone()[0] == 0
    assert (
        api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()["status"]
        == "payment_pending_verification"
    )


def test_amount_mismatch_is_held_for_review_not_confirmed(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _deal(api)
    _start(api, deal, buyer)
    tx = _tx_of(db_url, deal["id"])
    assert _send(api, "evt-cheap", tx, amount=1).json() == {"status": "flagged"}
    current = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()
    assert current["status"] == "payment_pending_verification"
    assert (
        api.get(f"/api/v1/deals/{deal['id']}/payment-status", headers=_auth(buyer)).json()[
            "latest_payment_status"
        ]
        == "requires_verification"
    )


def test_failed_payment_lets_the_buyer_try_again_with_a_new_key(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _deal(api)
    _start(api, deal, buyer)
    tx = _tx_of(db_url, deal["id"])
    assert _send(api, "evt-fail", tx, kind="payment.failed").json() == {"status": "failed"}
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()["status"] == "payment_failed"

    retry = _start(api, deal, buyer)
    assert retry.status_code == 201
    assert (
        api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()["status"]
        == "payment_pending_verification"
    )
    assert retry.json()["checkout_url"] != ""


def test_unknown_transaction_is_recorded_and_never_attached(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _deal(api)
    _start(api, deal, buyer)
    assert _send(api, "evt-stray", "mock_does_not_exist").json() == {"status": "unknown_transaction"}
    assert (
        api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()["status"]
        == "payment_pending_verification"
    )


def test_contradictory_event_after_confirmation_is_flagged(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _deal(api)
    _start(api, deal, buyer)
    tx = _tx_of(db_url, deal["id"])
    assert _send(api, "evt-c1", tx).json()["status"] == "confirmed"
    assert _send(api, "evt-c2", tx, kind="payment.failed").json()["status"] == "flagged"
    assert (
        api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()["status"] == "payment_confirmed"
    )


def test_unknown_provider_name_in_the_url_is_not_found(api: TestClient, db_url: str) -> None:
    body, headers = signed(event("evt-x", "mock_x"))
    assert api.post("/api/v1/payments/webhook/unknown", content=body, headers=headers).status_code == 404


# ---------- reconciliation ----------


def test_reconciliation_reports_disagreement_without_changing_state(
    api: TestClient, db_url: str, provider: MockPaymentProvider
) -> None:
    from app.integrations.payments.base import ProviderStatus

    deal, buyer, _ = _deal(api)
    _start(api, deal, buyer)
    tx = _tx_of(db_url, deal["id"])
    provider.set_status(tx, ProviderStatus.SUCCEEDED)  # the provider says paid; MIDA has not heard yet

    admin = _login(api, ADMIN_TG)
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (ADMIN_TG,))
    assert api.get("/api/v1/admin/payments/reconciliation", headers=_auth(buyer)).status_code == 403
    report = api.get("/api/v1/admin/payments/reconciliation", headers=_auth(admin)).json()
    assert report["mismatches"] == 1
    assert report["data"][0]["provider_status"] == "succeeded"
    assert (
        api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()["status"]
        == "payment_pending_verification"
    )
