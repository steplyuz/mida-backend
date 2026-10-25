"""End-to-end auth over HTTP, against a migrated SQLite file. No mocks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.deps import AdminUserDep, CurrentUserDep
from app.core.config import Settings
from app.main import create_app
from tests.support.telegram import TEST_BOT_TOKEN, make_fields, sign, valid_init_data

APP_SECRET = "k" * 32


def _sqlite_path(db_url: str) -> Path:
    return Path(db_url.removeprefix("sqlite+aiosqlite:///"))


def _settings(db_url: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "log_level": "WARNING",
        "database_url": db_url,
        "telegram_bot_token": SecretStr(TEST_BOT_TOKEN),
        "app_secret_key": SecretStr(APP_SECRET),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def api(db_url: str) -> Iterator[TestClient]:
    app = create_app(_settings(db_url))

    @app.get("/_whoami")
    def whoami(current: CurrentUserDep) -> dict[str, object]:
        return {"id": current.user.id, "role": current.user.role.value}

    @app.get("/_admin-only")
    def admin_only(current: AdminUserDep) -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        yield client


def _login(client: TestClient, init_data: str | None = None) -> dict[str, object]:
    response = client.post("/api/v1/auth/telegram", json={"init_data": init_data or valid_init_data()})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _set_db(db_url: str, sql: str, params: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(_sqlite_path(db_url)) as conn:
        conn.execute(sql, params)


# ---------- login ----------


def test_first_login_creates_buyer_and_returns_token(api: TestClient) -> None:
    body = _login(api, valid_init_data(telegram_user_id=5001, first_name="Dilshod"))
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert isinstance(body["access_token"], str) and body["access_token"].count(".") == 2
    user = body["user"]
    assert isinstance(user, dict)
    assert user["first_name"] == "Dilshod"
    assert user["role"] == "buyer"
    assert "telegram_user_id" not in user  # internal identity is not exposed


def test_repeated_login_reuses_the_same_user(api: TestClient) -> None:
    first = _login(api, valid_init_data(telegram_user_id=5002))
    second = _login(api, valid_init_data(telegram_user_id=5002))
    assert first["user"]["id"] == second["user"]["id"]  # type: ignore[index]


def test_invalid_signature_returns_401_problem_json(api: TestClient) -> None:
    init_data = valid_init_data().replace("Ali", "Eve")
    response = api.post("/api/v1/auth/telegram", json={"init_data": init_data})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["WWW-Authenticate"] == "Bearer"
    body = response.json()
    assert body["type"] == "urn:mida:error:authentication-failed"
    assert "hash" not in body["detail"]


def test_error_response_never_echoes_init_data(api: TestClient) -> None:
    init_data = valid_init_data()
    secret_hash = init_data.split("hash=")[1]
    response = api.post("/api/v1/auth/telegram", json={"init_data": init_data.replace("Ali", "Eve")})
    assert secret_hash not in response.text


def test_expired_init_data_returns_401(api: TestClient) -> None:
    import time

    stale = sign(make_fields(auth_date=int(time.time()) - 3600))
    response = api.post("/api/v1/auth/telegram", json={"init_data": stale})
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_client_cannot_inject_identity_fields(api: TestClient) -> None:
    response = api.post(
        "/api/v1/auth/telegram",
        json={"init_data": valid_init_data(), "telegram_user_id": 999, "role": "admin"},
    )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:mida:error:validation-error"


def test_disabled_account_cannot_log_in(api: TestClient, db_url: str) -> None:
    _login(api, valid_init_data(telegram_user_id=5003))
    _set_db(db_url, "UPDATE users SET is_active = 0 WHERE telegram_user_id = 5003")
    response = api.post("/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=5003)})
    assert response.status_code == 403
    assert response.json()["type"] == "urn:mida:error:account-disabled"


def test_login_without_configured_bot_token_fails_closed(db_url: str) -> None:
    app = create_app(_settings(db_url, telegram_bot_token=None))
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/telegram", json={"init_data": valid_init_data()})
    assert response.status_code == 503
    assert response.json()["type"] == "urn:mida:error:service-not-configured"


# ---------- bearer authentication ----------


def test_me_requires_a_token(api: TestClient) -> None:
    response = api.get("/api/v1/users/me")
    assert response.status_code == 401


@pytest.mark.parametrize("header", ["Bearer garbage", "Bearer a.b.c", "Basic dXNlcjpwYXNz", "Bearer "])
def test_malformed_authorization_is_rejected(api: TestClient, header: str) -> None:
    response = api.get("/api/v1/users/me", headers={"Authorization": header})
    assert response.status_code == 401


def test_me_returns_the_profile(api: TestClient) -> None:
    token = _login(api, valid_init_data(telegram_user_id=5010, first_name="Malika"))["access_token"]
    response = api.get("/api/v1/users/me", headers=_bearer(str(token)))
    assert response.status_code == 200
    assert response.json()["first_name"] == "Malika"


def test_deactivation_takes_effect_before_token_expiry(api: TestClient, db_url: str) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5011))["access_token"])
    assert api.get("/api/v1/users/me", headers=_bearer(token)).status_code == 200

    _set_db(db_url, "UPDATE users SET is_active = 0 WHERE telegram_user_id = 5011")

    response = api.get("/api/v1/users/me", headers=_bearer(token))
    assert response.status_code == 403
    assert response.json()["type"] == "urn:mida:error:account-disabled"


def test_role_change_takes_effect_without_new_token(api: TestClient, db_url: str) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5012))["access_token"])
    assert api.get("/_admin-only", headers=_bearer(token)).status_code == 403

    _set_db(db_url, "UPDATE users SET role = 'admin' WHERE telegram_user_id = 5012")
    assert api.get("/_admin-only", headers=_bearer(token)).status_code == 200


def test_token_for_a_removed_user_is_rejected(api: TestClient, db_url: str) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5013))["access_token"])
    _set_db(db_url, "DELETE FROM users WHERE telegram_user_id = 5013")
    assert api.get("/api/v1/users/me", headers=_bearer(token)).status_code == 401


# ---------- authorization ----------


def test_buyer_is_forbidden_from_admin_routes(api: TestClient) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5020))["access_token"])
    response = api.get("/_admin-only", headers=_bearer(token))
    assert response.status_code == 403
    assert response.json()["type"] == "urn:mida:error:permission-denied"


def test_admin_can_reach_admin_routes(api: TestClient, db_url: str) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5021))["access_token"])
    _set_db(db_url, "UPDATE users SET role = 'admin' WHERE telegram_user_id = 5021")
    assert api.get("/_admin-only", headers=_bearer(token)).status_code == 200


def test_profile_update_allows_only_self_editable_fields(api: TestClient) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5030))["access_token"])
    ok = api.patch("/api/v1/users/me", headers=_bearer(token), json={"phone": "+998901234567"})
    assert ok.status_code == 200
    assert ok.json()["phone"] == "+998901234567"


def test_profile_update_cannot_escalate_role(api: TestClient, db_url: str) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5031))["access_token"])
    response = api.patch("/api/v1/users/me", headers=_bearer(token), json={"role": "admin"})
    assert response.status_code == 422
    assert api.get("/_admin-only", headers=_bearer(token)).status_code == 403


def test_profile_update_rejects_malformed_phone(api: TestClient) -> None:
    token = str(_login(api, valid_init_data(telegram_user_id=5032))["access_token"])
    response = api.patch("/api/v1/users/me", headers=_bearer(token), json={"phone": "call me"})
    assert response.status_code == 422
