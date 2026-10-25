"""Access policy as a test: every API operation is explicitly public or requires a token.

The inventory comes from the OpenAPI schema, so a new endpoint cannot silently ship unprotected: it
appears here and fails until someone decides, in writing, that it is public.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data

# Public on purpose. Each one authenticates in another way (signature, shared secret) or is a probe.
PUBLIC_OPERATIONS = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("POST", "/api/v1/auth/telegram"),  # authenticated by Telegram's signed initData
    ("POST", "/api/v1/telegram/webhook"),  # authenticated by the shared webhook secret
    ("POST", "/api/v1/telegram/group-bot/webhook"),  # authenticated by the group bot's secret
    ("POST", "/api/v1/payments/webhook/{provider}"),  # authenticated by the provider signature
    ("POST", "/api/v1/auth/telegram-widget"),  # authenticated by the Login Widget signature; admins only
    ("POST", "/api/v1/inquiries"),  # public contact form: rate-limited per address, honeypot, no reads
}
ADMIN_ONLY = {
    ("GET", "/api/v1/admin/outbox"),
    ("POST", "/api/v1/admin/outbox/{event_id}/retry"),
    ("GET", "/api/v1/admin/integrations/telegram"),
    ("GET", "/api/v1/admin/payments/reconciliation"),
    ("POST", "/api/v1/disputes/{dispute_id}/assign"),
    ("POST", "/api/v1/disputes/{dispute_id}/resolve"),
    ("POST", "/api/v1/deals/{public_id}/telegram-group/retry"),
}
FAKE_UUID = "00000000-0000-4000-8000-000000000000"


def _operations(app: Any) -> list[tuple[str, str]]:
    ops: list[tuple[str, str]] = []
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            ops.append((method.upper(), path))
    return sorted(ops)


def _fill(path: str) -> str:
    return re.sub(
        r"\{([a-z_]+)\}",
        lambda m: "1" if m.group(1) in ("event_id", "message_id") else FAKE_UUID,
        path,
    )


@pytest.fixture
def app(db_url: str) -> Any:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
    )
    return create_app(settings)


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_the_public_surface_is_exactly_the_reviewed_list(app: Any) -> None:
    spec = app.openapi()["paths"]
    public = {
        (method.upper(), path)
        for path, methods in spec.items()
        for method, op in methods.items()
        if not op.get("security")
    }
    assert public == PUBLIC_OPERATIONS, f"unreviewed public operations: {public ^ PUBLIC_OPERATIONS}"


def test_every_non_public_operation_refuses_anonymous_callers(client: TestClient, app: Any) -> None:
    failures = []
    for method, path in _operations(app):
        if (method, path) in PUBLIC_OPERATIONS:
            continue
        response = client.request(method, _fill(path), json={})
        if response.status_code != 401:
            failures.append((method, path, response.status_code))
    assert failures == [], f"reachable without a token: {failures}"


def test_admin_operations_refuse_ordinary_members(client: TestClient, db_url: str) -> None:
    body = client.post(
        "/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=9500)}
    ).json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    failures = []
    for method, path in sorted(ADMIN_ONLY):
        response = client.request(
            method,
            _fill(path),
            headers=headers,
            json={
                "expected_version": 1,
                "event_id": 1,
                "resolution": "buyer_wins",
                "reason": "x" * 30,
            },
        )
        if response.status_code != 403:
            failures.append((method, path, response.status_code))
    assert failures == [], f"admin operations open to members: {failures}"


def test_responses_carry_the_baseline_security_headers(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


def test_error_bodies_never_contain_tracebacks(client: TestClient) -> None:
    for response in (
        client.get("/nope"),
        client.get("/api/v1/deals/x"),
        client.post("/api/v1/auth/telegram", json={}),
    ):
        assert "Traceback" not in response.text and 'File "' not in response.text


# ---------- rate limits ----------


def test_login_is_rate_limited_per_address(app: Any, client: TestClient) -> None:
    app.state.settings.rate_limit_login_per_minute = 3
    statuses = [
        client.post(
            "/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=9600 + n)}
        ).status_code
        for n in range(4)
    ]
    assert statuses[:3] == [200, 200, 200]
    limited = client.post("/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=9699)})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["type"] == "urn:mida:error:rate-limited"


def test_counterparty_probing_is_slowed_per_user(app: Any, client: TestClient) -> None:
    app.state.settings.rate_limit_deal_create_per_hour = 2
    me = client.post(
        "/api/v1/auth/telegram", json={"init_data": valid_init_data(telegram_user_id=9701)}
    ).json()
    headers = {"Authorization": f"Bearer {me['access_token']}", "Idempotency-Key": "probe-key-0001"}
    codes = []
    for guess in (111_111, 222_222, 333_333):
        response = client.post(
            "/api/v1/deals",
            headers={**headers, "Idempotency-Key": f"probe-{guess}"},
            json={
                "my_role": "buyer",
                "counterparty_telegram_user_id": guess,
                "title": "x",
                "product_amount_tiyin": 100,
                "terms_text": "t",
            },
        )
        codes.append(response.status_code)
    assert codes[:2] == [422, 422]  # unknown counterparty: the normal refusal
    assert codes[2] == 429  # the third probe is throttled


def test_rate_limit_window_resets_and_memory_stays_bounded() -> None:
    from app.core.domain_errors import DomainError
    from app.core.ratelimit import RateLimited, SlidingWindowLimiter

    now = [0.0]
    limiter = SlidingWindowLimiter(clock=lambda: now[0])
    limiter.check("k", limit=2, window_seconds=60)
    limiter.check("k", limit=2, window_seconds=60)
    with pytest.raises(RateLimited) as blocked:
        limiter.check("k", limit=2, window_seconds=60)
    assert isinstance(blocked.value, DomainError) and 1 <= blocked.value.retry_after <= 60
    now[0] = 61.0
    limiter.check("k", limit=2, window_seconds=60)  # window has moved on

    for n in range(50_010):
        limiter.check(f"key-{n}", limit=5, window_seconds=60)
    assert len(limiter._hits) <= 50_010  # noqa: SLF001 - the bound is what we are testing


# ---------- migrations and backups ----------


def test_schema_matches_models_and_has_one_head(db_url: str) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command
    from app.core.config import BACKEND_DIR

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["sqlalchemy_url"] = db_url
    command.upgrade(config, "head")
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
    command.check(config)  # raises if any model change has no migration


def test_backup_is_written_and_verified(tmp_path: Path) -> None:
    import sqlite3

    from scripts.backup_db import backup

    source = tmp_path / "mida.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
    copy = backup(source, tmp_path / "backups")
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == (42,)


def test_backup_of_a_missing_database_fails_loudly(tmp_path: Path) -> None:
    from scripts.backup_db import main

    assert main(["--source", str(tmp_path / "nope.db"), "--dest", str(tmp_path)]) == 1


# ---------- retention ----------


async def test_retention_removes_only_what_it_is_allowed_to_remove(
    session_factory: Any, pricing: Any, make_user: Any
) -> None:
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.core.time import utcnow
    from app.models.enums import OutboxStatus
    from app.models.idempotency import IdempotencyRecord
    from app.models.outbox import OutboxEvent
    from app.services.retention import purge_completed_outbox_events, purge_idempotency_records

    user = await make_user(9800)
    old = utcnow() - timedelta(days=30)
    async with session_factory() as session:
        session.add(
            IdempotencyRecord(
                user_id=user,
                operation="deal.create",
                idempotency_key="old-key-00001",
                request_hash="h",
                resource_type="deal",
                resource_id=1,
                created_at=old,
            )
        )
        session.add(
            IdempotencyRecord(
                user_id=user,
                operation="deal.create",
                idempotency_key="new-key-00002",
                request_hash="h",
                resource_type="deal",
                resource_id=2,
                created_at=utcnow(),
            )
        )
        for status, name in ((OutboxStatus.DONE, "done"), (OutboxStatus.FAILED, "failed")):
            session.add(
                OutboxEvent(
                    event_type="x",
                    aggregate_id=1,
                    dedupe_key=f"ret:{name}",
                    payload_json="{}",
                    status=status,
                    attempt_count=1,
                    next_attempt_at=old,
                    created_at=old,
                    processed_at=old,
                )
            )
        await session.commit()

    async with session_factory() as session:
        removed_keys = await purge_idempotency_records(session, older_than=utcnow() - timedelta(days=1))
        removed_events = await purge_completed_outbox_events(session, older_than=utcnow() - timedelta(days=1))
    assert (removed_keys, removed_events) == (1, 1)

    async with session_factory() as session:
        keys = await session.scalar(select(func.count()).select_from(IdempotencyRecord))
        failed = (
            await session.execute(select(OutboxEvent.status).where(OutboxEvent.dedupe_key == "ret:failed"))
        ).scalar_one()
    assert keys == 1
    assert failed is OutboxStatus.FAILED  # failed work is kept until an administrator has seen it
