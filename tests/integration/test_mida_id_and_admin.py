from __future__ import annotations

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.enums import UserRole
from app.services.auth_service import AuthService
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data


def _settings(db_url: str, **extra: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
        **extra,  # type: ignore[arg-type]
    )


async def test_mida_id_is_random_eight_digits_not_sequential(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    settings = _settings(db_url)
    ids = []
    for tg_id in (9001, 9002, 9003, 9004):
        async with session_factory() as session:
            result = await AuthService(session, settings).authenticate(
                valid_init_data(telegram_user_id=tg_id)
            )
            ids.append(result.user.mida_id)
    assert all(i is not None and 10_000_000 <= i <= 99_999_999 for i in ids)
    assert len(set(ids)) == len(ids)
    assert not all(b - a == 1 for a, b in zip(ids, ids[1:], strict=False))  # type: ignore[operator]


async def test_listed_telegram_id_becomes_admin_others_stay_buyer(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    settings = _settings(db_url, admin_telegram_ids="9100, 9101")
    async with session_factory() as session:
        admin = await AuthService(session, settings).authenticate(valid_init_data(telegram_user_id=9100))
    async with session_factory() as session:
        buyer = await AuthService(session, settings).authenticate(valid_init_data(telegram_user_id=9200))
    assert admin.user.role is UserRole.ADMIN
    assert buyer.user.role is UserRole.BUYER


async def test_existing_user_is_promoted_when_added_to_admin_list(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    async with session_factory() as session:
        first = await AuthService(session, _settings(db_url)).authenticate(
            valid_init_data(telegram_user_id=9300)
        )
    assert first.user.role is UserRole.BUYER
    async with session_factory() as session:
        again = await AuthService(session, _settings(db_url, admin_telegram_ids="9300")).authenticate(
            valid_init_data(telegram_user_id=9300)
        )
    assert again.user.role is UserRole.ADMIN


def test_worker_starts_automatically_when_mtproto_is_configured() -> None:
    base = {"_env_file": None}
    off = Settings(**base)  # type: ignore[arg-type]
    assert off.worker_active is False
    on = Settings(
        **base,  # type: ignore[arg-type]
        telegram_api_id=123,
        telegram_api_hash=SecretStr("h"),
        telegram_admin_session_path="/tmp/x.session",  # noqa: S108  (test-only path)
    )
    assert on.worker_active is True
    forced_off = Settings(
        **base,  # type: ignore[arg-type]
        telegram_api_id=123,
        telegram_api_hash=SecretStr("h"),
        telegram_admin_session_path="/tmp/x.session",  # noqa: S108  (test-only path)
        telegram_worker_enabled=False,
    )
    assert forced_off.worker_active is False


async def test_photo_url_and_telegram_id_are_stored_and_returned(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    from app.schemas.user import UserPublic

    url = "https://t.me/i/userpic/320/abc.jpg"
    async with session_factory() as session:
        result = await AuthService(session, _settings(db_url)).authenticate(
            valid_init_data(telegram_user_id=9400, photo_url=url)
        )
        public = UserPublic.model_validate(result.user).model_dump()
    assert public["telegram_id"] == 9400
    assert public["photo_url"] == url
    assert len(str(public["mida_id"])) == 8


async def test_non_https_photo_url_is_dropped(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    async with session_factory() as session:
        result = await AuthService(session, _settings(db_url)).authenticate(
            valid_init_data(telegram_user_id=9500, photo_url="javascript:alert(1)")
        )
    assert result.user.photo_url is None
