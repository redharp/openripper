from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, now_iso
from .discovery import optical_hotplug_events, optical_media_labels
from .makemkv import (
    DriveInfo,
    MakeMKVError,
    RipperBackend,
    TitleInfo,
    choose_titles,
    fingerprint_disc,
)
from .metadata import MetadataResolver
from .naming import clean_component, movie_destination, tv_destination

ACTIVE_STATUSES = {"scanning", "queued", "ripping", "publishing"}
TERMINAL_STATUSES = {"complete", "failed", "cancelled"}


def stage_bytes(stage: Path) -> int:
    total = 0
    for candidate in stage.glob("*.mkv"):
        with suppress(OSError):
            total += candidate.stat().st_size
    return total


class RipperService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        backend: RipperBackend,
    ):
        self.settings = settings
        self.database = database
        self.backend = backend
        self.metadata = MetadataResolver(settings.tmdb_token)
        self._watcher: asyncio.Task[None] | None = None
        self._udev_watcher: asyncio.Task[None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._seen_drives: set[str] = set()
        self._media_labels: dict[str, str] = {}
        self._compatibility_checked: set[str] = set()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_rips)
        self._poll_lock = asyncio.Lock()
        self._drive_locks: dict[str, asyncio.Lock] = {}

    def _drive_lock(self, drive_id: str) -> asyncio.Lock:
        return self._drive_locks.setdefault(drive_id, asyncio.Lock())

    def _track_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[job_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(job_id) is completed:
                self._tasks.pop(job_id, None)

        task.add_done_callback(forget)

    async def start(self) -> None:
        self.database.initialize()
        recovered = self.database.execute(
            """
            UPDATE jobs
            SET status='failed', error='Interrupted by service restart', completed_at=?
            WHERE status IN ('scanning','queued','ripping','publishing')
            """,
            (now_iso(),),
        )
        if recovered:
            self.database.add_event(
                f"Recovered {recovered} interrupted ingest job{'s' if recovered != 1 else ''}",
                level="warning",
            )
        self.settings.library_root.mkdir(parents=True, exist_ok=True)
        if self.settings.output_mode == "library":
            self.settings.movie_root.mkdir(parents=True, exist_ok=True)
            self.settings.tv_root.mkdir(parents=True, exist_ok=True)
        self.settings.staging_root.mkdir(parents=True, exist_ok=True)
        self._watcher = asyncio.create_task(self._watch_loop(), name="disc-watcher")
        if self.settings.udev_discovery:
            self._udev_watcher = asyncio.create_task(self._udev_loop(), name="udev-optical-watcher")

    async def stop(self) -> None:
        if self._watcher:
            self._watcher.cancel()
        tasks = [
            task for task in [self._watcher, self._udev_watcher, *self._tasks.values()] if task
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # watcher must survive hardware/tool failures
                self.database.add_event(
                    f"Drive scan failed: {exc}", level="error", details={"type": type(exc).__name__}
                )
                await self.broadcast()
            await asyncio.sleep(self.settings.poll_interval)

    async def _udev_loop(self) -> None:
        async for event in optical_hotplug_events():
            self.database.add_event(
                "Optical hardware changed",
                details={"source": "udev", "event": event},
            )
            await self.poll_once()

    async def poll_once(self) -> None:
        async with self._poll_lock:
            drives = await self.backend.list_drives()
            # MakeMKV's --noscan listing omits loaded discs, so ask the OS. Windows
            # has no udev, so its label lookup always runs.
            media_labels = (
                await asyncio.to_thread(optical_media_labels) if not self.settings.simulate else {}
            )
            for drive in drives:
                if not drive.disc_name and media_labels.get(drive.device):
                    drive.disc_name = media_labels[drive.device]
                    drive.state = "ready"
                    drive.status_text = "Disc ready"
            present_ids = {drive.id for drive in drives}
            self._seen_drives.intersection_update(present_ids)
            self._compatibility_checked.intersection_update(present_ids)
            self.database.mark_missing_drives_offline(present_ids)
            for drive in drives:
                active_job = self.database.fetchone(
                    """
                    SELECT id, status, progress FROM jobs
                    WHERE drive_id=? AND status IN ('scanning','queued','ripping','publishing')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (drive.id,),
                )
                if active_job:
                    drive.state = active_job["status"]
                    drive.status_text = active_job["status"].replace("_", " ").title()
                data = drive.to_dict()
                data["discovery_source"] = (
                    "udev+makemkv" if self.settings.udev_discovery else "makemkv"
                )
                data["online"] = True
                self.database.upsert_drive(data)
                if active_job:
                    continue
                if self._media_labels.get(drive.id) != drive.disc_name:
                    self._seen_drives.discard(drive.id)
                self._media_labels[drive.id] = drive.disc_name
                if drive.disc_name and self.settings.auto_rip and drive.id not in self._seen_drives:
                    self.queue_drive(drive)
                    continue
                if (
                    self.settings.compatibility_check
                    and drive.id not in self._compatibility_checked
                ):
                    await self.check_compatibility(drive.id, drive=drive)
                if not drive.disc_name:
                    self._seen_drives.discard(drive.id)
                    continue
            await self.broadcast()

    async def check_compatibility(
        self, drive_id: str, *, drive: DriveInfo | None = None
    ) -> dict[str, Any]:
        """Read-only MakeMKV drive info, summarized as what the drive can rip."""
        if drive is None:
            row = self.database.fetchone("SELECT * FROM drives WHERE id=?", (drive_id,))
            if not row:
                raise KeyError(drive_id)
            drive = DriveInfo(
                id=row["id"],
                disc_index=row["disc_index"],
                name=row["name"],
                device=row["device"],
                disc_name=row["disc_name"],
                state=row["state"],
                status_text=row["status_text"],
            )
        try:
            info = await self.backend.compatibility_info(drive.device)
            self.database.update_drive(
                drive_id,
                firmware_version=info.firmware_version or info.revision,
                libredrive_status=info.libredrive_status,
                uhd_status=info.status,
                firmware_message=info.message,
            )
            self._compatibility_checked.add(drive_id)
        except Exception as exc:
            self.database.update_drive(
                drive_id,
                uhd_status="check_failed",
                firmware_message=f"Couldn't read drive details: {exc}",
            )
            self.database.add_event(
                f"Compatibility check failed: {exc}",
                level="error",
                details={"drive_id": drive_id},
            )
        await self.broadcast()
        return self.database.fetchone("SELECT * FROM drives WHERE id=?", (drive_id,)) or {}

    def queue_drive(self, drive: DriveInfo) -> str:
        self._seen_drives.add(drive.id)
        self._media_labels[drive.id] = drive.disc_name
        active = self.database.fetchone(
            """
            SELECT id FROM jobs
            WHERE drive_id=? AND status IN ('scanning','queued','ripping','publishing')
            ORDER BY created_at DESC LIMIT 1
            """,
            (drive.id,),
        )
        if active:
            self._seen_drives.add(drive.id)
            return str(active["id"])
        job_id = uuid.uuid4().hex
        provisional = f"{drive.id}:{drive.disc_name}"
        self.database.create_job(
            {
                "id": job_id,
                "drive_id": drive.id,
                "disc_name": drive.disc_name,
                "fingerprint": provisional,
                "title": drive.disc_name,
                "status": "scanning",
                "stage_path": str(self.settings.staging_root / job_id),
            }
        )
        self.database.add_event(
            f"Detected {drive.disc_name}", job_id=job_id, details={"drive": drive.name}
        )
        task = asyncio.create_task(self._run_job(job_id, drive), name=f"rip-{job_id}")
        self._track_task(job_id, task)
        return job_id

    async def _run_job(self, job_id: str, drive: DriveInfo) -> None:
        async with self._drive_lock(drive.id), self._semaphore:
            await self._process_job(job_id, drive)

    async def _process_job(self, job_id: str, drive: DriveInfo) -> None:
        try:
            titles = await self.backend.scan_disc(drive.device)
            if not titles:
                raise MakeMKVError("MakeMKV found no usable titles on the disc")
            fingerprint = fingerprint_disc(drive.disc_name, titles)
            duplicate = self.database.fetchone(
                """
                SELECT id, status, created_at FROM jobs
                WHERE fingerprint=? AND id<>? AND status='complete'
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint, job_id),
            )
            self.database.update_job(job_id, fingerprint=fingerprint)
            if duplicate and self.settings.output_mode == "library":
                self.database.update_job(
                    job_id,
                    status="needs_review",
                    error=f"Possible duplicate of job {duplicate['id'][:8]}",
                )
                self.database.add_event(
                    "This disc appears to have been completed before",
                    job_id=job_id,
                    level="warning",
                )
                await self.broadcast()
                return

            selected = choose_titles(
                titles,
                min_seconds=self.settings.min_title_seconds,
                mode=self.settings.rip_mode,
            )
            if not selected:
                raise MakeMKVError("No titles passed the configured selection rules")
            self.database.replace_titles(job_id, [title.to_dict() for title in titles])

            resolver = (
                self.metadata if self.settings.output_mode == "library" else MetadataResolver()
            )
            match = await resolver.resolve(drive.disc_name)
            self.database.update_job(
                job_id,
                media_type=match.media_type,
                title=match.title,
                year=match.year,
                metadata_confidence=match.confidence,
                status="queued",
            )
            self.database.add_event(
                f"Selected {len(selected)} title{'s' if len(selected) != 1 else ''}",
                job_id=job_id,
                details={
                    "metadata_source": match.source,
                    "confidence": match.confidence,
                },
            )
            await self.broadcast()
            await self._rip(job_id, drive, selected)
        except asyncio.CancelledError:
            self.database.update_job(
                job_id,
                status="cancelled",
                error="Cancelled",
                completed_at=now_iso(),
            )
            self.database.add_event("Job cancelled", job_id=job_id, level="warning")
            await self.broadcast()
            raise
        except Exception as exc:
            self.database.update_job(
                job_id,
                status="failed",
                error=str(exc),
                completed_at=now_iso(),
            )
            self.database.add_event(str(exc), job_id=job_id, level="error")
            await self.broadcast()

    async def _rip(self, job_id: str, drive: DriveInfo, selected_titles: list[TitleInfo]) -> None:
        current = self.database.job_detail(job_id)
        stage = Path(current["stage_path"]) if current else self.settings.staging_root / job_id
        stage.mkdir(parents=True, exist_ok=True)
        self.database.update_job(
            job_id,
            status="ripping",
            progress=0,
            started_at=now_iso(),
            stage_path=str(stage),
        )
        await self.broadcast()
        for offset, title in enumerate(selected_titles):
            reported_value = 0.0

            async def report(value: float, message: str, *, current: int = offset) -> None:
                nonlocal reported_value
                measured = max(0.0, min(1.0, value))
                if measured <= reported_value:
                    return
                reported_value = measured
                overall = (current + reported_value) / len(selected_titles)
                self.database.update_job(job_id, progress=round(overall, 4))
                await self.broadcast()

            baseline_bytes = stage_bytes(stage)
            rip_task = asyncio.create_task(
                self.backend.rip_title(drive.device, title.index, stage, report),
                name=f"makemkv-{job_id}-{title.index}",
            )
            try:
                while not rip_task.done():
                    await asyncio.wait({rip_task}, timeout=2)
                    if title.size_bytes > 0:
                        written = max(0, stage_bytes(stage) - baseline_bytes)
                        await report(min(0.995, written / title.size_bytes), "Ripping title")
                output = await rip_task
            finally:
                if not rip_task.done():
                    rip_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await rip_task
            await report(1.0, "Finished title")
            self.database.execute(
                "UPDATE titles SET ripped_path=? WHERE job_id=? AND title_index=?",
                (str(output), job_id, title.index),
            )
            self.database.add_event(
                f"Finished title {title.index}",
                job_id=job_id,
                details={"file": output.name},
            )
        self.database.update_job(job_id, progress=1)
        job = self.database.job_detail(job_id)
        assert job
        can_publish = (
            bool(job["title"])
            and job["metadata_confidence"] >= self.settings.auto_publish_confidence
        )
        if self.settings.output_mode == "disc":
            await self.complete_disc(job_id)
        elif can_publish:
            await self.publish_job(job_id)
        else:
            self.database.update_job(job_id, status="needs_review")
            self.database.add_event(
                "Rip is safe in staging; confirm the media name to publish it",
                job_id=job_id,
                level="warning",
            )
            await self.broadcast()

        if self.settings.eject_on_success:
            try:
                await self.backend.eject(drive.device)
                self.database.add_event("Drive ejected", job_id=job_id)
            except Exception as exc:
                self.database.add_event(
                    f"Rip saved, but the tray could not open: {exc}",
                    job_id=job_id,
                    level="warning",
                )
            await self.broadcast()

    async def complete_disc(self, job_id: str) -> None:
        """Finish all titles together; a unique folder never replaces another rip."""
        job = self.database.job_detail(job_id)
        assert job
        stage = Path(job["stage_path"])
        label = clean_component(job["disc_name"], "Disc")[:100]
        destination = self.settings.library_root / f"{label} - {job_id}"
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        self.database.update_job(job_id, status="publishing")
        await self.broadcast()
        # Staging is under the configured destination, so this is one filesystem rename.
        stage.rename(destination)
        for title in job["titles"]:
            if title["ripped_path"]:
                self.database.execute(
                    "UPDATE titles SET ripped_path=? WHERE id=?",
                    (str(destination / Path(title["ripped_path"]).name), title["id"]),
                )
        self.database.update_job(
            job_id,
            status="complete",
            final_path=str(destination),
            completed_at=now_iso(),
            progress=1,
            error="",
        )
        self.database.add_event(
            "Rip saved to destination", job_id=job_id, details={"path": str(destination)}
        )
        await self.broadcast()

    async def publish_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.job_detail(job_id)
        if not job:
            raise KeyError(job_id)
        ripped = [title for title in job["titles"] if title["selected"] and title["ripped_path"]]
        if not ripped:
            raise ValueError("This job has no completed title files to publish")
        if not job["title"]:
            raise ValueError("A title is required before publishing")
        self.database.update_job(job_id, status="publishing", error="")
        await self.broadcast()
        ripped.sort(key=lambda item: item["duration_seconds"], reverse=True)
        published: list[Path] = []
        for index, title in enumerate(ripped):
            source = Path(title["ripped_path"])
            if not source.exists():
                raise FileNotFoundError(f"Staged file is missing: {source.name}")
            if job["media_type"] == "tv":
                episode = int(job["episode_start"] or 1) + index
                destination = tv_destination(
                    self.settings.tv_root,
                    show=job["title"],
                    year=job["year"],
                    season=int(job["season"] or 1),
                    episode=episode,
                    episode_title=title["name"] if title["name"] != "MainFeature" else "",
                )
            elif index == 0:
                destination = movie_destination(
                    self.settings.movie_root,
                    title=job["title"],
                    year=job["year"],
                    edition=job["edition"],
                )
            else:
                extra_name = title["name"] or f"Title {title['title_index']:02d}"
                destination = movie_destination(
                    self.settings.movie_root,
                    title=job["title"],
                    year=job["year"],
                    extra_name=extra_name,
                )
            if destination.exists():
                raise FileExistsError(f"Refusing to overwrite {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            published.append(destination)
        stage = Path(job["stage_path"])
        if stage.exists() and not any(stage.iterdir()):
            stage.rmdir()
        common = (
            published[0].parent
            if len(published) == 1
            else Path(Path(*Path(published[0]).parts[:-1]))
        )
        self.database.update_job(
            job_id,
            status="complete",
            final_path=str(common),
            completed_at=now_iso(),
            progress=1,
            error="",
        )
        self.database.add_event(
            f"Published {len(published)} file{'s' if len(published) != 1 else ''}",
            job_id=job_id,
            details={"paths": [str(path) for path in published]},
        )
        await self.broadcast()
        return self.database.job_detail(job_id) or {}

    async def update_metadata(
        self,
        job_id: str,
        *,
        media_type: str,
        title: str,
        year: int | None,
        season: int | None,
        episode_start: int | None,
        edition: str,
        selected_title_ids: list[int] | None,
    ) -> dict[str, Any]:
        if media_type not in {"movie", "tv"}:
            raise ValueError("media_type must be movie or tv")
        if selected_title_ids is not None:
            self.database.set_selected_titles(job_id, selected_title_ids)
        self.database.update_job(
            job_id,
            media_type=media_type,
            title=title.strip(),
            year=year,
            season=season,
            episode_start=episode_start,
            edition=edition.strip(),
            metadata_confidence=1.0,
            error="",
        )
        self.database.add_event("Metadata confirmed", job_id=job_id)
        job = self.database.job_detail(job_id)
        if (
            job
            and job["status"] == "needs_review"
            and any(item["ripped_path"] for item in job["titles"])
        ):
            return await self.publish_job(job_id)
        await self.broadcast()
        return self.database.job_detail(job_id) or {}

    async def retry_job(self, job_id: str) -> None:
        job = self.database.job_detail(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] not in {"failed", "cancelled"}:
            raise ValueError("Only failed or cancelled jobs can be retried")
        if job_id in self._tasks:
            raise ValueError("Job is already running")
        active = self.database.fetchone(
            """
            SELECT id FROM jobs
            WHERE drive_id=? AND id<>?
              AND status IN ('scanning','queued','ripping','publishing')
            LIMIT 1
            """,
            (job["drive_id"], job_id),
        )
        if active:
            raise ValueError("The drive already has an active ingest job")
        drive_row = self.database.fetchone("SELECT * FROM drives WHERE id=?", (job["drive_id"],))
        if not drive_row or not drive_row["disc_name"] or (
            drive_row["disc_name"] != job["disc_name"]
        ):
            raise ValueError("Insert the original disc before retrying this job")
        drive = DriveInfo(
            id=drive_row["id"],
            disc_index=drive_row["disc_index"],
            name=drive_row["name"],
            device=drive_row["device"],
            disc_name=drive_row["disc_name"],
            state=drive_row["state"],
            status_text=drive_row["status_text"],
        )
        selected_rows = [row for row in job["titles"] if row["selected"]]
        selected = [
            TitleInfo(
                index=row["title_index"],
                name=row["name"],
                duration_seconds=row["duration_seconds"],
                size_bytes=row["size_bytes"],
                chapters=row["chapters"],
                playlist=row["playlist"],
                source_filename=row["source_filename"],
                selected=True,
            )
            for row in selected_rows
        ]
        self.database.update_job(
            job_id,
            status="queued" if selected else "scanning",
            progress=0,
            error="",
            completed_at=None,
        )
        task = asyncio.create_task(
            self._run_retry(job_id, drive, selected) if selected else self._run_job(job_id, drive),
            name=f"retry-{job_id}",
        )
        self._track_task(job_id, task)

    async def _run_retry(
        self,
        job_id: str,
        drive: DriveInfo,
        selected: list[TitleInfo],
    ) -> None:
        try:
            async with self._drive_lock(drive.id), self._semaphore:
                await self._rip(job_id, drive, selected)
        except asyncio.CancelledError:
            self.database.update_job(
                job_id,
                status="cancelled",
                error="Cancelled",
                completed_at=now_iso(),
            )
            self.database.add_event("Job cancelled", job_id=job_id, level="warning")
            await self.broadcast()
            raise
        except Exception as exc:
            self.database.update_job(
                job_id,
                status="failed",
                error=str(exc),
                completed_at=now_iso(),
            )
            self.database.add_event(str(exc), job_id=job_id, level="error")
            await self.broadcast()

    async def cancel_job(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            return
        job = self.database.job_detail(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] not in ACTIVE_STATUSES:
            raise ValueError("Job is not currently running")
        self.database.update_job(
            job_id,
            status="cancelled",
            error="Cancelled",
            completed_at=now_iso(),
        )
        self.database.add_event("Job cancelled", job_id=job_id, level="warning")
        await self.broadcast()

    async def eject_drive(self, drive_id: str) -> None:
        drive = self.database.fetchone("SELECT * FROM drives WHERE id=?", (drive_id,))
        if not drive:
            raise KeyError(drive_id)
        lock = self._drive_lock(drive_id)
        if lock.locked():
            raise ValueError("The drive has an active operation")
        async with lock:
            active = self.database.fetchone(
                """
                SELECT id FROM jobs
                WHERE drive_id=? AND status IN ('scanning','queued','ripping','publishing')
                LIMIT 1
                """,
                (drive_id,),
            )
            if active:
                raise ValueError("The drive has an active ingest job")
            await self.backend.eject(drive["device"])
        self._seen_drives.discard(drive_id)
        self.database.add_event(f"Opened tray for {drive['name']}")
        await self.broadcast()

    async def broadcast(self) -> None:
        if not self._subscribers:
            return
        snapshot = self.database.overview()
        for queue in list(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(snapshot)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield self.database.overview()
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
