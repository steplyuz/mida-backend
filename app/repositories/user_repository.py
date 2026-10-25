from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

# Public account number: 8 digits, random, never sequential. The first digit is never 0, so the
# number is always exactly 8 characters long and never shows up with leading zeros.
MIDA_ID_MIN = 10_000_000
MIDA_ID_MAX = 99_999_999
MIDA_ID_MAX_ATTEMPTS = 25


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user

    async def get(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
        return result.scalar_one_or_none()

    async def next_mida_id(self) -> int:
        """A random 8-digit number that no user holds yet.

        The unique index on users.mida_id is the real guarantee: if two logins pick the same
        number at the same moment, the second insert fails and the caller retries the login.
        """
        for _ in range(MIDA_ID_MAX_ATTEMPTS):
            candidate = MIDA_ID_MIN + secrets.randbelow(MIDA_ID_MAX - MIDA_ID_MIN + 1)
            taken = await self._session.scalar(select(User.id).where(User.mida_id == candidate))
            if taken is None:
                return candidate
        raise RuntimeError("Could not allocate a free MIDA ID.")
