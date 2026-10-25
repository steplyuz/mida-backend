"""Application settings loaded from environment variables and backend/.env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
SQLITE_URL_PREFIX = "sqlite+aiosqlite:///"
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "MIDA API"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: str = f"{SQLITE_URL_PREFIX}./mida.db"
    cors_origins: str = ""

    service_fee_bps: int = Field(default=1000, ge=0, le=10_000)
    max_deal_amount_uzs: int = Field(default=1_000_000_000, gt=0)

    telegram_bot_token: SecretStr | None = None
    telegram_initdata_max_age_seconds: int = Field(default=300, gt=0)

    app_secret_key: SecretStr | None = None
    access_token_ttl_seconds: int = Field(default=900, gt=0, le=86_400)

    mock_payment_provider: bool = False
    # Admin records bank transfers by hand; a second admin approves them.
    manual_payments: bool = False
    # MIDA's own receiving account, shown to buyers. Set in .env on the server, never in code.
    manual_payment_bank: str | None = Field(default=None, max_length=120)
    manual_payment_account: str | None = Field(default=None, max_length=40)
    manual_payment_recipient: str | None = Field(default=None, max_length=160)

    # MTProto admin session (group creation). The Bot API is NOT used for group creation.
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    telegram_admin_session_path: str | None = None
    telegram_invite_ttl_seconds: int = Field(default=86_400, gt=0, le=604_800)
    telegram_webhook_secret: SecretStr | None = None
    # Separate bot that works inside deal groups (rules, status, commands). Never the same token
    # as the notification bot, so each bot's webhook and permissions stay independent.
    group_bot_token: SecretStr | None = None
    group_bot_webhook_secret: SecretStr | None = None
    group_bot_username: str | None = None
    # Secret for the MOCK provider's HMAC signature. Real providers bring their own verification.
    mock_payment_webhook_secret: SecretStr | None = None
    mida_logo_path: str = "./assets/mida-logo.png"

    # Comma-separated Telegram user ids that are promoted to the admin role on login.
    admin_telegram_ids: str = ""

    # Optional Mini App URL: notifications get an "open" button that launches it.
    telegram_miniapp_url: str | None = None

    # None = automatic: the outbox worker runs whenever MTProto or the bot is configured.
    telegram_worker_enabled: bool | None = None
    telegram_worker_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    outbox_max_attempts: int = Field(default=8, ge=1, le=50)

    rate_limit_login_per_minute: int = Field(default=10, ge=1, le=10_000)
    rate_limit_deal_create_per_hour: int = Field(default=30, ge=1, le=100_000)
    rate_limit_payment_start_per_hour: int = Field(default=20, ge=1, le=100_000)
    rate_limit_inquiry_per_hour: int = Field(default=5, ge=1, le=100_000)

    evidence_storage_path: str = "./storage/evidence"
    evidence_max_bytes: int = Field(default=10_000_000, gt=0, le=50_000_000)

    @field_validator(
        "telegram_api_id",
        "telegram_admin_session_path",
        "app_secret_key",
        "telegram_bot_token",
        "telegram_api_hash",
        "telegram_webhook_secret",
        "group_bot_token",
        "group_bot_webhook_secret",
        "group_bot_username",
        "mock_payment_webhook_secret",
        "telegram_worker_enabled",
        "telegram_miniapp_url",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """A blank .env line (e.g. TELEGRAM_API_ID=) means not configured, not a parse error."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        """Resolve relative SQLite paths against backend/, not the process cwd."""
        if not value.startswith(SQLITE_URL_PREFIX):
            return value
        path_part = value[len(SQLITE_URL_PREFIX) :]
        if path_part == ":memory:":
            return value
        path = Path(path_part)
        if not path.is_absolute():
            path = BACKEND_DIR / path
        return f"{SQLITE_URL_PREFIX}{path}"

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        if self.app_env != "production":
            return self
        if self.debug:
            raise ValueError("DEBUG must be false when APP_ENV=production")
        if self.mock_payment_provider:
            raise ValueError("MOCK_PAYMENT_PROVIDER must be false when APP_ENV=production")
        if self.telegram_bot_token is None or not self.telegram_bot_token.get_secret_value():
            raise ValueError("TELEGRAM_BOT_TOKEN is required when APP_ENV=production")
        secret = self.app_secret_key.get_secret_value() if self.app_secret_key else ""
        if len(secret) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"APP_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters when APP_ENV=production"
            )
        return self

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _webhook_secret_strength(cls, value: SecretStr | None) -> SecretStr | None:
        # Telegram accepts 1-256 chars; we require a strong secret when one is configured.
        if value is not None and len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(f"TELEGRAM_WEBHOOK_SECRET must be at least {MIN_SECRET_KEY_LENGTH} characters")
        return value

    @field_validator("group_bot_webhook_secret")
    @classmethod
    def _group_bot_secret_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(f"GROUP_BOT_WEBHOOK_SECRET must be at least {MIN_SECRET_KEY_LENGTH} characters")
        return value

    @property
    def evidence_root(self) -> Path:
        path = Path(self.evidence_storage_path)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def logo_path(self) -> Path:
        path = Path(self.mida_logo_path)
        return path if path.is_absolute() else BACKEND_DIR / path

    @property
    def mtproto_configured(self) -> bool:
        return bool(
            self.telegram_api_id
            and self.telegram_api_hash
            and self.telegram_api_hash.get_secret_value()
            and self.telegram_admin_session_path
        )

    @property
    def worker_active(self) -> bool:
        if self.telegram_worker_enabled is not None:
            return self.telegram_worker_enabled
        # Bot notifications also need the worker, so a bot token alone is enough to start it.
        return self.mtproto_configured or self.bot_configured

    @property
    def bot_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_bot_token.get_secret_value())

    @property
    def admin_telegram_id_set(self) -> frozenset[int]:
        ids: set[int] = set()
        for part in self.admin_telegram_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return frozenset(ids)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
