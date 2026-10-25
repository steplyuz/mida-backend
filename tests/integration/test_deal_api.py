"""Deal HTTP API over a migrated SQLite file: lifecycle, access control, idempotency, pagination."""

from __future__ import annotations

import asyncio
import sqlite3
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
from app.main import create_app
from app.models.enums import DealStatus
from app.services.deal_service import DealService
from app.services.pricing_service import PricingService
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data

APP_SECRET = "k" * 32
BUYER_TG, SELLER_TG, OUTSIDER_TG = 9001, 9002, 9003
ONE_MILLION_UZS = 100_000_000  # tiyin


def _sqlite_path(db_url: str) -> Path:
    return Path(db_url.removeprefix("sqlite+aiosqlite:///"))


@pytest.fixture
def api(db_url: str) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr(APP_SECRET),
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _login(client: TestClient, telegram_user_id: int) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/telegram",
        json={
            "init_data": valid_init_data(telegram_user_id=telegram_user_id, first_name=f"U{telegram_user_id}")
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return {"token": body["access_token"], "user_id": body["user"]["id"]}


@pytest.fixture
def people(api: TestClient) -> dict[str, dict[str, Any]]:
    return {
        "buyer": _login(api, BUYER_TG),
        "seller": _login(api, SELLER_TG),
        "outsider": _login(api, OUTSIDER_TG),
    }


def _auth(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def _create(
    api: TestClient,
    who: dict[str, Any],
    *,
    my_role: str = "buyer",
    counterparty: int = SELLER_TG,
    key: str | None = None,
    title: str = "iPhone 15",
    amount: Any = ONE_MILLION_UZS,
) -> Any:
    headers = {**_auth(who), "Idempotency-Key": key or f"create-{uuid.uuid4().hex}"}
    return api.post(
        "/api/v1/deals",
        headers=headers,
        json={
            "my_role": my_role,
            "counterparty_telegram_user_id": counterparty,
            "title": title,
            "product_amount_tiyin": amount,
            "terms_text": "Standard MIDA terms",
        },
    )


def _deal(api: TestClient, who: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    response = _create(api, who, **kwargs)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _run_system_steps(db_url: str, public_id: str, steps: list[tuple[Actor, DealStatus]]) -> None:
    """Drives service-level transitions that have no HTTP endpoint yet (payment and delivery)."""

    async def run() -> None:
        engine = create_engine_from_url(db_url)
        factory = create_session_factory(engine)
        pricing = PricingService(service_fee_bps=1000, max_amount_uzs=1_000_000_000)
        try:
            async with factory() as session:
                deal = await DealService(session, pricing).get_deal_by_public_id(Actor.system(), public_id)
            for actor, target in steps:
                async with factory() as session:
                    deal = await DealService(session, pricing).transition(
                        actor, deal.id, target, expected_version=deal.version
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def _set_role(db_url: str, telegram_user_id: int, role: str) -> None:
    with sqlite3.connect(_sqlite_path(db_url)) as conn:
        conn.execute("UPDATE users SET role = ? WHERE telegram_user_id = ?", (role, telegram_user_id))


# ---------- create ----------


def test_create_returns_server_computed_price(api: TestClient, people: dict[str, Any]) -> None:
    body = _deal(api, people["buyer"])
    assert body["deal_number"].startswith("MIDA-")
    assert body["my_role"] == "buyer"
    assert body["status"] == "created"
    assert body["service_fee_tiyin"] == 10_000_000
    assert body["total_amount_tiyin"] == 110_000_000
    assert uuid.UUID(body["id"])  # public UUID, not the internal integer id
    assert "telegram_user_id" not in body  # internal Telegram identities are never exposed


def test_seller_side_creation_reports_seller_role(api: TestClient, people: dict[str, Any]) -> None:
    body = _deal(api, people["seller"], my_role="seller", counterparty=BUYER_TG)
    assert body["my_role"] == "seller"
    assert body["seller_user_id"] == people["seller"]["user_id"]


def test_missing_idempotency_key_is_rejected(api: TestClient, people: dict[str, Any]) -> None:
    response = api.post(
        "/api/v1/deals",
        headers=_auth(people["buyer"]),
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "X",
            "product_amount_tiyin": 100,
            "terms_text": "t",
        },
    )
    assert response.status_code == 400
    assert response.json()["type"] == "urn:mida:error:idempotency-key-invalid"


def test_malformed_idempotency_key_is_rejected(api: TestClient, people: dict[str, Any]) -> None:
    response = _create(api, people["buyer"], key="short")
    assert response.status_code == 400


@pytest.mark.parametrize("amount", [1.5, "100000000", True, 0, -5])
def test_amount_must_be_a_positive_integer(api: TestClient, people: dict[str, Any], amount: Any) -> None:
    assert _create(api, people["buyer"], amount=amount).status_code == 422


def test_amount_above_the_configured_maximum_is_rejected(api: TestClient, people: dict[str, Any]) -> None:
    too_big = 1_000_000_000 * 100 + 1  # max deal is 1 000 000 000 UZS
    response = _create(api, people["buyer"], amount=too_big)
    assert response.status_code == 422
    assert response.json()["type"] == "urn:mida:error:invalid-amount"


def test_client_cannot_name_internal_parties_or_fees(api: TestClient, people: dict[str, Any]) -> None:
    response = api.post(
        "/api/v1/deals",
        headers={**_auth(people["buyer"]), "Idempotency-Key": f"inject-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "X",
            "product_amount_tiyin": 100,
            "terms_text": "t",
            "buyer_user_id": 1,
            "service_fee_tiyin": 0,
            "total_amount_tiyin": 1,
        },
    )
    assert response.status_code == 422


def test_counterparty_must_have_signed_in(api: TestClient, people: dict[str, Any]) -> None:
    response = _create(api, people["buyer"], counterparty=777_777)
    assert response.status_code == 422
    assert "sign in" in response.json()["detail"]


def test_cannot_create_a_deal_with_yourself(api: TestClient, people: dict[str, Any]) -> None:
    assert _create(api, people["buyer"], counterparty=BUYER_TG).status_code == 422


# ---------- idempotency ----------


def test_same_key_and_body_replays_the_same_deal(api: TestClient, people: dict[str, Any]) -> None:
    key = f"replay-{uuid.uuid4().hex}"
    first = _create(api, people["buyer"], key=key)
    second = _create(api, people["buyer"], key=key)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["deal_number"] == second.json()["deal_number"]
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert first.headers.get("Idempotent-Replayed") is None

    listed = api.get("/api/v1/deals", headers=_auth(people["buyer"])).json()
    assert len(listed["data"]) == 1  # one deal, not two


def test_same_key_with_different_body_is_a_conflict(api: TestClient, people: dict[str, Any]) -> None:
    key = f"conflict-{uuid.uuid4().hex}"
    assert _create(api, people["buyer"], key=key, title="A").status_code == 201
    response = _create(api, people["buyer"], key=key, title="B")
    assert response.status_code == 409
    assert response.json()["type"] == "urn:mida:error:idempotency-key-reused"


def test_same_key_for_another_user_is_independent(api: TestClient, people: dict[str, Any]) -> None:
    key = f"shared-{uuid.uuid4().hex}"
    mine = _create(api, people["buyer"], key=key)
    theirs = _create(api, people["seller"], my_role="seller", counterparty=BUYER_TG, key=key)
    assert mine.status_code == theirs.status_code == 201
    assert mine.json()["id"] != theirs.json()["id"]


def test_cancel_replay_returns_the_original_result(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    key = f"cancel-{uuid.uuid4().hex}"
    body = {"expected_version": deal["version"], "reason": "changed my mind"}
    first = api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers={**_auth(people["buyer"]), "Idempotency-Key": key},
        json=body,
    )
    again = api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers={**_auth(people["buyer"]), "Idempotency-Key": key},
        json=body,
    )
    assert first.status_code == again.status_code == 200
    assert again.json()["status"] == "cancelled"
    assert again.json()["version"] == first.json()["version"] == 2
    assert again.headers.get("Idempotent-Replayed") == "true"


def test_cancel_with_same_key_but_different_reason_conflicts(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    key = f"cancel-conflict-{uuid.uuid4().hex}"
    headers = {**_auth(people["buyer"]), "Idempotency-Key": key}
    api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers=headers,
        json={"expected_version": 1, "reason": "a"},
    )
    response = api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers=headers,
        json={"expected_version": 1, "reason": "b"},
    )
    assert response.status_code == 409


def test_new_key_on_a_cancelled_deal_is_an_invalid_transition(
    api: TestClient, people: dict[str, Any]
) -> None:
    deal = _deal(api, people["buyer"])
    api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers={**_auth(people["buyer"]), "Idempotency-Key": f"c1-{uuid.uuid4().hex}"},
        json={"expected_version": 1},
    )
    response = api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers={**_auth(people["buyer"]), "Idempotency-Key": f"c2-{uuid.uuid4().hex}"},
        json={"expected_version": 2},
    )
    assert response.status_code == 409
    assert response.json()["type"] == "urn:mida:error:invalid-transition"


