from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_checks_database(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


def test_ready_reports_unavailable_without_leaking_details(tmp_path: Path) -> None:
    broken = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'missing_dir' / 'nope.db'}",
    )
    with TestClient(create_app(broken)) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error"}
    assert "unable" not in response.text.lower()
    assert str(tmp_path) not in response.text


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["X-Request-ID"]
    assert len(response.headers["X-Request-ID"]) == 32


def test_valid_incoming_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-abc.123"})
    assert response.headers["X-Request-ID"] == "trace-abc.123"


def test_invalid_incoming_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "bad!id"})
    assert response.headers["X-Request-ID"] != "bad!id"


def test_unknown_route_returns_problem_json(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_unhandled_exception_returns_generic_500(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/_boom")
    def boom() -> None:
        raise RuntimeError("secret-internal-detail-xyz")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_boom")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "secret-internal-detail-xyz" not in response.text
    assert "Traceback" not in response.text
    assert response.json()["type"] == "urn:mida:error:internal-error"


def test_validation_error_is_problem_json_without_echoing_input(settings: Settings) -> None:
    app: FastAPI = create_app(settings)

    @app.get("/_items")
    def items(limit: int = Query(...)) -> dict[str, int]:
        return {"limit": limit}

    with TestClient(app) as client:
        response = client.get("/_items", params={"limit": "not-a-number-token"})

    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "urn:mida:error:validation-error"
    assert body["errors"][0]["field"] == "limit"
    assert "not-a-number-token" not in response.text
