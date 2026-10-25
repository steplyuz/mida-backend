"""Existing users get random 8-digit MIDA IDs (replaces the sequential numbers).

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-24

Downgrade is a no-op: the sequential numbers are not recoverable and nothing depends on them.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIDA_ID_MIN = 10_000_000
MIDA_ID_MAX = 99_999_999


def upgrade() -> None:
    bind = op.get_bind()
    user_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM users ORDER BY id"))]
    if not user_ids:
        return
    # Step 1: move every value out of the range so the unique index never sees a clash.
    bind.execute(sa.text("UPDATE users SET mida_id = -id"))
    # Step 2: hand out distinct random numbers.
    taken: set[int] = set()
    for user_id in user_ids:
        while True:
            candidate = MIDA_ID_MIN + secrets.randbelow(MIDA_ID_MAX - MIDA_ID_MIN + 1)
            if candidate not in taken:
                break
        taken.add(candidate)
        bind.execute(
            sa.text("UPDATE users SET mida_id = :mida WHERE id = :id"),
            {"mida": candidate, "id": user_id},
        )


def downgrade() -> None:
    pass
