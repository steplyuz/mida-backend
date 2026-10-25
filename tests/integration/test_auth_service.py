from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.user import User
from app.services.auth_service import AuthService
from tests.support.telegram import TEST_BOT_TOKEN, valid_init_data


def _settings(db_url: str) -> Settings:
    from pydantic import SecretStr

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=db_url,
        telegram_bot_token=SecretStr(TEST_BOT_TOKEN),
        app_secret_key=SecretStr("k" * 32),
    )


async def test_concurrent_first_logins_create_exactly_one_user(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    settings = _settings(db_url)
    init_data = valid_init_data(telegram_user_id=7777)

    async def login() -> int:
        async with session_factory() as session:
            result = await AuthService(session, settings).authenticate(init_data)
            return result.user.id

    ids = await asyncio.gather(*(login() for _ in range(5)))
    assert len(set(ids)) == 1

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(User).where(User.telegram_user_id == 7777)
        )
    assert count == 1


async def test_profile_fields_follow_the_latest_telegram_data(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    settings = _settings(db_url)
    async with session_factory() as session:
        await AuthService(session, settings).authenticate(
            valid_init_data(telegram_user_id=8888, first_name="Old")
        )
    async with session_factory() as session:
        await AuthService(session, settings).authenticate(
            valid_init_data(telegram_user_id=8888, first_name="New")
        )
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 8888))
        assert user is not None and user.first_name == "New"
