"""Manual group link: an administrator attaches a group they created by hand."""

# pytest fixtures are imported from the admin tests on purpose; the redefinition warning is expected.
# ruff: noqa: F811

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.integration.test_admin_ops import (  # noqa: F401  (fixture and helpers)
    ADMIN_A,
    MEMBER,
    _admin,
    _deal,
    _h,
    _login,
    api,
)

GROUP_A = -1001234567890
GROUP_B = -1009876543210


def _link(
    api: TestClient, admin: dict[str, Any], public_id: str, chat_id: int, title: str = "MIDA: Velosiped"
) -> Any:
    return api.post(
        f"/api/v1/admin/deals/{public_id}/telegram-group/link",
        headers=_h(admin),
        json={"chat_id": chat_id, "title": title},
    )


def test_admin_attaches_a_group_and_the_deal_shows_it_created(api: TestClient, db_url: str) -> None:
    buyer = _login(api, MEMBER)
    a = _admin(api, db_url, ADMIN_A)
    deal = _deal(api, buyer)
    r = _link(api, a, deal["id"], GROUP_A)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["telegram_group_status"] == "created" and body["telegram_chat_id"] == GROUP_A
    tg = api.get(f"/api/v1/deals/{deal['id']}/telegram-group", headers=_h(buyer)).json()
    assert tg["status"] == "created"


def test_relinking_the_same_group_is_harmless(api: TestClient, db_url: str) -> None:
    buyer = _login(api, MEMBER)
    a = _admin(api, db_url, ADMIN_A)
    deal = _deal(api, buyer)
    assert _link(api, a, deal["id"], GROUP_A).status_code == 200
    assert _link(api, a, deal["id"], GROUP_A).status_code == 200


def test_one_group_cannot_serve_two_deals(api: TestClient, db_url: str) -> None:
    buyer = _login(api, MEMBER)
    a = _admin(api, db_url, ADMIN_A)
    first = _deal(api, buyer)
    second = _deal(api, buyer)
    assert _link(api, a, first["id"], GROUP_A).status_code == 200
    assert _link(api, a, second["id"], GROUP_A).status_code == 409


def test_a_deal_cannot_be_moved_to_a_different_group(api: TestClient, db_url: str) -> None:
    buyer = _login(api, MEMBER)
    a = _admin(api, db_url, ADMIN_A)
    deal = _deal(api, buyer)
    assert _link(api, a, deal["id"], GROUP_A).status_code == 200
    assert _link(api, a, deal["id"], GROUP_B).status_code == 409


def test_only_supergroup_ids_are_accepted(api: TestClient, db_url: str) -> None:
    buyer = _login(api, MEMBER)
    a = _admin(api, db_url, ADMIN_A)
    deal = _deal(api, buyer)
    assert _link(api, a, deal["id"], 12345).status_code == 422  # positive: a user, not a group
    assert _link(api, a, deal["id"], -42).status_code == 422  # a small basic-group id


def test_members_cannot_link_groups(api: TestClient, db_url: str) -> None:
    buyer = _login(api, MEMBER)
    deal = _deal(api, buyer)
    assert _link(api, buyer, deal["id"], GROUP_A).status_code == 403