def test_cancel_requires_idempotency_key(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    response = api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers=_auth(people["buyer"]),
        json={"expected_version": 1},
    )
    assert response.status_code == 400


# ---------- reads, access control (BOLA/IDOR) ----------


def test_participants_can_read_their_deal(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(people["buyer"])).status_code == 200
    seller_view = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(people["seller"]))
    assert seller_view.status_code == 200
    assert seller_view.json()["my_role"] == "seller"


def test_outsider_gets_not_found_for_every_deal_route(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    outsider = _auth(people["outsider"])
    base = f"/api/v1/deals/{deal['id']}"
    checks = [
        api.get(base, headers=outsider),
        api.patch(base, headers=outsider, json={"expected_version": 1, "title": "hijack"}),
        api.post(
            f"{base}/cancel",
            headers={**outsider, "Idempotency-Key": f"o-{uuid.uuid4().hex}"},
            json={"expected_version": 1},
        ),
        api.post(
            f"{base}/confirm-receipt",
            headers={**outsider, "Idempotency-Key": f"o-{uuid.uuid4().hex}"},
            json={"expected_version": 1},
        ),
        api.get(f"{base}/events", headers=outsider),
        api.get(f"{base}/participants", headers=outsider),
    ]
    assert all(response.status_code == 404 for response in checks)


def test_missing_and_foreign_deals_are_indistinguishable(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    foreign = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(people["outsider"]))
    missing = api.get(f"/api/v1/deals/{uuid.uuid4()}", headers=_auth(people["outsider"]))
    malformed = api.get("/api/v1/deals/not-a-uuid", headers=_auth(people["outsider"]))
    assert foreign.status_code == missing.status_code == malformed.status_code == 404
    assert foreign.json()["detail"] == missing.json()["detail"] == malformed.json()["detail"]


def test_outsider_listing_shows_no_foreign_deals(api: TestClient, people: dict[str, Any]) -> None:
    _deal(api, people["buyer"])
    listed = api.get("/api/v1/deals", headers=_auth(people["outsider"])).json()
    assert listed["data"] == []


def test_admin_can_read_any_deal(api: TestClient, people: dict[str, Any], db_url: str) -> None:
    deal = _deal(api, people["buyer"])
    admin = _login(api, 9100)
    _set_role(db_url, 9100, "admin")
    response = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(admin))
    assert response.status_code == 200
    assert response.json()["my_role"] == "admin"


