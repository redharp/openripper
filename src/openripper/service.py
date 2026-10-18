from __future__ import annotations

import asyncio
import errno
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, now_iso
from .discovery import optical_hotplug_events, optical_media_labels
from .episodes import episode_code, validate_assignments
from .handbrake import encode
from .integrations import Handoffs
from .makemkv import (
    DriveInfo,
    MakeMKVError,
    RipperBackend,
    TitleInfo,
    ambiguous_title_pairs,
    choose_titles,
    content_key,
    fingerprint_disc,
    unique_titles,
)
from .metadata import MetadataResolver, disc_media_type, disc_season, disc_title
from .naming import clean_component, movie_destination, tv_destination
from .output import render_name
from .runtime import RipSlots, TransferMeter

ACTIVE_STATUSES = {"awaiting_start", "scanning", "queued", "ripping", "publishing"}
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
        self._semaphore = RipSlots(settings.max_concurrent_rips)
        self._telemetry: dict[str, dict] = {}
        self._poll_lock = asyncio.Lock()
        self._drive_locks: dict[str, asyncio.Lock] = {}
        self.handoffs = Handoffs(settings, database, self.broadcast)

    def _drive_lock(self, drive_id: str) -> asyncio.Lock:
        return self._drive_locks.setdefault(drive_id, asyncio.Lock())

    def _track_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[job_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            self._semaphore.clear_override(job_id)
            self._telemetry.pop(job_id, None)
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
        await self.handoffs.resume()
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
        await self.handoffs.stop()

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
            for pending in self.database.fetchall(
                "SELECT id, drive_id, disc_name FROM jobs WHERE status='awaiting_start'"
            ):
                loaded = next((d for d in drives if d.id == pending["drive_id"]), None)
                if loaded is None or loaded.disc_name != pending["disc_name"]:
                    self.database.update_job(
                        pending["id"], status="dismissed", completed_at=now_iso()
                    )
            for drive in drives:
                active_job = self.database.fetchone(
                    """
                    SELECT id, status, progress, disc_name FROM jobs
                    WHERE drive_id=? AND status IN
                    ('awaiting_start','needs_review','scanning','queued','ripping','publishing')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (drive.id,),
                )
                if (
                    active_job
                    and active_job["status"] == "needs_review"
                    and active_job["disc_name"] != drive.disc_name
                ):
                    active_job = None
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
                if drive.disc_name and drive.id not in self._seen_drives:
                    self.queue_drive(drive, hold=not self.settings.auto_rip)
                    if self.settings.auto_rip:
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

    def media_root(self, drive_id: str, media_type: str) -> Path:
        override = self.settings.drive_paths.get(drive_id)
        if override:
            return Path(override) / ("TV" if media_type == "tv" else "Movies")
        return self.settings.tv_root if media_type == "tv" else self.settings.movie_root

    def staging_folder(self, drive_id: str, media_type: str, job_id: str) -> Path:
        root = self.media_root(drive_id, media_type)
        # TV is held beside its library folder, never inside the watched TV tree.
        if media_type == "tv":
            root = root.parent
        return root / ".openripper-staging" / job_id

    def queue_drive(self, drive: DriveInfo, *, hold: bool = False) -> str:
        self._seen_drives.add(drive.id)
        self._media_labels[drive.id] = drive.disc_name
        active = self.database.fetchone(
            """
            SELECT id FROM jobs
            WHERE drive_id=? AND status IN
                    ('awaiting_start','needs_review','scanning','queued','ripping','publishing')
              AND (status<>'needs_review' OR disc_name=?)
            ORDER BY created_at DESC LIMIT 1
            """,
            (drive.id, drive.disc_name),
        )
        if active:
            self._seen_drives.add(drive.id)
            return str(active["id"])
        job_id = uuid.uuid4().hex
        provisional = f"{drive.id}:{drive.disc_name}"
        media_type = disc_media_type(drive.disc_name)
        self.database.create_job(
            {
                "id": job_id,
                "drive_id": drive.id,
                "disc_name": drive.disc_name,
                "fingerprint": provisional,
                "title": disc_title(drive.disc_name)[0],
                "media_type": media_type,
                "status": "awaiting_start" if hold else "queued",
                "stage_path": str(self.staging_folder(drive.id, media_type, job_id)),
            }
        )
        self.database.add_event(
            f"Detected {drive.disc_name}", job_id=job_id, details={"drive": drive.name}
        )
        if hold:
            return job_id
        task = asyncio.create_task(self._run_job(job_id, drive), name=f"rip-{job_id}")
        self._track_task(job_id, task)
        return job_id

    async def start_pending(self, job_id: str, media_type: str) -> None:
        job = self.database.job_detail(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] != "awaiting_start":
            raise ValueError("This disc is no longer waiting to start")
        row = self.database.fetchone("SELECT * FROM drives WHERE id=?", (job["drive_id"],))
        if not row or not row["online"] or row["disc_name"] != job["disc_name"]:
            raise ValueError("Insert the original disc and refresh before starting")
        if media_type not in {"auto", "movie", "tv"}:
            raise ValueError("Choose Auto, Movie or TV show")
        drive = DriveInfo(
            id=row["id"],
            disc_index=row["disc_index"],
            name=row["name"],
            device=row["device"],
            disc_name=row["disc_name"],
            state="ready",
        )
        self.database.update_job(
            job_id,
            status="queued",
            media_type=job["media_type"] if media_type == "auto" else media_type,
        )
        self._track_task(
            job_id,
            asyncio.create_task(
                self._run_job(
                    job_id, drive, media_type=None if media_type == "auto" else media_type
                ),
                name=f"rip-{job_id}",
            ),
        )
        self.database.add_event(
            "Start requested", job_id=job_id, details={"media_type": media_type}
        )
        await self.broadcast()

    async def _run_job(
        self, job_id: str, drive: DriveInfo, *, media_type: str | None = None
    ) -> None:
        try:
            async with self._drive_lock(drive.id), self._semaphore.slot(job_id):
                await self._process_job(job_id, drive, media_type=media_type)
        except asyncio.CancelledError:
            job = self.database.job_detail(job_id)
            if job and job["status"] == "queued":
                self.database.update_job(
                    job_id, status="cancelled", error="Cancelled", completed_at=now_iso()
                )
                self.database.add_event("Job cancelled", job_id=job_id, level="warning")
                await self.broadcast()
            raise

    async def _process_job(
        self, job_id: str, drive: DriveInfo, *, media_type: str | None = None
    ) -> None:
        try:
            self.database.update_job(job_id, status="scanning")
            await self.broadcast()
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
            match = await self.metadata.resolve(drive.disc_name, titles)
            selected = choose_titles(
                titles,
                min_seconds=self.settings.min_title_seconds,
                mode=self.settings.rip_mode,
                media_type=media_type or match.media_type,
            )
            if not selected:
                raise MakeMKVError("No titles passed the configured selection rules")
            # Bonus titles are optional; never add short material to an episode cluster.
            if self.settings.include_extras and len(selected) == 1:
                selected_indexes = {title.index for title in selected}
                for title in unique_titles(titles):
                    if title.index not in selected_indexes and (
                        title.duration_seconds >= self.settings.extras_min_seconds
                    ):
                        title.selected = True
                        selected.append(title)
            self.database.replace_titles(job_id, [title.to_dict() for title in titles])

            unique = unique_titles(titles)
            canonical = {content_key(t): t.index for t in unique if content_key(t) is not None}
            evidence = {
                str(t.index): {
                    "segment_map": t.segment_map,
                    "duplicate_of": canonical.get(content_key(t)) if t not in unique else None,
                }
                for t in titles
            }
            self.database.add_event(
                "Title scan evidence", job_id=job_id, details={"titles": evidence}
            )
            self.database.update_job(
                job_id,
                media_type=media_type or match.media_type,
                title=match.title,
                year=match.year,
                metadata_confidence=match.confidence,
                season=disc_season(drive.disc_name, titles)
                if (media_type or match.media_type) == "tv"
                else None,
                status="queued",
                stage_path=str(
                    self.staging_folder(drive.id, media_type or match.media_type, job_id)
                ),
            )
            detected_kind = "TV show" if (media_type or match.media_type) == "tv" else "Movie"
            detected_source = "manual override" if media_type else match.source
            self.database.add_event(
                f"{detected_kind} detected from {detected_source}",
                job_id=job_id,
            )
            self.database.add_event(
                f"Selected {len(selected)} title{'s' if len(selected) != 1 else ''}",
                job_id=job_id,
                details={
                    "metadata_source": match.source,
                    "confidence": match.confidence,
                },
            )
            duplicate_review = duplicate and self.settings.output_mode == "library"
            uncertain_selection = self.settings.rip_mode != "all" and ambiguous_title_pairs(
                selected
            )
            if self.settings.review_before_rip or duplicate_review or uncertain_selection:
                self.database.update_job(
                    job_id,
                    status="needs_review",
                    error=f"Possible duplicate of job {duplicate['id'][:8]}"
                    if duplicate_review
                    else "Same size and runtime: review possible alternate titles before ripping."
                    if uncertain_selection
                    else "",
                )
                self.database.add_event(
                    "This disc appears to have been completed before"
                    if duplicate_review
                    else "Review selected titles before ripping",
                    job_id=job_id,
                    level="warning" if duplicate_review else "info",
                )
                await self.broadcast()
                return
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
        meter = TransferMeter(time.monotonic(), stage_bytes(stage))
        expected_bytes = sum(title.size_bytes for title in selected_titles)
        for offset, title in enumerate(selected_titles):
            recorded = self.database.job_detail(job_id)
            completed = (
                next(
                    (
                        item["ripped_path"]
                        for item in recorded["titles"]
                        if item["title_index"] == title.index and item["ripped_path"]
                    ),
                    "",
                )
                if recorded
                else ""
            )
            if completed and Path(completed).is_file() and Path(completed).stat().st_size:
                continue
            reported_value = 0.0

            async def report(value: float, message: str, *, current: int = offset) -> None:
                nonlocal reported_value
                if message.startswith("Disc read error"):
                    telemetry = self._telemetry.setdefault(job_id, {})
                    if telemetry.get("read_warning") != message:
                        self.database.add_event(message, job_id=job_id, level="warning")
                    telemetry["read_warning"] = message
                    await self.broadcast()
                    return
                measured = max(0.0, min(1.0, value))
                if measured <= reported_value:
                    return
                reported_value = measured
                overall = (current + reported_value) / len(selected_titles)
                self.database.update_job(job_id, progress=round(overall, 4))
                await self.broadcast()

            baseline_bytes = stage_bytes(stage)
            self._telemetry[job_id] = {
                "title_number": offset + 1,
                "title_count": len(selected_titles),
                "title_name": title.name or f"Title {title.index}",
            }
            rip_task = asyncio.create_task(
                self.backend.rip_title(drive.device, title.index, stage, report),
                name=f"makemkv-{job_id}-{title.index}",
            )
            try:
                while not rip_task.done():
                    await asyncio.wait({rip_task}, timeout=2)
                    current_bytes = stage_bytes(stage)
                    self._telemetry[job_id].update(
                        meter.sample(time.monotonic(), current_bytes, expected_bytes)
                    )
                    if self._telemetry[job_id]["seconds_since_write"] == 0:
                        self._telemetry[job_id].pop("read_warning", None)
                    if title.size_bytes > 0:
                        written = max(0, current_bytes - baseline_bytes)
                        await report(min(0.995, written / title.size_bytes), "Ripping title")
                    await self.broadcast()
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
        if self.settings.handbrake_enabled:
            await self._encode_job(job_id)
        job = self.database.job_detail(job_id)
        assert job
        can_publish = (
            bool(job["title"])
            and job["metadata_confidence"] >= self.settings.auto_publish_confidence
            and (
                job["media_type"] != "tv"
                or (job["season"] is not None and job["episode_start"] is not None)
            )
        )
        if job["media_type"] == "tv":
            await self.hold_tv_job(job_id)
        elif self.settings.output_mode == "disc":
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

    async def hold_tv_job(self, job_id: str) -> None:
        self.database.update_job(job_id, status="needs_review", progress=1, error="")
        self.database.add_event(
            "Rip finished; match episodes before publishing to the TV library",
            job_id=job_id,
            level="warning",
        )
        await self.broadcast()

    async def complete_disc(self, job_id: str) -> None:
        """Finish all titles together without replacing an existing disc folder."""
        job = self.database.job_detail(job_id)
        assert job
        if job["media_type"] == "tv":
            await self.hold_tv_job(job_id)
            return
        stage = Path(job["stage_path"])
        destination = self.destination_folder(job)
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        self.database.update_job(job_id, status="publishing")
        await self.broadcast()
        # Staging is under the configured destination, so this is one filesystem rename.
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after broadcasting: another drive may have published this name.
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite {destination}")
        stage.rename(destination)
        await self._save_artwork(job, destination)
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
        await self.handoffs.auto(job_id)
        await self.broadcast()

    def destination_folder(self, job: dict) -> Path:
        """Show the same destination folder that publishing will use."""
        override = self.settings.drive_paths.get(job["drive_id"])
        if self.settings.output_mode == "library" or job["media_type"] == "tv":
            if job["media_type"] == "tv":
                root = Path(override) / "TV" if override else self.settings.tv_root
                return tv_destination(
                    root,
                    show=job["title"],
                    year=job["year"],
                    season=int(job["season"] if job["season"] is not None else 1),
                    episode=int(job["episode_start"] or 1),
                ).parent
            root = Path(override) / "Movies" if override else self.settings.movie_root
            return movie_destination(
                root,
                title=job["title"],
                year=job["year"],
                edition=job["edition"],
            ).parent
        job_id = job["id"]
        folder_name = render_name(
            self.settings.disc_folder_template,
            disc=clean_component(job["disc_name"], "Disc"),
            job_id=job_id,
            title=job["title"],
            year=job["year"] or 0,
        )
        return self.media_root(job["drive_id"], job["media_type"]) / folder_name

    async def publish_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.job_detail(job_id)
        if not job:
            raise KeyError(job_id)
        ripped = [title for title in job["titles"] if title["selected"] and title["ripped_path"]]
        if not ripped:
            raise ValueError("This job has no completed title files to publish")
        if not job["title"]:
            raise ValueError("A title is required before publishing")
        assignments = {}
        if job["media_type"] == "tv":
            assignments = validate_assignments(job["titles"], job.get("episode_assignments"))
            if len(ripped) != len(assignments):
                raise ValueError("All selected episodes must finish ripping before publishing")
        if job["media_type"] == "tv":
            ripped.sort(key=lambda item: item["title_index"])
        else:
            ripped.sort(key=lambda item: item["duration_seconds"], reverse=True)
        published: list[Path] = []
        plan = []
        override = self.settings.drive_paths.get(job["drive_id"])
        movie_root = Path(override) / "Movies" if override else self.settings.movie_root
        tv_root = Path(override) / "TV" if override else self.settings.tv_root
        for index, title in enumerate(ripped):
            source = Path(title["ripped_path"])
            if not source.exists():
                raise FileNotFoundError(f"Staged file is missing: {source.name}")
            episode_name = title["name"]
            if job["media_type"] == "tv":
                assignment = assignments[title["id"]]
                episode = assignment["episode"]
                episode_name = assignment.get("episode_title", "").strip()
                if episode_name == "MainFeature" or disc_title(episode_name)[0].casefold() == (
                    disc_title(job["title"])[0].casefold()
                ):
                    episode_name = ""
                destination = tv_destination(
                    tv_root,
                    show=job["title"],
                    year=job["year"],
                    season=assignment["season"],
                    episode=episode,
                    episode_title=episode_name,
                )
            elif index == 0:
                destination = movie_destination(
                    movie_root,
                    title=job["title"],
                    year=job["year"],
                    edition=job["edition"],
                )
            else:
                extra_name = title["name"] or f"Title {title['title_index']:02d}"
                destination = movie_destination(
                    movie_root,
                    title=job["title"],
                    year=job["year"],
                    extra_name=extra_name,
                )
            if job["media_type"] == "tv" or index == 0:
                template = (
                    self.settings.tv_name_template
                    if job["media_type"] == "tv"
                    else self.settings.movie_name_template
                )
                destination = destination.with_name(
                    render_name(
                        template,
                        title=job["title"],
                        disc=job["disc_name"],
                        job_id=job_id,
                        year=job["year"] or 0,
                        year_suffix=f" ({job['year']})" if job["year"] else "",
                        edition_suffix=f" {{edition-{clean_component(job['edition'])}}}"
                        if job["edition"]
                        else "",
                        season=assignment["season"] if assignments else 1,
                        episode=assignment["episode"] if assignments else 1,
                        episode_suffix=f" - {clean_component(episode_name)}"
                        if episode_name and episode_name != "MainFeature"
                        else "",
                        title_index=title["title_index"],
                    )
                    + ".mkv"
                )
            if assignments:
                code = episode_code(assignment)
                first = f"S{assignment['season']:02d}E{assignment['episode']:02d}"
                name = destination.stem
                name = name.replace(first, code) if first in name else f"{name} - {code}"
                destination = destination.with_name(name + ".mkv")
            if destination.exists() and source != destination:
                raise FileExistsError(f"Refusing to overwrite {destination}")
            if any(target == destination for _, _, target in plan):
                raise ValueError("The naming template produces duplicate filenames")
            plan.append((title, source, destination))
        # Validate the entire batch before exposing even one file to a library scanner.
        self.database.update_job(job_id, status="publishing", error="")
        await self.broadcast()
        for title, source, destination in plan:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source != destination:
                if destination.exists():
                    raise FileExistsError(f"Refusing to overwrite {destination}")
                # Cross-filesystem copies stay invisible as videos until fully copied.
                temporary = destination.with_name(destination.name + f".{job_id}.pending")
                if temporary.exists() and temporary != source:
                    raise FileExistsError(f"An unfinished publish needs inspection: {temporary}")
                size = source.stat().st_size
                copied = False
                if source != temporary:
                    try:
                        source.rename(temporary)
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        # Keep the original until the copy is verified and promoted.
                        await asyncio.to_thread(shutil.copyfile, source, temporary)
                        copied = True
                if not copied:
                    self.database.execute(
                        "UPDATE titles SET ripped_path=? WHERE id=?", (str(temporary), title["id"])
                    )
                if temporary.stat().st_size != size:
                    raise OSError("Published copy size differs from the staged video")
                if destination.exists():
                    raise FileExistsError(f"Refusing to overwrite {destination}")
                temporary.rename(destination)
                self.database.execute(
                    "UPDATE titles SET ripped_path=? WHERE id=?", (str(destination), title["id"])
                )
                if copied:
                    source.unlink()

            published.append(destination)
            self.database.execute(
                "UPDATE titles SET ripped_path=? WHERE id=?", (str(destination), title["id"])
            )
        stage = Path(job["stage_path"])
        if stage.exists() and not any(stage.iterdir()):
            stage.rmdir()
        common = Path(os.path.commonpath([path.parent for path in published]))
        await self._save_artwork(job, common)
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
        await self.handoffs.auto(job_id)
        await self.broadcast()
        return self.database.job_detail(job_id) or {}

    async def _encode_job(self, job_id: str) -> None:
        job = self.database.job_detail(job_id)
        assert job
        self.database.update_job(job_id, status="publishing")
        selected = [t for t in job["titles"] if t["selected"] and t["ripped_path"]]
        for index, title in enumerate(selected):
            if Path(title["ripped_path"]).name.endswith(".encoded.mkv"):
                continue
            self._telemetry[job_id] = {
                "phase": "encoding",
                "title_name": title["name"] or f"Title {title['title_index']}",
                "title_number": index + 1,
                "title_count": len(selected),
                "encode_progress": 0,
            }

            async def report(progress: float) -> None:
                self._telemetry[job_id]["encode_progress"] = progress
                await self.broadcast()

            await self.broadcast()
            if self.settings.simulate:
                raise MakeMKVError(
                    "HandBrake needs a real video; simulation files cannot be encoded"
                )
            output = await encode(Path(title["ripped_path"]), self.settings, report)
            self.database.execute(
                "UPDATE titles SET ripped_path=? WHERE id=?", (str(output), title["id"])
            )
            self.database.add_event("HandBrake encode completed", job_id=job_id)
        self._telemetry.pop(job_id, None)

    async def _save_artwork(self, job: dict, folder: Path) -> None:
        if not self.settings.artwork_enabled:
            return
        try:
            saved = await self.metadata.save_poster(
                folder, job["title"], job["year"], job["media_type"]
            )
            self.database.add_event(
                "Poster artwork saved"
                if saved
                else "No confident artwork match; video saved without a poster",
                job_id=job["id"],
                level="info" if saved else "warning",
            )
        except Exception as exc:
            self.database.add_event(
                f"Video saved; artwork unavailable: {exc}", job_id=job["id"], level="warning"
            )

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
        episode_assignments: list[dict] | None = None,
    ) -> dict[str, Any]:
        existing = self.database.job_detail(job_id)
        if not existing or existing["status"] != "needs_review":
            raise ValueError("Only jobs awaiting review can be edited")
        if media_type not in {"movie", "tv"}:
            raise ValueError("media_type must be movie or tv")
        if not title.strip():
            raise ValueError("Enter the show or movie title")
        has_files = any(item["ripped_path"] for item in existing["titles"])
        if media_type == "tv" and has_files:
            proposed = [
                {**item, "selected": item["id"] in selected_title_ids}
                if selected_title_ids is not None
                else item
                for item in existing["titles"]
            ]
            matches = validate_assignments(proposed, episode_assignments)
            if any(t["selected"] and not t["ripped_path"] for t in proposed):
                raise ValueError("Select only completed files; unripped titles need another rip")
            first = next(iter(matches.values()))
            season, episode_start = first["season"], first["episode"]
        if selected_title_ids is not None:
            valid = {item["id"] for item in existing["titles"]}
            if not selected_title_ids or not set(selected_title_ids).issubset(valid):
                raise ValueError("Select at least one valid title from this disc")
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
        if media_type == "tv" and has_files:
            self.database.add_event(
                "Episode assignments confirmed",
                job_id=job_id,
                details={"assignments": list(matches.values())},
            )
        self.database.add_event("Metadata confirmed", job_id=job_id)
        if not any(item["ripped_path"] for item in existing["titles"]):
            self.database.update_job(
                job_id,
                stage_path=str(self.staging_folder(existing["drive_id"], media_type, job_id)),
            )
        job = self.database.job_detail(job_id)
        if (
            job
            and job["status"] == "needs_review"
            and any(item["ripped_path"] for item in job["titles"])
        ):
            try:
                return await self.publish_job(job_id)
            except (OSError, ValueError) as exc:
                self.database.update_job(job_id, status="needs_review", error=str(exc))
                await self.broadcast()
                raise
        if job and job["status"] == "needs_review":
            await self.retry_job(job_id, approved=True)
        await self.broadcast()
        return self.database.job_detail(job_id) or {}

    async def retry_job(self, job_id: str, *, approved: bool = False) -> None:
        job = self.database.job_detail(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] not in {"failed", "cancelled"} and not (
            approved and job["status"] == "needs_review"
        ):
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
        if (
            not drive_row
            or not drive_row["disc_name"]
            or (drive_row["disc_name"] != job["disc_name"])
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
                segment_map=row.get("segment_map", ""),
            )
            for row in selected_rows
        ]
        self.database.update_job(
            job_id,
            status="queued",
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
            async with self._drive_lock(drive.id), self._semaphore.slot(job_id):
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

    async def start_queued_now(self, job_id: str) -> None:
        job = self.database.job_detail(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] != "queued" or job_id not in self._tasks:
            raise ValueError("This job is no longer waiting for a slot")
        await self._semaphore.override(job_id)
        self.database.add_event(
            "Rip now requested: bypassing the slot limit for this job only", job_id=job_id
        )
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
        if job["status"] == "publishing":
            raise ValueError("Files are being published; wait for publication to finish")
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
        snapshot = self.overview()
        for queue in list(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(snapshot)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield self.overview()
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    def overview(self) -> dict[str, Any]:
        snapshot = self.database.overview()
        queued = sorted(
            (job for job in snapshot["active_jobs"] if job["status"] == "queued"),
            key=lambda job: job["created_at"],
        )
        positions = {job["id"]: index + 1 for index, job in enumerate(queued)}
        for job in snapshot["active_jobs"]:
            job["telemetry"] = self._telemetry.get(job["id"], {})
            job["queue_position"] = positions.get(job["id"])
            job["slot_override"] = job["id"] in self._semaphore.active_overrides
            if job["stage_path"] and (job["title"] or self.settings.output_mode == "disc"):
                job["destination_folder"] = str(self.destination_folder(job))
        snapshot["runtime"] = {
            "max_concurrent_rips": self._semaphore.limit,
            "slots_in_use": self._semaphore.active,
            "slot_override_supported": True,
            "queue_controls_supported": True,
            "slot_overrides_active": len(self._semaphore.active_overrides),
            "auto_rip": self.settings.auto_rip,
            "updated_at": now_iso(),
        }
        return snapshot
