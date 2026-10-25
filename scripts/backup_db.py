"""Consistent SQLite backup, safe while the server is running (uses the online backup API).

Usage (Windows PowerShell, from backend/):
    python scripts/backup_db.py --source mida.db --dest backups
The copy is verified with PRAGMA integrity_check before the script reports success.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path


def backup(source: Path, dest_dir: Path) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"{source.stem}-{stamp}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    with sqlite3.connect(target) as check:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    if verdict != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity check: {verdict}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the MIDA SQLite database.")
    parser.add_argument("--source", type=Path, default=Path("mida.db"))
    parser.add_argument("--dest", type=Path, default=Path("backups"))
    args = parser.parse_args(argv)
    try:
        path = backup(args.source, args.dest)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 1
    print(f"backup written and verified: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
