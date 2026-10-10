from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from contextlib import aclosing, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ValidationError

from . import __version__
from .arr import PROVIDERS, ArrClient, ArrConfig, ArrError
from .config import Settings
from .db import Database
from .handbrake import list_presets
from .integrations import handoff_state
from .makemkv import (
    DriveInfo,
    MakeMKVBackend,
    MakeMKVError,
    RipperBackend,
    SimulationBackend,
)
from .output import validate_template
from .service import RipperService


class IntegrationUpdate(BaseModel):
    enabled: bool = False
    url: str = ""
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    local_path: str = ""
    remote_path: str = ""


class ImportFile(BaseModel):
    title_id: int = Field(gt=0)
    episode_ids: list[int] = Field(default_factory=list, max_length=50)


class HandoffRequest(BaseModel):
    provider: str = Field(pattern="^(radarr|sonarr)$")
    target_id: int = Field(gt=0)
    files: list[ImportFile] = Field(min_length=1, max_length=100)


def persist_settings(settings: Settings) -> None:
    path = settings.preferences_path
    path.parent.mkdir(parents=True, exist_ok=True)
    persisted = settings.public_preferences()
    persisted.update(
        movie_root=str(settings.movie_root),
        tv_root=str(settings.tv_root),
        integrations=settings.integrations,
    )
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as output:
        temporary = Path(output.name)
        json.dump(persisted, output, indent=2)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class MetadataUpdate(BaseModel):
    media_type: str = Field(pattern="^(movie|tv)$")
    title: str = Field(min_length=1, max_length=180)
    year: int | None = Field(default=None, ge=1888, le=2200)
    season: int | None = Field(default=None, ge=0, le=999)
    episode_start: int | None = Field(default=None, ge=0, le=9999)
    edition: str = Field(default="", max_length=120)
    selected_title_ids: list[int] | None = None


class StartRequest(BaseModel):
    media_type: str = Field(default="auto", pattern="^(auto|movie|tv)$")


