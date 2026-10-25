"""Grants the administrator role to an existing account. There is no HTTP route for this on purpose.

Usage (from backend/, after the person has signed in to MIDA at least once):
    python scripts/grant_admin.py --telegram-id 123456789
    python scripts/grant_admin.py --telegram-id 123456789 --revoke
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import create_engine_from_url, create_session_factory  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.user import User  # noqa: E402


async def _run(telegram_id: int, revoke: bool) -> int:
    engine = create_engine_from_url(get_settings().database_url)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            user = (
                await session.execute(select(User).where(User.telegram_user_id == telegram_id))
            ).scalar_one_or_none()
            if user is None:
                print("No account with that Telegram id. The person must sign in to MIDA first.")
                return 1
            user.role = UserRole.BUYER if revoke else UserRole.ADMIN
            await session.commit()
            print(f"Account {user.id} is now {user.role.value}.")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Grant or revoke the administrator role.")
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args.telegram_id, args.revoke))


if __name__ == "__main__":
    sys.exit(main())
