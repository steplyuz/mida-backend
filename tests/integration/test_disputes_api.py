"""Disputes and evidence over HTTP on a migrated SQLite file. Mock payments, real signatures."""

from __future__ import annotations

import hashlib
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

BUYER_TG, SELLER_TG, OUTSIDER_TG, ADMIN_TG, ADMIN2_TG = 9201, 9202, 9203, 9204, 9205
TOTAL = 110_000_000
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
DESCRIPTION = "Mahsulot kelmadi, sotuvchi trek raqamini bermadi."
REASON = "Seller did not deliver the item and stopped answering after payment."


def _sqlite(db_url: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(db_url.removeprefix("sqlite+aiosqlite:///")))


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    return tmp_path / "evidence"


@pytest.fixture
def api(db_url: str, evidence_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        evidence_storage_path=str(evidence_dir),
        evidence_max_bytes=5_000,
    )
    provider = MockPaymentProvider(webhook_secret=MOCK_SECRET, app_env="test")
    with TestClient(create_app(settings, payment_provider=provider)) as client:
        yield client


def _login(client: TestClient, tg: int) -> dict[str, Any]:
    body = client.post(
        "/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=tg)}
    ).json()
    return {"token": body["access_token"], "user_id": body["user"]["id"], "tg": tg}


def _auth(user: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {user['token']}"}


def _promote(db_url: str, tg: int) -> None:
    with _sqlite(db_url) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE telegram_user_id = ?", (tg,))


def _admin(api: TestClient, db_url: str, tg: int) -> dict[str, Any]:
    """Users exist only after their first login, so log in first, then grant the role.
    The role is read on every request, so the same token works after promotion."""
    user = _login(api, tg)
    _promote(db_url, tg)
    return user


def _confirmed_deal(api: TestClient, db_url: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """A deal whose payment is confirmed, so a dispute may be opened from it."""
    buyer, seller = _login(api, BUYER_TG), _login(api, SELLER_TG)
    deal = api.post(
        "/api/v1/deals",
        headers={**_auth(buyer), "Idempotency-Key": f"d-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "Bike",
            "product_amount_tiyin": 100_000_000,
            "terms_text": "terms",
        },
    ).json()
    api.post(
        f"/api/v1/deals/{deal['id']}/payments",
        headers={**_auth(buyer), "Idempotency-Key": f"p-{uuid.uuid4().hex}"},
    )
    with _sqlite(db_url) as conn:
        tx = conn.execute(
            "SELECT p.provider_transaction_id FROM payments p "
            "JOIN deals d ON d.id = p.deal_id WHERE d.public_id = ?",
            (deal["id"],),
        ).fetchone()[0]
    body, headers = signed(event(f"evt-{uuid.uuid4().hex}", tx, amount=TOTAL))
    assert (
        api.post("/api/v1/payments/webhook/mock", content=body, headers=headers).json()["status"]
        == "confirmed"
    )
    deal = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()
    return deal, buyer, seller


def _open(api: TestClient, deal: dict[str, Any], who: dict[str, Any], version: int | None = None) -> Any:
    return api.post(
        f"/api/v1/deals/{deal['id']}/disputes",
        headers=_auth(who),
        json={
            "reason_code": "not_delivered",
            "description": DESCRIPTION,
            "expected_version": version if version is not None else deal["version"],
        },
    )


# ---------- opening ----------


def test_buyer_opens_a_dispute_and_the_deal_is_frozen_into_disputed(api: TestClient, db_url: str) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    response = _open(api, deal, buyer)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "open" and body["reason_code"] == "not_delivered"
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(seller)).json()["status"] == "disputed"


def test_only_one_unresolved_dispute_per_deal(api: TestClient, db_url: str) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    assert _open(api, deal, buyer).status_code == 201
    again = _open(api, deal, seller, version=deal["version"] + 1)
    assert again.status_code == 409


def test_outsider_cannot_see_or_open_and_admin_is_not_a_party(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    outsider = _login(api, OUTSIDER_TG)
    assert _open(api, deal, outsider).status_code == 404
    admin = _admin(api, db_url, ADMIN_TG)
    assert _open(api, deal, admin).status_code == 403


def test_stale_version_cannot_open_a_dispute(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    assert _open(api, deal, buyer, version=deal["version"] - 1).status_code == 409


def test_a_created_deal_cannot_be_disputed(api: TestClient, db_url: str) -> None:
    buyer, _seller = _login(api, BUYER_TG), _login(api, SELLER_TG)
    deal = api.post(
        "/api/v1/deals",
        headers={**_auth(buyer), "Idempotency-Key": f"c-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "X",
            "product_amount_tiyin": 100_000_000,
            "terms_text": "terms",
        },
    ).json()
    assert _open(api, deal, buyer).status_code == 409


# ---------- decision ----------


def test_admin_assigns_then_resolves_and_the_deal_follows_the_decision(api: TestClient, db_url: str) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    dispute = _open(api, deal, buyer).json()
    admin = _admin(api, db_url, ADMIN_TG)

    assert (
        api.post(
            f"/api/v1/disputes/{dispute['id']}/assign",
            headers=_auth(buyer),
            json={"expected_version": 1},
        ).status_code
        == 403
    )
    assigned = api.post(
        f"/api/v1/disputes/{dispute['id']}/assign",
        headers=_auth(admin),
        json={"expected_version": 1},
    ).json()
    assert assigned["status"] == "under_review" and assigned["assigned_admin_user_id"] == admin["user_id"]

    resolved = api.post(
        f"/api/v1/disputes/{dispute['id']}/resolve",
        headers=_auth(admin),
        json={
            "resolution": "buyer_wins",
            "reason": REASON,
            "expected_version": assigned["version"],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved" and resolved.json()["resolution"] == "buyer_wins"
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(seller)).json()["status"] == "cancelled"

    events = api.get(f"/api/v1/deals/{deal['id']}/events", headers=_auth(seller)).json()["data"]
    actions = [e["action"] for e in events]
    assert "dispute.opened" in actions and "dispute.assigned" in actions and "dispute.resolved" in actions
    resolution_event = next(e for e in events if e["action"] == "dispute.resolved")
    assert resolution_event["actor_kind"] == "admin"
    assert resolution_event["new_values"]["reason"] == REASON


def test_seller_wins_completes_the_deal(api: TestClient, db_url: str) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    dispute = _open(api, deal, seller).json()
    admin = _admin(api, db_url, ADMIN_TG)
    assigned = api.post(
        f"/api/v1/disputes/{dispute['id']}/assign",
        headers=_auth(admin),
        json={"expected_version": 1},
    ).json()
    api.post(
        f"/api/v1/disputes/{dispute['id']}/resolve",
        headers=_auth(admin),
        json={
            "resolution": "seller_wins",
            "reason": REASON,
            "expected_version": assigned["version"],
        },
    )
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()["status"] == "completed"


def test_resolution_is_only_for_the_assigned_admin(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    dispute = _open(api, deal, buyer).json()
    first = _admin(api, db_url, ADMIN_TG)
    second = _admin(api, db_url, ADMIN2_TG)
    assigned = api.post(
        f"/api/v1/disputes/{dispute['id']}/assign",
        headers=_auth(first),
        json={"expected_version": 1},
    ).json()
    other = api.post(
        f"/api/v1/disputes/{dispute['id']}/resolve",
        headers=_auth(second),
        json={
            "resolution": "buyer_wins",
            "reason": REASON,
            "expected_version": assigned["version"],
        },
    )
    assert other.status_code == 403


def test_resolution_before_assignment_is_refused(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    dispute = _open(api, deal, buyer).json()
    admin = _admin(api, db_url, ADMIN_TG)
    response = api.post(
        f"/api/v1/disputes/{dispute['id']}/resolve",
        headers=_auth(admin),
        json={"resolution": "buyer_wins", "reason": REASON, "expected_version": 1},
    )
    assert response.status_code == 409


def test_resolution_needs_a_real_reason(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    dispute = _open(api, deal, buyer).json()
    admin = _admin(api, db_url, ADMIN_TG)
    api.post(
        f"/api/v1/disputes/{dispute['id']}/assign",
        headers=_auth(admin),
        json={"expected_version": 1},
    )
    response = api.post(
        f"/api/v1/disputes/{dispute['id']}/resolve",
        headers=_auth(admin),
        json={"resolution": "buyer_wins", "reason": "no", "expected_version": 2},
    )
    assert response.status_code == 422


def test_a_resolved_deal_cannot_be_disputed_again(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    dispute = _open(api, deal, buyer).json()
    admin = _admin(api, db_url, ADMIN_TG)
    assigned = api.post(
        f"/api/v1/disputes/{dispute['id']}/assign",
        headers=_auth(admin),
        json={"expected_version": 1},
    ).json()
    api.post(
        f"/api/v1/disputes/{dispute['id']}/resolve",
        headers=_auth(admin),
        json={
            "resolution": "buyer_wins",
            "reason": REASON,
            "expected_version": assigned["version"],
        },
    )
    current = api.get(f"/api/v1/deals/{deal['id']}", headers=_auth(buyer)).json()
    assert _open(api, deal, buyer, version=current["version"]).status_code == 409


def test_dispute_is_visible_to_parties_and_admins_only(api: TestClient, db_url: str) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    dispute = _open(api, deal, buyer).json()
    assert api.get(f"/api/v1/disputes/{dispute['id']}", headers=_auth(seller)).status_code == 200
    outsider = _login(api, OUTSIDER_TG)
    assert api.get(f"/api/v1/disputes/{dispute['id']}", headers=_auth(outsider)).status_code == 404
    assert api.get(f"/api/v1/disputes/{uuid.uuid4()}", headers=_auth(buyer)).status_code == 404


# ---------- evidence ----------


def _upload(
    api: TestClient,
    deal: dict[str, Any],
    who: dict[str, Any],
    data: bytes = PNG,
    content_type: str = "image/png",
    filename: str = "photo.png",
) -> Any:
    return api.post(
        f"/api/v1/deals/{deal['id']}/evidence/files",
        headers={
            **_auth(who),
            "Content-Type": content_type,
            "X-Filename": filename,
            "X-Evidence-Label": "photo",
        },
        content=data,
    )


def test_file_upload_is_stored_hashed_and_downloadable_as_an_attachment(
    api: TestClient, db_url: str, evidence_dir: Path
) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    uploaded = _upload(api, deal, buyer)
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert body["original_filename"] == "photo.png"

    download = api.get(f"/api/v1/evidence/{body['id']}/content", headers=_auth(seller))
    assert download.status_code == 200 and download.content == PNG
    assert download.headers["content-type"] == "image/png"
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["x-content-type-options"] == "nosniff"


def test_storage_never_uses_the_client_filename(api: TestClient, db_url: str, evidence_dir: Path) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    _upload(api, deal, buyer, filename="../../../../etc/passwd.png")
    stored = [p.name for p in evidence_dir.rglob("*") if p.is_file()]
    assert stored and all(len(name) == 32 and set(name) <= set("0123456789abcdef") for name in stored)
    assert not any("passwd" in name for name in stored)


def test_declared_type_must_match_the_real_bytes(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    html = b"<html><script>steal()</script></html>"
    response = _upload(api, deal, buyer, data=html, content_type="image/png")
    assert response.status_code == 422


def test_active_content_types_are_refused(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    assert _upload(api, deal, buyer, data=b"<svg></svg>", content_type="image/svg+xml").status_code == 422


def test_oversized_upload_is_refused_before_it_is_stored(
    api: TestClient, db_url: str, evidence_dir: Path
) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    big = PNG + b"x" * 6_000  # limit is 5 000 bytes in this test
    assert _upload(api, deal, buyer, data=big).status_code == 413
    assert not any(p.is_file() for p in evidence_dir.rglob("*")) if evidence_dir.exists() else True


def test_tampered_file_on_disk_is_not_served(api: TestClient, db_url: str, evidence_dir: Path) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    body = _upload(api, deal, buyer).json()
    [stored] = [p for p in evidence_dir.rglob("*") if p.is_file()]
    stored.write_bytes(b"\x89PNG\r\n\x1a\n" + b"tampered")
    response = api.get(f"/api/v1/evidence/{body['id']}/content", headers=_auth(seller))
    assert response.status_code == 422
    assert b"tampered" not in response.content


def test_outsider_cannot_list_or_download_evidence(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    body = _upload(api, deal, buyer).json()
    outsider = _login(api, OUTSIDER_TG)
    assert api.get(f"/api/v1/deals/{deal['id']}/evidence", headers=_auth(outsider)).status_code == 404
    assert api.get(f"/api/v1/evidence/{body['id']}/content", headers=_auth(outsider)).status_code == 404


def test_notes_are_recorded_and_listed(api: TestClient, db_url: str) -> None:
    deal, buyer, seller = _confirmed_deal(api, db_url)
    note = api.post(
        f"/api/v1/deals/{deal['id']}/evidence/notes",
        headers=_auth(seller),
        json={"text": "Kuryer 3-kuni javob bermadi."},
    )
    assert note.status_code == 201 and note.json()["kind"] == "note"
    listed = api.get(f"/api/v1/deals/{deal['id']}/evidence", headers=_auth(buyer)).json()["data"]
    assert [item["note_text"] for item in listed] == ["Kuryer 3-kuni javob bermadi."]


def test_message_reference_must_belong_to_the_same_deal(api: TestClient, db_url: str) -> None:
    deal, buyer, _ = _confirmed_deal(api, db_url)
    assert (
        api.post(
            f"/api/v1/deals/{deal['id']}/evidence/message-references",
            headers=_auth(buyer),
            json={"message_id": 999_999},
        ).status_code
        == 404
    )


def test_evidence_is_frozen_once_a_deal_closes_without_a_dispute(api: TestClient, db_url: str) -> None:
    buyer, seller = _login(api, BUYER_TG), _login(api, SELLER_TG)
    deal = api.post(
        "/api/v1/deals",
        headers={**_auth(buyer), "Idempotency-Key": f"f-{uuid.uuid4().hex}"},
        json={
            "my_role": "buyer",
            "counterparty_telegram_user_id": SELLER_TG,
            "title": "Y",
            "product_amount_tiyin": 100_000_000,
            "terms_text": "terms",
        },
    ).json()
    api.post(
        f"/api/v1/deals/{deal['id']}/cancel",
        headers={**_auth(buyer), "Idempotency-Key": f"x-{uuid.uuid4().hex}"},
        json={"expected_version": 1},
    )
    response = api.post(
        f"/api/v1/deals/{deal['id']}/evidence/notes", headers=_auth(seller), json={"text": "late"}
    )
    assert response.status_code == 409
