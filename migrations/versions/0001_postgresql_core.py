"""Create PostgreSQL core schema.

Revision ID: 0001_postgresql_core
Revises:
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_postgresql_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "drives",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("disc_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("device", sa.String(255), nullable=False, server_default=""),
        sa.Column("disc_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("state", sa.String(32), nullable=False, server_default="empty"),
        sa.Column("status_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "discovery_source",
            sa.String(32),
            nullable=False,
            server_default="makemkv",
        ),
        sa.Column("online", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("firmware_platform", sa.String(80), nullable=False, server_default=""),
        sa.Column("firmware_version", sa.String(80), nullable=False, server_default=""),
        sa.Column("firmware_date", sa.String(80), nullable=False, server_default=""),
        sa.Column("firmware_type", sa.String(160), nullable=False, server_default=""),
        sa.Column("libredrive_status", sa.String(80), nullable=False, server_default=""),
        sa.Column("uhd_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("firmware_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("flash_candidate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("flash_profile", sa.String(120), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "drive_id",
            sa.String(80),
            sa.ForeignKey("drives.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("disc_name", sa.String(255), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("media_type", sa.String(24), nullable=False, server_default="movie"),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("year", sa.Integer()),
        sa.Column("season", sa.Integer()),
        sa.Column("episode_start", sa.Integer()),
        sa.Column("edition", sa.String(160), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("metadata_confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("stage_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("final_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("jobs_created_idx", "jobs", ["created_at"])
    op.create_index("jobs_drive_status_idx", "jobs", ["drive_id", "status"])
    op.create_index("jobs_fingerprint_idx", "jobs", ["fingerprint"])
    op.create_table(
        "titles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            sa.String(64),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("chapters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("playlist", sa.String(160), nullable=False, server_default=""),
        sa.Column("source_filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ripped_path", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index(
        "titles_job_title_idx",
        "titles",
        ["job_id", "title_index"],
        unique=True,
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            sa.String(64),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
        ),
        sa.Column("level", sa.String(24), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("events_job_idx", "events", ["job_id", "id"])


def downgrade() -> None:
    op.drop_index("events_job_idx", table_name="events")
    op.drop_table("events")
    op.drop_index("titles_job_title_idx", table_name="titles")
    op.drop_table("titles")
    op.drop_index("jobs_fingerprint_idx", table_name="jobs")
    op.drop_index("jobs_drive_status_idx", table_name="jobs")
    op.drop_index("jobs_created_idx", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("drives")