def test_forged_identity_in_body_does_not_grant_access(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    response = api.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=_auth(people["outsider"]),
        json={"expected_version": 1, "title": "X", "buyer_user_id": people["outsider"]["user_id"]},
    )
    assert response.status_code == 422  # extra field rejected before access is even considered


# ---------- listing, filtering, pagination ----------


def test_listing_paginates_without_gaps_or_duplicates(api: TestClient, people: dict[str, Any]) -> None:
    created = [_deal(api, people["buyer"], title=f"Item {i}")["id"] for i in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = api.get("/api/v1/deals", headers=_auth(people["buyer"]), params=params).json()
        seen.extend(item["id"] for item in page["data"])
        if not page["pagination"]["has_more"]:
            break
        cursor = page["pagination"]["next_cursor"]
    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen))


def test_default_sort_is_newest_first_and_ascending_is_supported(
    api: TestClient, people: dict[str, Any]
) -> None:
    first = _deal(api, people["buyer"], title="first")
    second = _deal(api, people["buyer"], title="second")
    desc = api.get("/api/v1/deals", headers=_auth(people["buyer"])).json()["data"]
    asc = api.get("/api/v1/deals", headers=_auth(people["buyer"]), params={"sort": "created_at"}).json()[
        "data"
    ]
    assert [d["id"] for d in desc] == [second["id"], first["id"]]
    assert [d["id"] for d in asc] == [first["id"], second["id"]]


