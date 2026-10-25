from pathlib import Path

from sqlalchemy import text

from app.db.session import create_engine_from_url, create_session_factory


async def test_sqlite_foreign_keys_are_enforced(tmp_path: Path) -> None:
    engine = create_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            result = await session.execute(text("PRAGMA foreign_keys"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


async def test_each_session_gets_a_connection_with_pragmas(tmp_path: Path) -> None:
    engine = create_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'multi.db'}")
    try:
        factory = create_session_factory(engine)
        async with factory() as first, factory() as second:
            for session in (first, second):
                result = await session.execute(text("PRAGMA foreign_keys"))
                assert result.scalar_one() == 1
    finally:
        await engine.dispose()
