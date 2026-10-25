"""Administrators see every deal in the list and can open any deal; members see only their own."""

# Fixtures come from the admin tests on purpose; the redefinition warning is expected.
# ruff: noqa: F811

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.integration.test_admin_ops import (  # noqa: F401  (fixture and helpers)
    ADMIN_A,
    BLOCKED,
    MEMBER,
    _admin,
    _deal,
    _h,
    _login,
    api,
)


def test_admin_list_contains_deals_of_other_people(api: TestClient, db_url: str) -> None:
    buyer = _login(api, MEMBER)
    stranger = _login(api, BLOCKED)
    admin = _admin(api, db_url, ADMIN_A)
    deal = _deal(api, buyer)
    ids = [d["id"] for d in api.get("/api/v1/deals?limit=50", headers=_h(admin)).json()["data"]]
    assert deal["id"] in ids
    assert all(d["deal_number"] for d in api.get("/api/v1/deals?limit=50", headers=_h(admin)).json()["data"])
    # The admin can also open that deal and read its thread (archive page).
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_h(admin)).status_code == 200
    assert api.get(f"/api/v1/deals/{deal['id']}/messages", headers=_h(admin)).status_code == 200
    # A member who is not a party sees nothing of it.
    assert deal["id"] not in [
        d["id"] for d in api.get("/api/v1/deals?limit=50", headers=_h(stranger)).json()["data"]
    ]
    assert api.get(f"/api/v1/deals/{deal['id']}", headers=_h(stranger)).status_code == 404