def test_status_filter(api: TestClient, people: dict[str, Any]) -> None:
    keep = _deal(api, people["buyer"], title="keep")
    drop = _deal(api, people["buyer"], title="drop")
    api.post(
        f"/api/v1/deals/{drop['id']}/cancel",
        headers={**_auth(people["buyer"]), "Idempotency-Key": f"f-{uuid.uuid4().hex}"},
        json={"expected_version": 1},
    )
    cancelled = api.get(
        "/api/v1/deals", headers=_auth(people["buyer"]), params={"status": "cancelled"}
    ).json()["data"]
    created = api.get("/api/v1/deals", headers=_auth(people["buyer"]), params={"status": "created"}).json()[
        "data"
    ]
    assert [d["id"] for d in cancelled] == [drop["id"]]
    assert [d["id"] for d in created] == [keep["id"]]


def test_tampered_or_mismatched_cursor_is_rejected(api: TestClient, people: dict[str, Any]) -> None:
    for _ in range(3):
        _deal(api, people["buyer"])
    page = api.get("/api/v1/deals", headers=_auth(people["buyer"]), params={"limit": 1}).json()
    cursor = page["pagination"]["next_cursor"]
    assert cursor
    tampered = api.get("/api/v1/deals", headers=_auth(people["buyer"]), params={"cursor": cursor[:-2] + "zz"})
    assert tampered.status_code == 422
    mismatched = api.get(
        "/api/v1/deals",
        headers=_auth(people["buyer"]),
        params={"cursor": cursor, "sort": "created_at"},
    )
    assert mismatched.status_code == 422


def test_invalid_sort_and_limit_are_rejected(api: TestClient, people: dict[str, Any]) -> None:
    assert (
        api.get("/api/v1/deals", headers=_auth(people["buyer"]), params={"sort": "title"}).status_code == 422
    )
    assert api.get("/api/v1/deals", headers=_auth(people["buyer"]), params={"limit": 101}).status_code == 422


# ---------- detail updates ----------


def test_title_edit_bumps_version(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    response = api.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=_auth(people["seller"]),
        json={"expected_version": 1, "title": "  New   title "},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "New title"
    assert response.json()["version"] == 2


def test_stale_version_edit_conflicts(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    api.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=_auth(people["buyer"]),
        json={"expected_version": 1, "title": "one"},
    )
    response = api.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=_auth(people["buyer"]),
        json={"expected_version": 1, "title": "two"},
    )
    assert response.status_code == 409
    assert response.json()["type"] == "urn:mida:error:concurrent-modification"


def test_price_and_terms_cannot_be_edited(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    response = api.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=_auth(people["buyer"]),
        json={"expected_version": 1, "product_amount_tiyin": 1},
    )
    assert response.status_code == 422


def test_edits_are_rejected_after_payment_has_progressed(
    api: TestClient, people: dict[str, Any], db_url: str
) -> None:
    deal = _deal(api, people["buyer"])
    _run_system_steps(db_url, deal["id"], [(Actor.system(), DealStatus.AWAITING_PAYMENT)])
    assert (
        api.patch(
            f"/api/v1/deals/{deal['id']}",
            headers=_auth(people["buyer"]),
            json={"expected_version": 2, "title": "still ok"},
        ).status_code
        == 200
    )
    _run_system_steps(db_url, deal["id"], [(Actor.system(), DealStatus.PAYMENT_PENDING_VERIFICATION)])
    response = api.patch(
        f"/api/v1/deals/{deal['id']}",
        headers=_auth(people["buyer"]),
        json={"expected_version": 4, "title": "too late"},
    )
    assert response.status_code == 409


