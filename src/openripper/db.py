from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

ACTIVE_JOB_STATUS_SQL = "status IN ('scanning','queued','ripping','publishing')"


def now_iso() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Drive(Base):
    __tablename__ = "drives"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    disc_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    device: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    disc_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="empty")
    status_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    discovery_source: Mapped[str] = mapped_column(String(32), nullable=False, default="makemkv")
    online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    firmware_version: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    libredrive_status: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    uhd_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    firmware_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("jobs_created_idx", "created_at"),
        Index("jobs_drive_status_idx", "drive_id", "status"),
        Index("jobs_fingerprint_idx", "fingerprint"),
        Index(
            "jobs_one_active_per_drive_idx",
            "drive_id",
            unique=True,
            postgresql_where=text(ACTIVE_JOB_STATUS_SQL),
            sqlite_where=text(ACTIVE_JOB_STATUS_SQL),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    drive_id: Mapped[str] = mapped_column(
        ForeignKey("drives.id", ondelete="RESTRICT"), nullable=False
    )
    disc_name: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[str] = mapped_column(String(24), nullable=False, default="movie")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    season: Mapped[int | None] = mapped_column(Integer)
    episode_start: Mapped[int | None] = mapped_column(Integer)
    edition: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    metadata_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    stage_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    final_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Title(Base):
    __tablename__ = "titles"
    __table_args__ = (Index("titles_job_title_idx", "job_id", "title_index", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    title_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    chapters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    playlist: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ripped_path: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("events_job_idx", "job_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _model_dict(instance: Base) -> dict[str, Any]:
    return {column.name: getattr(instance, column.name) for column in instance.__table__.columns}


def _parameterize(sql: str, params: tuple[Any, ...]) -> tuple[str, dict[str, Any]]:
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise ValueError("SQL placeholder count does not match parameters")
    rendered = parts[0]
    bindings: dict[str, Any] = {}
    for index, value in enumerate(params):
        name = f"p{index}"
        rendered += f":{name}{parts[index + 1]}"
        bindings[name] = value
    return rendered, bindings


class Database:
    def __init__(self, url: str):
        self.url = url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def ping(self) -> bool:
        with self.engine.connect() as connection:
            return bool(connection.execute(text("SELECT 1")).scalar_one())

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        rendered, bindings = _parameterize(sql, params)
        with self.engine.begin() as connection:
            result = connection.execute(text(rendered), bindings)
            return int(result.rowcount or 0)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rendered, bindings = _parameterize(sql, params)
        with self.engine.connect() as connection:
            row = connection.execute(text(rendered), bindings).mappings().first()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        rendered, bindings = _parameterize(sql, params)
        with self.engine.connect() as connection:
            return [
                dict(row) for row in connection.execute(text(rendered), bindings).mappings().all()
            ]

    def upsert_drive(self, drive: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(Drive, drive["id"]) or Drive(
                id=drive["id"],
                disc_index=drive["disc_index"],
                name=drive["name"],
                updated_at=now_iso(),
            )
            for field in (
                "disc_index",
                "name",
                "device",
                "disc_name",
                "state",
                "status_text",
                "discovery_source",
                "online",
                "firmware_version",
                "libredrive_status",
                "uhd_status",
                "firmware_message",
            ):
                if field in drive:
                    setattr(row, field, drive[field])
            row.updated_at = now_iso()
            session.add(row)

    def mark_missing_drives_offline(self, present_ids: set[str]) -> None:
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(select(Drive).where(Drive.online.is_(True))).all()
            for row in rows:
                if row.id not in present_ids:
                    row.online = False
                    row.state = "offline"
                    row.status_text = "Drive disconnected"
                    row.disc_name = ""
                    row.updated_at = now_iso()

    def update_drive(self, drive_id: str, **fields: Any) -> None:
        allowed = {
            "state",
            "status_text",
            "firmware_version",
            "libredrive_status",
            "uhd_status",
            "firmware_message",
        }
        with Session(self.engine) as session, session.begin():
            drive = session.get(Drive, drive_id)
            if not drive:
                return
            for key, value in fields.items():
                if key in allowed:
                    setattr(drive, key, value)
            drive.updated_at = now_iso()

    def create_job(self, job: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(
                Job(
                    id=job["id"],
                    drive_id=job["drive_id"],
                    disc_name=job["disc_name"],
                    fingerprint=job["fingerprint"],
                    media_type=job.get("media_type", "movie"),
                    title=job.get("title", ""),
                    year=job.get("year"),
                    status=job["status"],
                    metadata_confidence=job.get("metadata_confidence", 0),
                    stage_path=job.get("stage_path", ""),
                    created_at=now_iso(),
                )
            )

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "fingerprint",
            "media_type",
            "title",
            "year",
            "season",
            "episode_start",
            "edition",
            "status",
            "progress",
            "metadata_confidence",
            "stage_path",
            "final_path",
            "error",
            "started_at",
            "completed_at",
        }
        with Session(self.engine) as session, session.begin():
            job = session.get(Job, job_id)
            if not job:
                return
            for key, value in fields.items():
                if key in allowed:
                    setattr(job, key, value)

    def replace_titles(self, job_id: str, titles: list[dict[str, Any]]) -> None:
        with Session(self.engine) as session, session.begin():
            for row in session.scalars(select(Title).where(Title.job_id == job_id)):
                session.delete(row)
            session.flush()
            session.add_all(
                [
                    Title(
                        job_id=job_id,
                        title_index=title["index"],
                        name=title.get("name", ""),
                        duration_seconds=title.get("duration_seconds", 0),
                        size_bytes=title.get("size_bytes", 0),
                        chapters=title.get("chapters", 0),
                        playlist=title.get("playlist", ""),
                        source_filename=title.get("source_filename", ""),
                        selected=bool(title.get("selected", False)),
                    )
                    for title in titles
                ]
            )

    def set_selected_titles(self, job_id: str, title_ids: list[int]) -> None:
        selected = set(title_ids)
        with Session(self.engine) as session, session.begin():
            rows = session.scalars(select(Title).where(Title.job_id == job_id)).all()
            for row in rows:
                row.selected = row.id in selected

    def job_detail(self, job_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            job = session.get(Job, job_id)
            if not job:
                return None
            result = _model_dict(job)
            result["titles"] = [
                _model_dict(row)
                for row in session.scalars(
                    select(Title)
                    .where(Title.job_id == job_id)
                    .order_by(Title.duration_seconds.desc())
                ).all()
            ]
            result["events"] = [
                _model_dict(row)
                for row in session.scalars(
                    select(Event).where(Event.job_id == job_id).order_by(Event.id.desc()).limit(100)
                ).all()
            ]
            return result

    def add_event(
        self,
        message: str,
        *,
        job_id: str | None = None,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> None:
        with Session(self.engine) as session, session.begin():
            session.add(
                Event(
                    job_id=job_id,
                    level=level,
                    message=message,
                    details=details or {},
                    created_at=now_iso(),
                )
            )

    def overview(self) -> dict[str, Any]:
        with Session(self.engine) as session:
            drives = [
                _model_dict(row)
                for row in session.scalars(
                    select(Drive).order_by(Drive.online.desc(), Drive.disc_index)
                ).all()
            ]
            active_statuses = [
                "scanning",
                "queued",
                "ripping",
                "publishing",
                "needs_review",
            ]
            active = [
                _model_dict(row)
                for row in session.scalars(
                    select(Job)
                    .where(Job.status.in_(active_statuses))
                    .order_by(Job.created_at.desc())
                ).all()
            ]
            history = [
                _model_dict(row)
                for row in session.scalars(
                    select(Job)
                    .where(Job.status.not_in(active_statuses[:-1]))
                    .order_by(Job.created_at.desc())
                    .limit(50)
                ).all()
            ]
            all_jobs = session.query(Job).count()
            completed = session.query(Job).filter(Job.status == "complete").count()
            failed = session.query(Job).filter(Job.status == "failed").count()
        return {
            "drives": drives,
            "active_jobs": active,
            "history": history,
            "totals": {
                "all_jobs": all_jobs,
                "completed": completed,
                "failed": failed,
            },
        }
