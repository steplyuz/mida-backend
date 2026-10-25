"""user mida_id (public account number, does not start from 1)

Revision ID: a1d2e3f4b5c6
Revises: 909de8a70809
Create Date: 2026-10-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1d2e3f4b5c6"
down_revision: str | None = "909de8a70809"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIDA_ID_START = 100_000


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("mida_id", sa.BigInteger(), nullable=True))
    op.execute(f"UPDATE users SET mida_id = id + {MIDA_ID_START}")
    with op.batch_alter_table("users") as batch:
        batch.create_unique_constraint(op.f("uq_users_mida_id"), ["mida_id"])
    op.execute(
        "INSERT INTO counters (name, value) "
        f"VALUES ('mida_id', COALESCE((SELECT MAX(id) FROM users), 0) + {MIDA_ID_START})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM counters WHERE name = 'mida_id'")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint(op.f("uq_users_mida_id"), type_="unique")
        batch.drop_column("mida_id")