# ---------- completion, roles, audit, participants ----------


def test_only_the_buyer_can_confirm_receipt(api: TestClient, people: dict[str, Any], db_url: str) -> None:
    deal = _deal(api, people["buyer"])
    _run_system_steps(
        db_url,
        deal["id"],
        [
            (Actor.system(), DealStatus.AWAITING_PAYMENT),
            (Actor.system(), DealStatus.PAYMENT_PENDING_VERIFICATION),
            (Actor.system(), DealStatus.PAYMENT_CONFIRMED),
            (Actor.user(people["seller"]["user_id"]), DealStatus.DELIVERY_IN_PROGRESS),
            (Actor.user(people["seller"]["user_id"]), DealStatus.AWAITING_BUYER_CONFIRMATION),
        ],
    )
    current = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(people["buyer"])).json()
    url = f"/api/v1/deals/{deal['id']}/confirm-receipt"

    seller_attempt = api.post(
        url,
        headers={**_auth(people["seller"]), "Idempotency-Key": f"s-{uuid.uuid4().hex}"},
        json={"expected_version": current["version"]},
    )
    assert seller_attempt.status_code == 403

    key = f"confirm-{uuid.uuid4().hex}"
    done = api.post(
        url,
        headers={**_auth(people["buyer"]), "Idempotency-Key": key},
        json={"expected_version": current["version"]},
    )
    replay = api.post(
        url,
        headers={**_auth(people["buyer"]), "Idempotency-Key": key},
        json={"expected_version": current["version"]},
    )
    assert done.status_code == replay.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["completed_at"] is not None
    assert replay.headers.get("Idempotent-Replayed") == "true"


def test_events_are_visible_to_parties_in_order(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers={**_auth(people["buyer"]), "Idempotency-Key": f"e-{uuid.uuid4().hex}"},
        json={"expected_version": 1, "reason": "no longer needed"},
    )
    events = api.get(f"/api/v1/deals/{deal['id']}/events", headers=_auth(people["seller"])).json()["data"]
    assert [e["action"] for e in events] == ["deal.created", "deal.status_changed"]
    assert events[1]["old_values"]["status"] == "created"
    assert events[1]["new_values"]["status"] == "cancelled"
    assert events[1]["new_values"]["reason"] == "no longer needed"


def test_events_paginate_by_cursor(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers={**_auth(people["buyer"]), "Idempotency-Key": f"p-{uuid.uuid4().hex}"},
        json={"expected_version": 1},
    )
    first = api.get(
        f"/api/v1/deals/{deal['id']}/events", headers=_auth(people["buyer"]), params={"limit": 1}
    ).json()
    assert len(first["data"]) == 1 and first["pagination"]["has_more"] is True
    second = api.get(
        f"/api/v1/deals/{deal['id']}/events",
        headers=_auth(people["buyer"]),
        params={"limit": 1, "cursor": first["pagination"]["next_cursor"]},
    ).json()
    assert second["data"][0]["id"] > first["data"][0]["id"]
    assert second["pagination"]["has_more"] is False


def test_participants_lists_buyer_and_seller(api: TestClient, people: dict[str, Any]) -> None:
    deal = _deal(api, people["buyer"])
    body = api.get(f"/api/v1/deals/{deal['id']}/participants", headers=_auth(people["seller"])).json()
    roles = {p["role"]: p["user_id"] for p in body["data"]}
    assert roles == {"buyer": people["buyer"]["user_id"], "seller": people["seller"]["user_id"]}
    assert body["pagination"]["has_more"] is False


def test_deal_routes_require_authentication(api: TestClient) -> None:
    assert api.get("/api/v1/deals").status_code == 401
    assert api.post("/api/v1/deals", json={}).status_code == 401