class PreferencesUpdate(BaseModel):
    library_root: str = Field(min_length=1, max_length=1000)
    auto_rip: bool
    eject_on_success: bool
    output_mode: str = Field(pattern="^(disc|library)$")
    rip_mode: str = Field(pattern="^(smart|main_feature|all)$")
    max_concurrent_rips: int | None = Field(default=None, ge=1, le=16)
    movie_root: str | None = Field(default=None, min_length=1, max_length=1000)
    tv_root: str | None = Field(default=None, min_length=1, max_length=1000)
    drive_paths: dict[str, str] | None = None
    disc_folder_template: str | None = None
    movie_name_template: str | None = None
    tv_name_template: str | None = None
    include_extras: bool | None = None
    review_before_rip: bool | None = None
    extras_min_seconds: int | None = Field(default=None, ge=1, le=36000)
    min_title_seconds: int | None = Field(default=None, ge=0, le=36000)
    subtitle_mode: str | None = Field(default=None, pattern="^(all|preferred|none)$")
    audio_mode: str | None = Field(default=None, pattern="^(all|preferred)$")
    preferred_languages: str | None = Field(default=None, pattern=r"^[a-z]{3}(,[a-z]{3})*$")
    artwork_enabled: bool | None = None
    handbrake_enabled: bool | None = None
    handbrake_bin: str | None = Field(default=None, min_length=1, max_length=1000)
    handbrake_preset: str | None = Field(default=None, min_length=1, max_length=120)
    handbrake_keep_original: bool | None = None


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
            else MakeMKVBackend(settings.makemkv_bin, settings.sdf_path, settings)
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

    def integration_config(provider: str, payload: IntegrationUpdate | None = None) -> ArrConfig:
        if provider not in PROVIDERS:
            raise HTTPException(404, "Unknown integration")
        values = dict(settings.integrations.get(provider, {}))
        if payload:
            # Never send a saved key to a newly entered server URL.
            changed_server = payload.url.strip().rstrip("/") != values.get("url", "")
            values.update(payload.model_dump(exclude={"api_key", "clear_api_key"}))
            if payload.clear_api_key or changed_server:
                values["api_key"] = ""
            if payload.api_key and payload.api_key.get_secret_value():
                values["api_key"] = payload.api_key.get_secret_value()
        try:
            return ArrConfig(**values)
        except ValidationError as exc:
            message = "; ".join(e["msg"] for e in exc.errors(include_input=False))
            raise HTTPException(422, message) from None

    @app.get("/api/integrations")
    async def get_integrations():
        return {provider: integration_config(provider).public() for provider in PROVIDERS}

    @app.put("/api/integrations/{provider}")
    async def save_integration(provider: str, payload: IntegrationUpdate):
        config = integration_config(provider, payload)
        async with service._poll_lock:
            if service.handoffs.tasks:
                raise HTTPException(
                    409, "Wait for active naming tasks to finish before changing connections"
                )
            values = {**settings.integrations, provider: config.private()}
            try:
                persist_settings(replace(settings, integrations=values))
            except OSError:
                raise HTTPException(422, "Could not save integration settings") from None
            settings.integrations = values
        return config.public()

    @app.post("/api/integrations/{provider}/test")
    async def test_integration(provider: str, payload: IntegrationUpdate):
        try:
            return await ArrClient(provider, integration_config(provider, payload)).test()
        except ArrError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/integrations/{provider}/library")
    async def integration_library(provider: str, query: str = ""):
        integration_config(provider)
        try:
            items = await service.handoffs.client(provider).library(query)
            return [
                {"id": m["id"], "title": m["title"], "year": m.get("year")} for m in items[:100]
            ]
        except ArrError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/integrations/sonarr/episodes/{series_id}")
    async def integration_episodes(series_id: int):
        try:
            items = await service.handoffs.client("sonarr").request(
                "GET", "episode", params={"seriesId": series_id}
            )
            return [
                {k: item.get(k) for k in ("id", "title", "seasonNumber", "episodeNumber")}
                for item in items
            ]
        except ArrError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/jobs/{job_id}/integration", status_code=202)
    async def start_handoff(job_id: str, payload: HandoffRequest):
        try:
            await service.handoffs.start(
                job_id, payload.provider, payload.target_id, [f.model_dump() for f in payload.files]
            )
        except ArrError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"message": "Naming handoff started"}

    @app.post("/api/jobs/{job_id}/integration/check", status_code=202)
    async def check_handoff(job_id: str):
        try:
            await service.handoffs.check(job_id)
        except ArrError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"message": "Checking the existing naming task"}

    @app.get("/api/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {
            "tmdb_configured": bool(settings.tmdb_token),
            "handbrake_available": bool(shutil.which(settings.handbrake_bin)),
            "live_settings": True,
        }

    @app.get("/api/handbrake/presets")
    async def handbrake_presets():
        try:
            return await list_presets(settings.handbrake_bin)
        except (MakeMKVError, TimeoutError):
            raise HTTPException(
                422, "Could not load HandBrake presets. Check the executable path and try again"
            ) from None

    @app.post("/api/jobs/failed/clear")
    async def clear_failed():
        count = database.execute("UPDATE jobs SET status='dismissed' WHERE status='failed'")
        database.add_event(f"Cleared {count} failed entries; partial files retained")
        await service.broadcast()
        return {"cleared": count}

    @app.post("/api/jobs/{job_id}/dismiss")
    async def dismiss_failed(job_id: str):
        job = database.job_detail(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] != "failed":
            raise HTTPException(409, "Only failed rips can be cleared")
        database.execute(
            "UPDATE jobs SET status='dismissed' WHERE id=? AND status='failed'", (job_id,)
        )
        database.add_event("Failed entry cleared; partial files retained", job_id=job_id)
        await service.broadcast()
        return {"cleared": 1}

    @app.put("/api/settings")
    async def save_settings(payload: PreferencesUpdate) -> dict[str, Any]:
        async with service._poll_lock:
            busy = database.fetchone(
                "SELECT id FROM jobs WHERE status IN "
                "('scanning','queued','ripping','publishing','needs_review') LIMIT 1"
            )
            values = payload.model_dump(exclude_none=True)
            protected = set(values) - {"max_concurrent_rips", "auto_rip"}
            current = settings.public_preferences()
            if busy and any(values[name] != current[name] for name in protected):
                raise HTTPException(
                    409,
                    "While jobs are active, only automatic ripping and concurrent rips can change",
                )
            root = Path(payload.library_root.strip()).expanduser()
            if not root.is_absolute():
                raise HTTPException(422, "Use an absolute destination path on the ripper host")
            root = root.resolve()
            values["library_root"] = root
            for name in ("movie_root", "tv_root"):
                if name in values:
                    candidate = Path(values[name]).expanduser()
                    if not candidate.is_absolute():
                        raise HTTPException(422, f"{name} must be an absolute path")
                    values[name] = candidate.resolve()
            if "drive_paths" in values:
                if len(values["drive_paths"]) > 32:
                    raise HTTPException(422, "At most 32 drive overrides are supported")
                for path_value in values["drive_paths"].values():
                    if not Path(path_value).expanduser().is_absolute():
                        raise HTTPException(422, "Drive destinations must be absolute paths")
                values["drive_paths"] = {
                    key: str(Path(value).expanduser().resolve())
                    for key, value in values["drive_paths"].items()
                }
            try:
                for name in ("disc_folder_template", "movie_name_template", "tv_name_template"):
                    if name in values:
                        validate_template(values[name], unique=name == "disc_folder_template")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            updated = replace(settings, **values)
            root_changed = root != settings.library_root.resolve()
            if root_changed:
                if payload.movie_root is None:
                    updated.movie_root = root / "Movies"
                if payload.tv_root is None:
                    updated.tv_root = root / "TV"
            if updated.handbrake_enabled and not shutil.which(updated.handbrake_bin):
                raise HTTPException(
                    422, "HandBrakeCLI was not found. Install it separately or fix its path"
                )
            if updated.artwork_enabled and not settings.tmdb_token:
                raise HTTPException(422, "Artwork needs OPENRIPPER_TMDB_TOKEN on the server")
            try:
                if not busy:
                    destinations = {root, *(Path(p) for p in updated.drive_paths.values())}
                    destinations.update((updated.movie_root, updated.tv_root))
                    for destination in destinations:
                        destination.mkdir(parents=True, exist_ok=True)
                        with tempfile.TemporaryFile(dir=destination):
                            pass
                    updated.staging_root.mkdir(parents=True, exist_ok=True)
                persist_settings(updated)
            except OSError as exc:
                raise HTTPException(422, f"Could not save destination/settings: {exc}") from exc
            for field in (*values, "movie_root", "tv_root"):
                setattr(settings, field, getattr(updated, field))
            await service._semaphore.set_limit(settings.max_concurrent_rips)
            database.add_event("Ripping settings saved")
        await service.broadcast()
        return settings.public_preferences()

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return service.overview()

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str) -> dict[str, Any]:
        job = database.job_detail(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        job["telemetry"] = service._telemetry.get(job_id, {})
        job["integration"] = handoff_state(job)
        job["naming_supported"] = True
        if job["stage_path"]:
            job["destination_folder"] = str(service.destination_folder(job))
        return job

    @app.post("/api/jobs/{job_id}/metadata")
    async def update_metadata(job_id: str, payload: MetadataUpdate) -> dict[str, Any]:
        if not database.job_detail(job_id):
            raise HTTPException(404, "Job not found")
        try:
            return await service.update_metadata(job_id, **payload.model_dump())
        except (ValueError, FileExistsError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/artwork")
    async def artwork(job_id: str) -> FileResponse:
        job = database.job_detail(job_id)
        if not job or not job["final_path"]:
            raise HTTPException(404, "No saved artwork")
        path = Path(job["final_path"]) / "poster.jpg"
        if not path.is_file():
            raise HTTPException(404, "No saved artwork")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str) -> dict[str, str]:
        try:
            await service.retry_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "queued"}

    @app.post("/api/jobs/{job_id}/start-now", status_code=202)
    async def start_queued_now(job_id: str) -> dict[str, str]:
        try:
            await service.start_queued_now(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job_id": job_id}

    @app.post("/api/jobs/{job_id}/start", status_code=202)
    async def start_pending(job_id: str, payload: StartRequest):
        try:
            await service.start_pending(job_id, payload.media_type)
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"job_id": job_id}

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
        job_id = service.queue_drive(drive)
        pending = database.job_detail(job_id)
        if pending and pending["status"] == "awaiting_start":
            await service.start_pending(job_id, "auto")
        return {"job_id": job_id}

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

        async def send_updates() -> None:
            async with aclosing(service.subscribe()) as snapshots:
                async for snapshot in snapshots:
                    await websocket.send_json(websocket_payload(snapshot))

        async def wait_for_disconnect() -> None:
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(send_updates()), asyncio.create_task(wait_for_disconnect())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            return
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app
