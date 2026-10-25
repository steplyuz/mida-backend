from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import BACKEND_DIR, Settings

PROD_SECRET = SecretStr("p" * 32)
PROD_TOKEN = SecretStr("123:prod-token")


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _production(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "production",
        "app_secret_key": PROD_SECRET,
        "telegram_bot_token": PROD_TOKEN,
    }
    base.update(overrides)
    return _settings(**base)


def test_defaults_are_safe() -> None:
    settings = _settings()
    assert settings.service_fee_bps == 1000
    assert settings.debug is False
    assert settings.mock_payment_provider is False
    assert settings.access_token_ttl_seconds == 900


def test_relative_sqlite_path_resolves_against_backend_dir() -> None:
    settings = _settings(database_url="sqlite+aiosqlite:///./data/x.db")
    assert settings.database_url == f"sqlite+aiosqlite:///{BACKEND_DIR / 'data' / 'x.db'}"


def test_absolute_sqlite_path_is_kept(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"
    assert _settings(database_url=url).database_url == url


def test_memory_database_is_kept() -> None:
    url = "sqlite+aiosqlite:///:memory:"
    assert _settings(database_url=url).database_url == url


def test_production_rejects_debug() -> None:
    with pytest.raises(ValidationError, match="DEBUG must be false"):
        _production(debug=True)


def test_production_rejects_mock_payment_provider() -> None:
    with pytest.raises(ValidationError, match="MOCK_PAYMENT_PROVIDER must be false"):
        _production(mock_payment_provider=True)


def test_production_requires_telegram_bot_token() -> None:
    with pytest.raises(ValidationError, match="TELEGRAM_BOT_TOKEN is required"):
        _settings(app_env="production", app_secret_key=PROD_SECRET)


def test_production_requires_strong_secret_key() -> None:
    with pytest.raises(ValidationError, match="APP_SECRET_KEY must be at least 32"):
        _production(app_secret_key=SecretStr("short"))
    with pytest.raises(ValidationError, match="APP_SECRET_KEY must be at least 32"):
        _production(app_secret_key=None)


def test_production_accepts_safe_config() -> None:
    assert _production().app_env == "production"


def test_secrets_are_not_printed() -> None:
    settings = _production()
    assert "p" * 32 not in repr(settings)
    assert "prod-token" not in repr(settings)


def test_development_allows_mock_payment_provider() -> None:
    assert _settings(app_env="development", mock_payment_provider=True).mock_payment_provider


@pytest.mark.parametrize("bps", [-1, 10_001])
def test_fee_bps_out_of_range_is_rejected(bps: int) -> None:
    with pytest.raises(ValidationError):
        _settings(service_fee_bps=bps)


def test_token_ttl_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _settings(access_token_ttl_seconds=0)
    with pytest.raises(ValidationError):
        _settings(access_token_ttl_seconds=86_401)


def test_cors_origins_are_parsed_and_trimmed() -> None:
    settings = _settings(cors_origins=" http://a.test , http://b.test ,, ")
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]
