"""mida_id becomes an 8-digit number (10000001 ... 99999999)

Revision ID: d4e5f6a7b8c9
Revises: a1d2e3f4b5c6
Create Date: 2026-10-24

"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "a1d2e3f4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHIFT = 9_900_000  # 100001 -> 10000001
EIGHT_DIGIT_MIN = 10_000_000


def upgrade() -> None:
    op.execute(f"UPDATE users SET mida_id = mida_id + {SHIFT} WHERE mida_id < {EIGHT_DIGIT_MIN}")
    op.execute(
        f"UPDATE counters SET value = value + {SHIFT} "
        f"WHERE name = 'mida_id' AND value < {EIGHT_DIGIT_MIN}"
    )


def downgrade() -> None:
    op.execute(f"UPDATE users SET mida_id = mida_id - {SHIFT} WHERE mida_id >= {EIGHT_DIGIT_MIN}")
    op.execute(
        f"UPDATE counters SET value = value - {SHIFT} "
        f"WHERE name = 'mida_id' AND value >= {EIGHT_DIGIT_MIN}"
    )
