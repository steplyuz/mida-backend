"""Runs the real Alembic CLI against a temporary SQLite database."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import BACKEND_DIR


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}", "APP_ENV": "test"}
    return subprocess.run(  # noqa: S603  (fixed argv, no user input)
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_upgrade_head_on_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "migrate.db"
    result = _run_alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    with sqlite3.connect(db_path) as conn:
        versions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    head = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()
    assert versions == [(head,)]


def test_downgrade_to_base_and_back(tmp_path: Path) -> None:
    db_path = tmp_path / "roundtrip.db"
    assert _run_alembic(db_path, "upgrade", "head").returncode == 0
    assert _run_alembic(db_path, "downgrade", "base").returncode == 0
    assert _run_alembic(db_path, "upgrade", "head").returncode == 0
