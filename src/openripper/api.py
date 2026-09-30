from __future__ import annotations

import json
import tempfile
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .db import Database
from .makemkv import (
    DriveInfo,
    MakeMKVBackend,
    RipperBackend,
    SimulationBackend,
)
from .service import RipperService


class MetadataUpdate(BaseModel):
    media_type: str = Field(pattern="^(movie|tv)$")
    title: str = Field(min_length=1, max_length=180)
    year: int | None = Field(default=None, ge=1888, le=2200)
    season: int | None = Field(default=None, ge=0, le=999)
    episode_start: int | None = Field(default=None, ge=0, le=9999)
    edition: str = Field(default="", max_length=120)
    selected_title_ids: list[int] | None = None


class PreferencesUpdate(BaseModel):
    library_root: str = Field(min_length=1, max_length=1000)
    auto_rip: bool
    eject_on_success: bool
    output_mode: str = Field(pattern="^(disc|library)$")
    rip_mode: str = Field(pattern="^(smart|main_feature|all)$")


def websocket_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return jsonable_encoder(snapshot)


def create_app(
    settings: Settings | None = None,
    backend: RipperBackend | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_url)
    if backend is None:
        backend = (
            SimulationBackend()
            if settings.simulate
            else MakeMKVBackend(settings.makemkv_bin, settings.sdf_path)
        )
    service = RipperService(settings, database, backend)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="OpenRipper",
        description="Self-hosted automatic DVD and Blu-ray ripping to your own storage",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.service = service

    static_root = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static_root), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_root / "index.html")

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "simulation": settings.simulate,
            "auto_rip": settings.auto_rip,
            "output_mode": settings.output_mode,
            "library_root": str(settings.library_root),
            "movie_root": str(settings.movie_root),
            "tv_root": str(settings.tv_root),
            "database": "postgresql"
            if settings.database_url.startswith("postgresql")
            else "sqlite",
            "database_ready": database.ping(),
            "discovery": "udev+makemkv" if settings.udev_discovery else "makemkv-polling",
        }

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return settings.public_preferences()

    @app.put("/api/settings")
    async def save_settings(payload: PreferencesUpdate) -> dict[str, Any]:
        async with service._poll_lock:
            busy = database.fetchone(
                "SELECT id FROM jobs WHERE status IN "
                "('scanning','queued','ripping','publishing') LIMIT 1"
            )
            if busy:
                raise HTTPException(
                    409, "Wait for the current rip to finish before changing settings"
                )
            root = Path(payload.library_root.strip()).expanduser()
            if not root.is_absolute():
                raise HTTPException(422, "Use an absolute destination path on the ripper host")
            root = root.resolve()
            values = payload.model_dump()
            values["library_root"] = root
            updated = replace(settings, **values)
            root_changed = root != settings.library_root.resolve()
            if root_changed:
                updated.movie_root = root / "Movies"
                updated.tv_root = root / "TV"
            try:
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=root):
                    pass
                updated.staging_root.mkdir(parents=True, exist_ok=True)
                path = settings.preferences_path
                path.parent.mkdir(parents=True, exist_ok=True)
                persisted = updated.public_preferences()
                persisted.update(movie_root=str(updated.movie_root), tv_root=str(updated.tv_root))
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
                ) as output:
                    temporary = Path(output.name)
                    json.dump(persisted, output, indent=2)
                try:
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(422, f"Could not save destination/settings: {exc}") from exc
            for field in (*values, "movie_root", "tv_root"):
                setattr(settings, field, getattr(updated, field))
            database.add_event("Ripping settings saved")
        return settings.public_preferences()

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return database.overview()

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str) -> dict[str, Any]:
        job = database.job_detail(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    @app.post("/api/jobs/{job_id}/metadata")
    async def update_metadata(job_id: str, payload: MetadataUpdate) -> dict[str, Any]:
        if not database.job_detail(job_id):
            raise HTTPException(404, "Job not found")
        try:
            return await service.update_metadata(job_id, **payload.model_dump())
        except (ValueError, FileExistsError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str) -> dict[str, str]:
        try:
            await service.retry_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @app.post("/api/jobs/{job_id}/cancel", status_code=202)
    async def cancel(job_id: str) -> dict[str, str]:
        try:
            await service.cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "cancelling"}

    @app.post("/api/drives/{drive_id}/rip", status_code=202)
    async def rip_drive(drive_id: str) -> dict[str, str]:
        row = database.fetchone("SELECT * FROM drives WHERE id=?", (drive_id,))
        if not row:
            raise HTTPException(404, "Drive not found")
        if not row["disc_name"]:
            raise HTTPException(409, "There is no disc in this drive")
        if not row["online"]:
            raise HTTPException(409, "This drive is disconnected")
        drive = DriveInfo(
            id=row["id"],
            disc_index=row["disc_index"],
            name=row["name"],
            device=row["device"],
            disc_name=row["disc_name"],
            state=row["state"],
            status_text=row["status_text"],
        )
        return {"job_id": service.queue_drive(drive)}

    @app.post("/api/drives/{drive_id}/eject", status_code=202)
    async def eject_drive(drive_id: str) -> dict[str, str]:
        try:
            await service.eject_drive(drive_id)
        except KeyError as exc:
            raise HTTPException(404, "Drive not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "tray_opened"}

    @app.post("/api/drives/{drive_id}/compatibility")
    async def check_compatibility(drive_id: str) -> dict[str, Any]:
        try:
            return await service.check_compatibility(drive_id)
        except KeyError as exc:
            raise HTTPException(404, "Drive not found") from exc

    @app.post("/api/poll", status_code=202)
    async def poll_now(
        _: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, str]:
        await service.poll_once()
        return {"status": "complete"}

    @app.websocket("/api/ws")
    async def websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            async for snapshot in service.subscribe():
                await websocket.send_json(websocket_payload(snapshot))
        except WebSocketDisconnect:
            return

    return app
