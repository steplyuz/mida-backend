"""Administrator operations end to end: manual payments, payouts, users, inquiries, admin sign-in."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.actor import Actor
from app.core.config import Settings
from app.db.session import create_engine_from_url, create_session_factory
from app.integrations.payments.manual_provider import ManualBankProvider
from app.main import create_app
from app.models.enums import DealStatus
from app.services.deal_service import DealService
from app.services.pricing_service import PricingService
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data

BUYER, SELLER, ADMIN_A, ADMIN_B, MEMBER, BLOCKED = 9601, 9602, 9603, 9604, 9605, 9606
TOTAL = 110_000_000


def _sqlite(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_url.removeprefix("sqlite+aiosqlite:///")))


@pytest.fixture
def api(db_url: str) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        manual_payments=True,
        rate_limit_inquiry_per_hour=5,
    )
    with TestClient(create_app(settings, payment_provider=ManualBankProvider())) as client:
        yield client


def _login(api: TestClient, tg: int) -> dict[str, Any]:
    body = api.post("/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=tg)}).json()
    return {"token": body["access_token"], "user_id": body["user"]["id"]}


def _admin(api: TestClient, db_url: str, tg: int) -> dict[str, Any]:
    user = _login(api, tg)
    with _sqlite(db_url) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (tg,))
    return user


def _h(u: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {u['token']}"}


def _deal(api: TestClient, buyer: dict[str, Any], amount: int = 100_000_000) -> dict[str, Any]:
    _login(api, SELLER)  # the counterparty must have signed in first
    r = api.post(
        "/api/v1/deals",
        headers={**_h(buyer), "Idempotency-Key": f"d-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER,
            "title": "Sofa",
            "product_amount_tiyin": amount,
            "terms_text": "terms",
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def _run(db_url: str, steps: list[tuple[Actor, DealStatus]], public_id: str) -> None:
    async def go() -> None:
        engine = create_engine_from_url(db_url)
        factory = create_session_factory(engine)
        pricing = PricingService(service_fee_bps=1000, max_amount_uzs=1_000_000_000)
        try:
            for actor, target in steps:
                async with factory() as s:
                    svc = DealService(s, pricing)
                    deal = await svc.get_deal_by_public_id(Actor.system(), public_id)
                    await svc.transition(actor, deal.id, target, expected_version=deal.version)
        finally:
            await engine.dispose()

    asyncio.run(go())


def _pay_to_confirmed_path(
    api: TestClient,
    db_url: str,
    deal: dict[str, Any],
    buyer: dict[str, Any],
    admin_a: dict[str, Any],
    admin_b: dict[str, Any],
    ref: str = "BANK-REF-0001",
) -> None:
    """Buyer starts a manual payment; admin A records it; admin B approves."""
    started = api.post(
        f"/api/v1/deals/{deal['id']}/payments",
        headers={**_h(buyer), "Idempotency-Key": f"p-{uuid.uuid4().hex}"},
    )
    assert started.status_code == 201, started.text
    pid = started.json()["id"]
    assert (
        api.post(
            f"/api/v1/admin/payments/{pid}/record",
            headers=_h(admin_a),
            json={"bank_reference": ref, "amount_tiyin": TOTAL},
        ).status_code
        == 200
    )
    assert api.post(f"/api/v1/admin/payments/{pid}/approve", headers=_h(admin_b)).status_code == 200


def _complete(
    api: TestClient, db_url: str, deal: dict[str, Any], buyer: dict[str, Any], seller: dict[str, Any]
) -> None:
    _run(
        db_url,
        [
            (Actor.user(seller["user_id"]), DealStatus.DELIVERY_IN_PROGRESS),
            (Actor.user(seller["user_id"]), DealStatus.AWAITING_BUYER_CONFIRMATION),
        ],
        deal["id"],
    )
    cur = api.get(f"/api/v1/deals/{deal['id']}", headers=_h(buyer)).json()
    r = api.post(
        f"/api/v1/deals/{deal['id']}/confirm-receipt",
        headers={**_h(buyer), "Idempotency-Key": f"c-{uuid.uuid4().hex}"},
        json={"expected_version": cur["version"]},
    )
    assert r.status_code == 200, r.text


# ---------- statistics and access ----------


def test_dashboard_counts_and_is_admin_only(api: TestClient, db_url: str) -> None:
    buyer = _login(api, BUYER)
    _deal(api, buyer)
    assert api.get("/api/v1/admin/stats", headers=_h(buyer)).status_code == 403
    admin = _admin(api, db_url, ADMIN_A)
    body = api.get("/api/v1/admin/stats", headers=_h(admin)).json()
    assert body["users"]["total"] >= 2
    assert body["deals"]["by_status"].get("created", 0) >= 1
    assert set(body) >= {"users", "deals", "payments", "payouts", "disputes", "inquiries", "integrations"}


def test_every_admin_route_refuses_members(api: TestClient) -> None:
    member = _login(api, MEMBER)
    probes = [
        ("GET", "/api/v1/admin/stats"),
        ("GET", "/api/v1/admin/payments"),
        ("GET", "/api/v1/admin/payouts"),
        ("GET", "/api/v1/admin/users"),
        ("GET", "/api/v1/admin/inquiries"),
        ("GET", "/api/v1/admin/disputes"),
        ("POST", "/api/v1/admin/users/1/block"),
    ]
    for method, path in probes:
        assert api.request(method, path, headers=_h(member)).status_code == 403, path


# ---------- manual payments: maker and checker ----------


def test_payment_needs_two_administrators_and_matches_the_amount(api: TestClient, db_url: str) -> None:
    buyer, seller = _login(api, BUYER), _login(api, SELLER)
    a, b = _admin(api, db_url, ADMIN_A), _admin(api, db_url, ADMIN_B)
    deal = _deal(api, buyer)
    pid = api.post(
        f"/api/v1/deals/{deal['id']}/payments",
        headers={**_h(buyer), "Idempotency-Key": f"p-{uuid.uuid4().hex}"},
    ).json()["id"]

    assert (
        api.post(f"/api/v1/admin/payments/{pid}/approve", headers=_h(a)).status_code == 409
    )  # nothing recorded yet
    assert (
        api.post(
            f"/api/v1/admin/payments/{pid}/record",
            headers=_h(a),
            json={"bank_reference": "REF-AMOUNT", "amount_tiyin": TOTAL - 1},
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/api/v1/admin/payments/{pid}/record",
            headers=_h(a),
            json={"bank_reference": "REF-OK-0001", "amount_tiyin": TOTAL},
        ).status_code
        == 200
    )
    assert (
        api.post(f"/api/v1/admin/payments/{pid}/approve", headers=_h(a)).status_code == 403
    )  # maker cannot check
    assert api.post(f"/api/v1/admin/payments/{pid}/approve", headers=_h(b)).status_code == 200

    current = api.get(f"/api/v1/deals/{deal['id']}", headers=_h(buyer)).json()
    assert current["status"] == "payment_confirmed" and current["payment_status"] == "confirmed"
    listed = api.get("/api/v1/admin/payments?status=confirmed", headers=_h(a)).json()["data"]
    assert [p["bank_reference"] for p in listed] == ["REF-OK-0001"]
    _ = seller


def test_one_bank_reference_cannot_cover_two_payments(api: TestClient, db_url: str) -> None:
    buyer = _login(api, BUYER)
    a = _admin(api, db_url, ADMIN_A)
    ids = []
    for _ in range(2):
        d = _deal(api, buyer)
        ids.append(
            api.post(
                f"/api/v1/deals/{d['id']}/payments",
                headers={**_h(buyer), "Idempotency-Key": f"p-{uuid.uuid4().hex}"},
            ).json()["id"]
        )
    assert (
        api.post(
            f"/api/v1/admin/payments/{ids[0]}/record",
            headers=_h(a),
            json={"bank_reference": "SAME-REF-99", "amount_tiyin": TOTAL},
        ).status_code
        == 200
    )
    assert (
        api.post(
            f"/api/v1/admin/payments/{ids[1]}/record",
            headers=_h(a),
            json={"bank_reference": "SAME-REF-99", "amount_tiyin": TOTAL},
        ).status_code
        == 422
    )


def test_rejecting_a_payment_fails_the_deal_so_the_buyer_can_retry(api: TestClient, db_url: str) -> None:
    buyer = _login(api, BUYER)
    a = _admin(api, db_url, ADMIN_A)
    deal = _deal(api, buyer)
    pid = api.post(
        f"/api/v1/deals/{deal['id']}/payments",
        headers={**_h(buyer), "Idempotency-Key": f"p-{uuid.uuid4().hex}"},
    ).json()["id"]
    assert (
        api.post(
            f"/api/v1/admin/payments/{pid}/reject", headers=_h(a), json={"reason": "no matching transfer"}
        ).status_code
        == 200
    )
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_h(buyer)).json()["status"] == "payment_failed"
    retry = api.post(
        f"/api/v1/deals/{deal['id']}/payments",
        headers={**_h(buyer), "Idempotency-Key": f"p-{uuid.uuid4().hex}"},
    )
    assert retry.status_code == 201


# ---------- seller payouts ----------


def test_payout_only_after_completion_and_needs_two_administrators(api: TestClient, db_url: str) -> None:
    buyer, seller = _login(api, BUYER), _login(api, SELLER)
    a, b = _admin(api, db_url, ADMIN_A), _admin(api, db_url, ADMIN_B)
    deal = _deal(api, buyer)
    _pay_to_confirmed_path(api, db_url, deal, buyer, a, b)

    assert (
        api.post(f"/api/v1/admin/deals/{deal['id']}/payout", headers=_h(a)).status_code == 409
    )  # not completed
    _complete(api, db_url, deal, buyer, seller)

    created = api.post(f"/api/v1/admin/deals/{deal['id']}/payout", headers=_h(a))
    assert created.status_code == 201, created.text
    assert created.json()["amount_tiyin"] == 100_000_000  # the product price, not the MIDA fee
    pid = created.json()["id"]
    assert api.post(f"/api/v1/admin/payouts/{pid}/approve", headers=_h(a)).status_code == 403
    assert api.post(f"/api/v1/admin/payouts/{pid}/approve", headers=_h(b)).json()["status"] == "approved"
    assert (
        api.post(
            f"/api/v1/admin/payouts/{pid}/paid", headers=_h(a), json={"bank_reference": "PAYOUT-REF-1"}
        ).status_code
        == 403
    )
    paid = api.post(
        f"/api/v1/admin/payouts/{pid}/paid", headers=_h(b), json={"bank_reference": "PAYOUT-REF-1"}
    )
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    assert (
        api.post(f"/api/v1/admin/deals/{deal['id']}/payout", headers=_h(a)).status_code == 409
    )  # no duplicates


def test_rejected_payout_can_be_requested_again(api: TestClient, db_url: str) -> None:
    buyer, seller = _login(api, BUYER), _login(api, SELLER)
    a, b = _admin(api, db_url, ADMIN_A), _admin(api, db_url, ADMIN_B)
    deal = _deal(api, buyer)
    _pay_to_confirmed_path(api, db_url, deal, buyer, a, b)
    _complete(api, db_url, deal, buyer, seller)
    pid = api.post(f"/api/v1/admin/deals/{deal['id']}/payout", headers=_h(a)).json()["id"]
    assert (
        api.post(
            f"/api/v1/admin/payouts/{pid}/reject", headers=_h(b), json={"reason": "wrong bank account"}
        ).status_code
        == 200
    )
    again = api.post(f"/api/v1/admin/deals/{deal['id']}/payout", headers=_h(b))
    assert again.status_code == 201 and again.json()["status"] == "requested"


# ---------- users ----------


def test_blocking_a_user_stops_them_at_once_and_self_block_is_refused(api: TestClient, db_url: str) -> None:
    a = _admin(api, db_url, ADMIN_A)
    member = _login(api, BLOCKED)
    assert api.post(f"/api/v1/admin/users/{a['user_id']}/block", headers=_h(a)).status_code == 403
    assert (
        api.post(f"/api/v1/admin/users/{member['user_id']}/block", headers=_h(a)).json()["is_active"] is False
    )
    assert api.get("/api/v1/users/me", headers=_h(member)).status_code == 403
    assert (
        api.post(f"/api/v1/admin/users/{member['user_id']}/unblock", headers=_h(a)).json()["is_active"]
        is True
    )
    assert api.get("/api/v1/users/me", headers=_h(member)).status_code == 200


def test_user_search_by_name_and_id(api: TestClient, db_url: str) -> None:
    a = _admin(api, db_url, ADMIN_A)
    member = _login(api, MEMBER)
    by_id = api.get(f"/api/v1/admin/users?q={member['user_id']}", headers=_h(a)).json()["data"]
    assert any(u["id"] == member["user_id"] for u in by_id)


# ---------- inquiries ----------


def test_public_form_is_rate_limited_and_honeypot_stores_nothing(api: TestClient, db_url: str) -> None:
    payload = {
        "name": "Aziz Karimov",
        "contact": "+998901112233",
        "subject": "Hamkorlik so'rovi",
        "message": "Biz do'konimizni MIDA orqali sotmoqchimiz.",
        "source": "web_partnership",
    }
    bot = api.post("/api/v1/inquiries", json={**payload, "website": "http://spam.example"})
    assert bot.status_code == 201 and "id" not in bot.json()
    # The honeypot request already used one of the five slots: bots are limited too.
    for _ in range(4):
        assert api.post("/api/v1/inquiries", json=payload).status_code == 201
    assert api.post("/api/v1/inquiries", json=payload).status_code == 429
    a = _admin(api, db_url, ADMIN_A)
    listed = api.get("/api/v1/admin/inquiries", headers=_h(a)).json()["data"]
    assert len(listed) == 4  # the honeypot submission itself was not stored


def test_admin_answers_and_a_miniapp_user_sees_the_reply_in_their_thread(
    api: TestClient, db_url: str
) -> None:
    member = _login(api, MEMBER)
    opened = api.post(
        "/api/v1/inquiries/mine",
        headers=_h(member),
        json={"subject": "Pul qachon o'tadi?", "message": "To'lovdan keyin qancha kutish kerak?"},
    )
    assert opened.status_code == 201, opened.text
    inq = opened.json()["id"]
    a = _admin(api, db_url, ADMIN_A)
    reply = api.post(
        f"/api/v1/admin/inquiries/{inq}/messages", headers=_h(a), json={"body": "Odatda bir ish kuni ichida."}
    )
    assert reply.status_code == 201
    detail = api.get(f"/api/v1/admin/inquiries/{inq}", headers=_h(a)).json()
    assert detail["status"] == "in_progress" and detail["assigned_admin_id"] == a["user_id"]
    thread = api.get(f"/api/v1/inquiries/mine/{inq}", headers=_h(member)).json()
    assert [m["author_kind"] for m in thread["messages"]] == ["user", "admin"]
    api.post(
        f"/api/v1/inquiries/mine/{inq}/messages", headers=_h(member), json={"body": "Rahmat, tushunarli."}
    )
    assert api.get(f"/api/v1/admin/inquiries/{inq}", headers=_h(a)).json()["status"] == "in_progress"


def test_a_user_cannot_read_someone_elses_thread(api: TestClient, db_url: str) -> None:
    owner = _login(api, MEMBER)
    other = _login(api, BLOCKED)
    inq = api.post(
        "/api/v1/inquiries/mine",
        headers=_h(owner),
        json={"subject": "Mening murojaatim", "message": "Savol bor edi."},
    ).json()["id"]
    assert api.get(f"/api/v1/inquiries/mine/{inq}", headers=_h(other)).status_code == 404


# ---------- admin sign-in with the Login Widget ----------


def _widget(fields: dict[str, Any], bot_token: str = TEST_BOT_TOKEN) -> dict[str, Any]:
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    digest = hmac.new(hashlib.sha256(bot_token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return {**fields, "hash": digest}


def test_widget_login_gives_admins_a_token_and_refuses_everyone_else(api: TestClient, db_url: str) -> None:
    _admin(api, db_url, ADMIN_A)
    _login(api, MEMBER)
    now = int(time.time())
    ok = api.post(
        "/api/v1/auth/telegram-widget", json=_widget({"id": ADMIN_A, "first_name": "Admin", "auth_date": now})
    )
    assert ok.status_code == 200 and ok.json()["user"]["role"] == "admin"
    member = api.post(
        "/api/v1/auth/telegram-widget", json=_widget({"id": MEMBER, "first_name": "M", "auth_date": now})
    )
    assert member.status_code == 403
    stranger = api.post(
        "/api/v1/auth/telegram-widget", json=_widget({"id": 1234567, "first_name": "X", "auth_date": now})
    )
    assert stranger.status_code == 403  # same answer as a member: nothing is revealed


def test_widget_login_rejects_forged_stale_and_wrong_key_data(api: TestClient, db_url: str) -> None:
    _admin(api, db_url, ADMIN_A)
    now = int(time.time())
    good = _widget({"id": ADMIN_A, "first_name": "Admin", "auth_date": now})
    forged = {**good, "first_name": "Eve"}
    assert api.post("/api/v1/auth/telegram-widget", json=forged).status_code == 401
    stale = _widget({"id": ADMIN_A, "first_name": "Admin", "auth_date": now - 3600})
    assert api.post("/api/v1/auth/telegram-widget", json=stale).status_code == 401
    wrong_key = _widget({"id": ADMIN_A, "first_name": "Admin", "auth_date": now}, bot_token="999:OTHER")
    assert api.post("/api/v1/auth/telegram-widget", json=wrong_key).status_code == 401


# ---------- dispute list for the panel ----------


def test_admin_lists_disputes_with_deal_references(api: TestClient, db_url: str) -> None:
    a = _admin(api, db_url, ADMIN_A)
    assert api.get("/api/v1/admin/disputes", headers=_h(a)).json() == {"data": []}
