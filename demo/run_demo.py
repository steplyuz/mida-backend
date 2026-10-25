"""End-to-end demo of the MIDA backend against a running local server.

Shows login, deal creation with server-computed fee, idempotent replay, payment start, a signed
provider confirmation, a forged webhook being refused, payment status and the audit trail.
No money moves: the payment provider is a local mock.

Run the server first (see demo/README), then: python demo/run_demo.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

BASE = os.environ.get("MIDA_URL", "http://127.0.0.1:8000")
BOT_TOKEN = "123456:DEMO-BOT-TOKEN-NOT-REAL"
WEBHOOK_SECRET = "demo-only-telegram-webhook-secret-value"
MOCK_SECRET = "demo-only-mock-payment-webhook-secret-32c"
DB_PATH = Path(os.environ.get("MIDA_DB", "demo.db"))
BUYER_TG, SELLER_TG = 880001, 880002

STEP = 0


def step(title: str) -> None:
    global STEP
    STEP += 1
    print(f"\n[{STEP:02d}] {title}")


def show(label: str, value: Any) -> None:
    print(f"     {label}: {value}")


def init_data(telegram_user_id: int, first_name: str) -> str:
    """Signs initData the way Telegram does: HMAC over the sorted fields, keyed by the bot token."""
    user = json.dumps({"id": telegram_user_id, "first_name": first_name}, separators=(",", ":"))
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def login(client: httpx.Client, tg: int, name: str) -> dict[str, Any]:
    response = client.post("/api/v1/auth/telegram", json={"init_data": init_data(tg, name)})
    response.raise_for_status()
    body = response.json()
    return {"name": name, "token": body["access_token"], "user_id": body["user"]["id"]}


def auth(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def signed_webhook(body: dict[str, Any], secret: str = MOCK_SECRET) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Mock-Signature": f"sha256={digest}", "Content-Type": "application/json"}


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=10)

    step("Server health")
    health = client.get("/health").json()
    show("status", health["status"])

    step("Two parties sign in with Telegram Mini App data (server verifies the HMAC)")
    buyer = login(client, BUYER_TG, "Dilshod")
    seller = login(client, SELLER_TG, "Malika")
    show(
        "account role of buyer (deal role is per deal, not per account)",
        client.get("/api/v1/users/me", headers=auth(buyer)).json()["role"],
    )
    show(
        "account role of seller",
        client.get("/api/v1/users/me", headers=auth(seller)).json()["role"],
    )

    step("Buyer creates a deal for a 1 000 000 UZS product (amounts are integer tiyin)")
    key = f"demo-deal-{uuid.uuid4().hex}"
    payload = {
        "my_role": "buyer",
        "counterparty_telegram_user_id": SELLER_TG,
        "title": "Velosiped Trek (demo)",
        "product_amount_tiyin": 100_000_000,
        "terms_text": "Demo terms v1. Not a legal contract.",
    }
    created = client.post(
        "/api/v1/deals", headers={**auth(buyer), "Idempotency-Key": key}, json=payload
    )
    created.raise_for_status()
    deal = created.json()
    show("deal number", deal["deal_number"])
    show("product", f"{deal['product_amount_tiyin'] // 100} UZS")
    show("service fee (server-computed, 10%)", f"{deal['service_fee_tiyin'] // 100} UZS")
    show("total buyer pays", f"{deal['total_amount_tiyin'] // 100} UZS")
    show("status", deal["status"])

    step("Client retries the same request with the same Idempotency-Key")
    again = client.post(
        "/api/v1/deals", headers={**auth(buyer), "Idempotency-Key": key}, json=payload
    )
    show("same deal returned", again.json()["id"] == deal["id"])
    show("replay header", again.headers.get("Idempotent-Replayed"))

    step("Same key with a changed body is refused (no silent overwrite)")
    changed = client.post(
        "/api/v1/deals",
        headers={**auth(buyer), "Idempotency-Key": key},
        json={**payload, "title": "Something else"},
    )
    show("HTTP status", changed.status_code)
    show("error type", changed.json()["type"])

    step("Outsider cannot see the deal (no existence leak)")
    outsider = login(client, 880099, "Nodir")
    outsider_view = client.get(f"/api/v1/deals/{deal['id']}", headers=auth(outsider))
    show("HTTP status", outsider_view.status_code)

    step("Buyer starts payment. Provider is a local mock; no money moves")
    pay = client.post(
        f"/api/v1/deals/{deal['id']}/payments",
        headers={**auth(buyer), "Idempotency-Key": f"pay-{uuid.uuid4().hex}"},
    )
    pay.raise_for_status()
    payment = pay.json()
    show("payment status", payment["status"])
    show("checkout link (buyer only)", payment["checkout_url"])
    show(
        "deal status now",
        client.get(f"/api/v1/deals/{deal['id']}", headers=auth(buyer)).json()["status"],
    )

    with sqlite3.connect(DB_PATH) as conn:
        tx = conn.execute(
            "SELECT p.provider_transaction_id FROM payments p JOIN deals d ON d.id = p.deal_id "
            "WHERE d.public_id = ? ORDER BY p.id DESC LIMIT 1",
            (deal["id"],),
        ).fetchone()[0]

    step("Forged 'paid' webhook (wrong signing key) is refused")
    forged_body, forged_headers = signed_webhook(
        {
            "event_id": "evt-forged-1",
            "type": "payment.succeeded",
            "provider_transaction_id": tx,
            "amount_tiyin": deal["total_amount_tiyin"],
            "currency": "UZS",
        },
        secret="attacker-guess-key-not-real-32chars",
    )
    forged = client.post(
        "/api/v1/payments/webhook/mock", content=forged_body, headers=forged_headers
    )
    show("HTTP status", forged.status_code)
    show(
        "deal status after forgery",
        client.get(f"/api/v1/deals/{deal['id']}", headers=auth(buyer)).json()["status"],
    )

    step("Provider confirms payment with a correctly signed webhook")
    good_body, good_headers = signed_webhook(
        {
            "event_id": "evt-ok-1",
            "type": "payment.succeeded",
            "provider_transaction_id": tx,
            "amount_tiyin": deal["total_amount_tiyin"],
            "currency": "UZS",
        }
    )
    confirmed = client.post(
        "/api/v1/payments/webhook/mock", content=good_body, headers=good_headers
    ).json()
    show("webhook outcome", confirmed["status"])
    current = client.get(f"/api/v1/deals/{deal['id']}", headers=auth(buyer)).json()
    show("deal status", current["status"])
    show("deal payment status", current["payment_status"])

    step("Provider redelivers the same event: applied once")
    redelivered = client.post(
        "/api/v1/payments/webhook/mock", content=good_body, headers=good_headers
    ).json()
    show("webhook outcome", redelivered["status"])

    step("Parties see the same deal, but only the buyer gets the checkout link")
    seller_payments = client.get(
        f"/api/v1/deals/{deal['id']}/payments", headers=auth(seller)
    ).json()["data"]
    show("seller sees payment status", seller_payments[0]["status"])
    show("seller sees checkout link", seller_payments[0]["checkout_url"])
    status = client.get(f"/api/v1/deals/{deal['id']}/payment-status", headers=auth(seller)).json()
    show("payment-status", status)

    step("Audit trail records every state change with its actor")
    events = client.get(f"/api/v1/deals/{deal['id']}/events", headers=auth(buyer)).json()["data"]
    for event in events:
        show(event["action"], f"actor={event['actor_kind']}, new={event['new_values']}")

    step("What is NOT in this demo")
    show("bank / payment processor", "not connected (contract pending)")
    show("Telegram group creation", "needs MTProto credentials; not run here")
    show("seller delivery and buyer confirmation", "no endpoints yet (Phase 8 and later)")
    print("\nDemo finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
