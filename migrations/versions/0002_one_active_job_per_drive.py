"""Enforce one active ingest job per optical drive.

Revision ID: 0002_one_active_job_per_drive
Revises: 0001_postgresql_core
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_one_active_job_per_drive"
down_revision: str | None = "0001_postgresql_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY drive_id
                    ORDER BY
                        CASE status
                            WHEN 'ripping' THEN 1
                            WHEN 'publishing' THEN 2
                            WHEN 'queued' THEN 3
                            ELSE 4
                        END,
                        created_at
                ) AS position
            FROM jobs
            WHERE status IN ('scanning','queued','ripping','publishing')
        )
        UPDATE jobs
        SET
            status = 'cancelled',
            completed_at = CURRENT_TIMESTAMP,
            error = 'Superseded duplicate active job during migration'
        WHERE id IN (SELECT id FROM ranked WHERE position > 1)
        """
    )
    op.create_index(
        "jobs_one_active_per_drive_idx",
        "jobs",
        ["drive_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('scanning','queued','ripping','publishing')"),
        sqlite_where=sa.text("status IN ('scanning','queued','ripping','publishing')"),
    )


def downgrade() -> None:
    op.drop_index("jobs_one_active_per_drive_idx", table_name="jobs")
