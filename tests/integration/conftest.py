"""Integration fixtures: a real SQLite file migrated with Alembic, per test. No mocks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alembic import command
from app.core.config import BACKEND_DIR
from app.db.session import create_engine_from_url, create_session_factory
from app.models.enums import UserRole
from app.models.user import User
from app.services.pricing_service import PricingService


@pytest.fixture
def db_url(tmp_path: Path) -> Iterator[str]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["sqlalchemy_url"] = url
    command.upgrade(config, "head")
    yield url


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine_from_url(db_url)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
def pricing() -> PricingService:
    return PricingService(service_fee_bps=1000, max_amount_uzs=1_000_000_000)


MakeUser = Callable[..., Awaitable[int]]


@pytest.fixture
def make_user(session_factory: async_sessionmaker[AsyncSession]) -> MakeUser:
    async def _make(telegram_user_id: int, *, active: bool = True) -> int:
        async with session_factory() as session:
            user = User(
                telegram_user_id=telegram_user_id,
                first_name=f"User{telegram_user_id}",
                role=UserRole.BUYER,
                is_active=active,
            )
            session.add(user)
            await session.commit()
            return user.id

    return _make
