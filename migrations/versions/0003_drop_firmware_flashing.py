"""Drop firmware flashing state; keep a simple compatibility summary.

Revision ID: 0003_drop_firmware_flashing
Revises: 0002_one_active_job_per_drive
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_drop_firmware_flashing"
down_revision: str | None = "0002_one_active_job_per_drive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DROPPED = (
    "firmware_platform",
    "firmware_date",
    "firmware_type",
    "flash_candidate",
    "flash_profile",
)


def upgrade() -> None:
    with op.batch_alter_table("drives") as batch:
        for column in DROPPED:
            batch.drop_column(column)
    # Old flash-era classifications are re-derived on the next check.
    op.execute("UPDATE drives SET uhd_status = 'unknown', firmware_message = ''")


def downgrade() -> None:
    with op.batch_alter_table("drives") as batch:
        batch.add_column(
            sa.Column("firmware_platform", sa.String(80), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("firmware_date", sa.String(80), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("firmware_type", sa.String(160), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("flash_candidate", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("flash_profile", sa.String(120), nullable=False, server_default="")
        )
