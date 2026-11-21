import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from openripper.config import Settings
from openripper.db import Database
from openripper.makemkv import DriveInfo, MakeMKVError, SimulationBackend, TitleInfo
from openripper.service import RipperService


class FastBackend(SimulationBackend):
    label = "MY_DVD"
    eject_fails = False

    async def list_drives(self):
        return [DriveInfo("drive-test", 0, "Test DVD drive", "/dev/sr0", self.label, "ready")]

    async def scan_disc(self, device):
        return [TitleInfo(index=0, name="MainFeature", duration_seconds=7200)]

    async def rip_title(self, device, title_index, destination, progress):
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / "title00.mkv"
        output.write_bytes(b"test video contents")
        await progress(1, "Done")
        return output

    async def eject(self, device):
        if self.eject_fails:
            raise OSError("Tray stuck")


def make_service(tmp_path, **overrides):
    root = tmp_path / "library"
    overrides.setdefault("output_mode", "disc")
    settings = Settings(
        library_root=root,
        movie_root=root / "Movies",
        tv_root=root / "TV",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        preferences_path=tmp_path / "settings.json",
        udev_discovery=False,
        compatibility_check=False,
        simulate=True,
        **overrides,
    )
    database = Database(settings.database_url)
    database.initialize()
    return RipperService(settings, database, FastBackend())


async def finish(service):
    await asyncio.gather(*list(service._tasks.values()))


@pytest.mark.asyncio
async def test_enabling_automatic_ripping_starts_a_persisted_waiting_disc_once(tmp_path):
    service = make_service(tmp_path, auto_rip=False, eject_on_success=False)
    await service.poll_once()
    (waiting,) = service.overview()["active_jobs"]
    assert waiting["status"] == "awaiting_start"

    # Recreate the service before enabling automation to exercise the persisted job.
    service = RipperService(service.settings, service.database, service.backend)
    service.settings.auto_rip = True
    await service.poll_once()
    await finish(service)
    await service.poll_once()
    (completed,) = service.overview()["history"]
    assert completed["id"] == waiting["id"]
    assert completed["status"] == "complete"
    assert not service.overview()["active_jobs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["failure", "cancel", "empty"])
async def test_retry_publishes_only_complete_titles(tmp_path, monkeypatch, interruption):
    service = make_service(tmp_path, rip_mode="all")
    interrupted = asyncio.Event()
    attempts = []

    async def scan(device):
        return [TitleInfo(index=i, duration_seconds=7200 - i) for i in range(2)]

    async def rip(device, title_index, destination, progress):
        attempts.append(title_index)
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / f"title{title_index}.mkv"
        if attempts == [0, 1]:
            output.write_bytes(b"" if interruption == "empty" else b"partial")
            interrupted.set()
            if interruption == "cancel":
                await asyncio.Future()
            if interruption == "failure":
                raise MakeMKVError("Read failed")
            return output
        assert not output.exists()
        output.write_bytes(f"complete title {title_index}".encode())
        return output

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    monkeypatch.setattr(service.backend, "rip_title", rip)
    await service.poll_once()
    job_id = service.database.fetchone("SELECT id FROM jobs")["id"]
    await asyncio.wait_for(interrupted.wait(), 5)
    if interruption == "cancel":
        await service.cancel_job(job_id)
    await finish(service)
    job = service.database.job_detail(job_id)
    assert job["status"] == ("cancelled" if interruption == "cancel" else "failed")
    assert not job["titles"][1]["ripped_path"]

    # Older releases could leave unidentified output directly in the staging folder.
    legacy_partial = Path(job["stage_path"]) / "orphan.mkv"
    legacy_partial.write_bytes(b"legacy partial")
    await service.retry_job(job_id)
    await finish(service)
    job = service.database.job_detail(job_id)
    assert job["status"] == "complete", job["error"]
    assert attempts == [0, 1, 1]
    folder = Path(job["final_path"])
    assert {p.name: p.read_bytes() for p in folder.glob("*.mkv")} == {
        "title0.mkv": b"complete title 0",
        "title1.mkv": b"complete title 1",
    }
    retained = [p.read_bytes() for p in folder.parent.rglob("*.mkv") if folder not in p.parents]
    assert b"legacy partial" in retained
    assert (b"" if interruption == "empty" else b"partial") in retained


@pytest.mark.asyncio
async def test_retry_rejects_a_different_disc_with_the_same_label(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    async def fail(*args):
        raise MakeMKVError("Read failed")

    monkeypatch.setattr(service.backend, "rip_title", fail)
    await service.poll_once()
    await finish(service)
    job_id = service.database.fetchone("SELECT id FROM jobs")["id"]
    assert service.database.job_detail(job_id)["status"] == "failed"

    async def scan(device):
        return [TitleInfo(index=0, duration_seconds=900)]

    rip_calls = []

    async def rip(*args):
        rip_calls.append(args)

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    monkeypatch.setattr(service.backend, "rip_title", rip)
    await service.retry_job(job_id)
    await finish(service)
    job = service.database.job_detail(job_id)
    assert job["status"] == "failed"
    assert "Insert the original disc" in job["error"]
    assert not rip_calls


@pytest.mark.asyncio
async def test_cancel_before_queued_task_starts(tmp_path):
    service = make_service(tmp_path)
    await service.poll_once()
    job_id = service.database.fetchone("SELECT id FROM jobs")["id"]
    await service.cancel_job(job_id)
    assert service.database.job_detail(job_id)["status"] == "cancelled"
    assert not list(tmp_path.rglob("*.mkv"))


@pytest.mark.asyncio
async def test_rip_checks_free_space_before_reading_video(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.settings.simulate = False

    async def scan(device):
        return [TitleInfo(index=0, duration_seconds=7200, size_bytes=1_000)]

    async def rip(*args):
        pytest.fail("The rip started even though it would not fit")

    monkeypatch.setattr("openripper.service.optical_media_labels", lambda: {})
    monkeypatch.setattr(
        "openripper.service.shutil.disk_usage", lambda path: SimpleNamespace(free=100)
    )
    monkeypatch.setattr(service.backend, "scan_disc", scan)
    monkeypatch.setattr(service.backend, "rip_title", rip)
    await service.poll_once()
    await finish(service)
    job = service.overview()["history"][0]
    assert job["status"] == "failed"
    assert "No space left" in job["error"]
    assert service.settings.movie_root.as_posix() in job["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("simulate", "ffmpeg_available"), [(False, True), (False, False), (True, True)]
)
async def test_rip_reports_preparation_real_writes_and_verification(
    tmp_path, monkeypatch, simulate, ffmpeg_available
):
    service = make_service(tmp_path, artwork_enabled=False)
    service.settings.simulate = simulate
    no_write = asyncio.Event()
    first_write = asyncio.Event()
    snapshots = []
    verified = []
    warning = "Disc read error; MakeMKV is retrying"

    # Observe both zero-byte and growing-file samples without waiting two seconds each.
    original_wait = asyncio.wait

    async def sample_soon(tasks, *, timeout):
        return await original_wait(tasks, timeout=min(timeout, 0.01))

    async def capture():
        for job in service.overview()["active_jobs"]:
            if job["status"] != "ripping":
                continue
            telemetry = dict(job["telemetry"])
            snapshots.append(telemetry)
            if telemetry.get("bytes_written") == 0:
                no_write.set()
            if telemetry.get("bytes_written", 0) > 0:
                first_write.set()

    async def rip(device, title_index, destination, progress):
        output = destination / "title.mkv"
        await progress(0.2, "Ripping title")
        await progress(0, warning)
        await no_write.wait()
        output.write_bytes(b"video contents")
        await first_write.wait()
        return output

    async def verify(executable, video):
        verified.append(video.read_bytes())

    monkeypatch.setattr("openripper.service.asyncio.wait", sample_soon)
    monkeypatch.setattr("openripper.service.optical_media_labels", lambda: {})
    monkeypatch.setattr(
        "openripper.service.shutil.which", lambda _: "ffmpeg" if ffmpeg_available else None
    )
    monkeypatch.setattr("openripper.service.verify_video", verify)
    monkeypatch.setattr(service, "broadcast", capture)
    monkeypatch.setattr(service.backend, "rip_title", rip)

    await service.poll_once()
    await asyncio.wait_for(finish(service), timeout=5)
    (job,) = service.overview()["history"]
    assert job["status"] == "complete"
    unwritten = [state for state in snapshots if state.get("bytes_written") == 0]
    assert unwritten
    assert all(state.get("phase") == (None if simulate else "opening_title") for state in unwritten)
    assert all(state.get("read_warning") == warning for state in unwritten)

    observed_write = next(state for state in snapshots if state.get("bytes_written", 0) > 0)
    assert "phase" not in observed_write
    assert "read_warning" not in observed_write
    verification = [state for state in snapshots if state.get("phase") == "verifying_video"]
    if not simulate and ffmpeg_available:
        assert verification[0]["bytes_written"] == len(b"video contents")
        assert verified == [b"video contents"]
    else:
        assert not verification
        assert not verified


@pytest.mark.asyncio
async def test_invalid_video_is_retained_without_publication(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.settings.simulate = False

    async def verify(executable, output):
        assert output.read_bytes() == b"test video contents"
        raise ValueError("Output verification failed")

    monkeypatch.setattr("openripper.service.optical_media_labels", lambda: {})
    monkeypatch.setattr("openripper.service.shutil.which", lambda _: "ffmpeg")
    monkeypatch.setattr("openripper.service.verify_video", verify)
    await service.poll_once()
    await finish(service)
    summary = service.overview()["history"][0]
    job = service.database.job_detail(summary["id"])
    assert job["status"] == "failed"
    assert "Output verification failed" in job["error"]
    assert not job["titles"][0]["ripped_path"]
    assert not job["final_path"]
    (retained,) = service.settings.movie_root.rglob("*.mkv")
    assert retained.read_bytes() == b"test video contents"


@pytest.mark.asyncio
async def test_interrupted_artwork_keeps_completed_rip(tmp_path, monkeypatch):
    service = make_service(tmp_path, artwork_enabled=True)
    service.settings.tmdb_token = "test"
    entered = asyncio.Event()

    async def save_poster(*args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(service.metadata, "save_poster", save_poster)
    await service.poll_once()
    job_id = service.database.fetchone("SELECT id FROM jobs")["id"]
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        await service.stop()
    job = service.database.job_detail(job_id)
    assert job["status"] == "complete"
    assert Path(job["titles"][0]["ripped_path"]).read_bytes() == b"test video contents"


@pytest.mark.asyncio
async def test_disc_folder_uses_label_and_duplicate_preserves_both_rips(tmp_path):
    service = make_service(tmp_path)
    await service.poll_once()
    await finish(service)
    first = service.database.overview()["history"][0]
    assert first["status"] == "complete"
    assert Path(first["final_path"]).parent == service.settings.movie_root
    original = Path(first["final_path"]) / "title00.mkv"
    assert original.read_bytes() == b"test video contents"
    await service.poll_once()
    assert service.database.overview()["totals"]["all_jobs"] == 1

    service.backend.label = ""
    await service.poll_once()
    service.backend.label = "MY_DVD"
    await service.poll_once()
    await finish(service)
    jobs = service.database.overview()["history"]
    assert len(jobs) == 2
    assert {job["status"] for job in jobs} == {"complete", "failed"}
    duplicate = next(job for job in jobs if job["status"] == "failed")
    assert "Refusing to overwrite" in duplicate["error"]
    assert (Path(duplicate["stage_path"]) / "title00.mkv").exists()
    assert Path(first["final_path"]).name == "MY_DVD"
    assert original.read_bytes() == b"test video contents"


@pytest.mark.asyncio
async def test_new_label_between_polls_is_a_new_disc(tmp_path):
    service = make_service(tmp_path)
    await service.poll_once()
    await finish(service)
    service.backend.label = "ANOTHER_BLURAY"
    await service.poll_once()
    await finish(service)
    assert service.database.overview()["totals"]["completed"] == 2


@pytest.mark.asyncio
async def test_paused_automation_allows_manual_rip(tmp_path):
    service = make_service(tmp_path, auto_rip=False)
    await service.poll_once()
    job = service.overview()["active_jobs"][0]
    assert job["status"] == "awaiting_start"
    assert not service._tasks
    await service.poll_once()
    assert service.database.overview()["totals"]["all_jobs"] == 1
    await service.start_pending(job["id"], "movie")
    await finish(service)
    assert service.database.overview()["totals"]["completed"] == 1


@pytest.mark.asyncio
async def test_pending_disc_is_removed_when_ejected_and_survives_restart(tmp_path):
    service = make_service(tmp_path, auto_rip=False)
    await service.poll_once()
    job = service.overview()["active_jobs"][0]
    await service.start()
    try:
        await service.poll_once()
        assert service.overview()["active_jobs"][0]["id"] == job["id"]
        assert not service._tasks
        service.backend.label = ""
        await service.poll_once()
        assert service.overview()["active_jobs"] == []
        with pytest.raises(ValueError, match="no longer waiting"):
            await service.start_pending(job["id"], "movie")
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_tv_disc_and_manual_override_hold_files_outside_tv_root(tmp_path, manual):
    service = make_service(tmp_path, auto_rip=not manual)
    service.backend.label = "UNLABELED_DISC" if manual else "EXAMPLE_SHOW_S1_D1"
    await service.poll_once()
    job = service.overview()["active_jobs"][0]
    if manual:
        assert job["status"] == "awaiting_start"
        await service.start_pending(job["id"], "tv")
        with pytest.raises(ValueError, match="no longer waiting"):
            await service.start_pending(job["id"], "tv")
    await finish(service)
    result = service.database.job_detail(job["id"])
    assert result["status"] == "needs_review"
    assert result["media_type"] == "tv"
    assert result["progress"] == 1
    assert not Path(result["stage_path"]).is_relative_to(service.settings.tv_root)
    assert not result["final_path"]
    assert not list(service.settings.tv_root.rglob("*.mkv"))
    assert service.overview()["runtime"]["slots_in_use"] == 0
    assert not list(service.settings.movie_root.rglob("*.mkv"))


@pytest.mark.asyncio
async def test_eject_failure_does_not_fail_saved_rip(tmp_path):
    service = make_service(tmp_path)
    service.backend.eject_fails = True
    await service.poll_once()
    await finish(service)
    job = service.database.overview()["history"][0]
    assert job["status"] == "complete"
    detail = service.database.job_detail(job["id"])
    assert any("Tray stuck" in event["message"] for event in detail["events"])


@pytest.mark.asyncio
async def test_polling_reads_os_media_without_udev(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.settings.simulate = False
    service.backend.label = ""
    monkeypatch.setattr("openripper.service.optical_media_labels", lambda: {"/dev/sr0": "DVD"})
    await service.poll_once()
    await finish(service)
    assert service.database.overview()["totals"]["completed"] == 1


@pytest.mark.asyncio
async def test_existing_destination_is_never_overwritten(tmp_path):
    service = make_service(tmp_path)
    await service.poll_once()
    job = service.database.overview()["active_jobs"][0]
    occupied = service.settings.movie_root / "MY_DVD"
    occupied.mkdir(parents=True)
    (occupied / "keep.txt").write_text("keep")
    await finish(service)
    job = service.database.job_detail(job["id"])
    assert job["status"] == "failed"
    assert (occupied / "keep.txt").read_text() == "keep"
    assert (Path(job["stage_path"]) / "title00.mkv").exists()


@pytest.mark.asyncio
async def test_failed_scan_can_retry_without_selected_titles(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    original_scan = service.backend.scan_disc

    async def failed_scan(device):
        raise MakeMKVError("MakeMKV activation required")

    monkeypatch.setattr(service.backend, "scan_disc", failed_scan)
    await service.poll_once()
    await finish(service)
    job = service.database.overview()["history"][0]
    assert job["status"] == "failed"
    assert service.database.job_detail(job["id"])["titles"] == []
    monkeypatch.setattr(service.backend, "scan_disc", original_scan)
    await service.retry_job(job["id"])
    assert service.database.job_detail(job["id"])["status"] == "queued"
    await finish(service)
    assert service.database.job_detail(job["id"])["status"] == "complete"
    assert service.database.overview()["totals"]["all_jobs"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiting", [False, True])
async def test_waiting_drive_is_queued_until_a_slot_opens(tmp_path, monkeypatch, cancel_waiting):
    service = make_service(tmp_path, max_concurrent_rips=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    scanned = []
    original_scan = service.backend.scan_disc

    async def scan(device):
        scanned.append(device)
        entered.set()
        await release.wait()
        return await original_scan(device)

    drives = [
        DriveInfo(f"drive-{index}", index, "Test drive", f"/dev/sr{index}", f"DVD{index}", "ready")
        for index in range(2)
    ]
    monkeypatch.setattr(service.backend, "scan_disc", scan)
    for drive in drives:
        service.database.upsert_drive(drive.to_dict())
    first = service.queue_drive(drives[0])
    second = service.queue_drive(drives[1])
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert service.database.job_detail(first)["status"] == "scanning"
        assert service.database.job_detail(second)["status"] == "queued"
        assert scanned == ["/dev/sr0"]
        if cancel_waiting:
            await service.cancel_job(second)
            assert service.database.job_detail(second)["status"] == "cancelled"
    finally:
        release.set()
        await finish(service)
    assert service.database.job_detail(first)["status"] == "complete"
    assert service.database.job_detail(second)["status"] == (
        "cancelled" if cancel_waiting else "complete"
    )
    assert scanned == (["/dev/sr0"] if cancel_waiting else ["/dev/sr0", "/dev/sr1"])


@pytest.mark.asyncio
async def test_preview_waits_for_approval_then_rips_to_drive_override(tmp_path):
    override = tmp_path / "dedicated drive"
    service = make_service(
        tmp_path, review_before_rip=True, drive_paths={"drive-test": str(override)}
    )
    await service.poll_once()
    await finish(service)
    job = service.database.overview()["active_jobs"][0]
    assert job["status"] == "needs_review"
    assert not list(override.rglob("*.mkv"))
    assert service._semaphore.active == 0
    detail = service.database.job_detail(job["id"])
    await service.update_metadata(
        job["id"],
        media_type="movie",
        title="My Movie",
        year=2026,
        season=None,
        episode_start=None,
        edition="",
        selected_title_ids=[detail["titles"][0]["id"]],
    )
    await finish(service)
    completed = service.database.job_detail(job["id"])
    assert completed["status"] == "complete"
    assert Path(completed["final_path"]).parent == override / "Movies"


@pytest.mark.asyncio
async def test_extras_are_opt_in(tmp_path, monkeypatch):
    service = make_service(tmp_path, include_extras=True)

    async def scan(device):
        return [
            TitleInfo(index=0, duration_seconds=7200),
            TitleInfo(index=1, duration_seconds=300),
            TitleInfo(index=2, duration_seconds=15),
        ]

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    service.settings.review_before_rip = True
    await service.poll_once()
    await finish(service)
    job = service.database.overview()["active_jobs"][0]
    detail = service.database.job_detail(job["id"])
    assert {t["title_index"] for t in detail["titles"] if t["selected"]} == {0, 1}


@pytest.mark.asyncio
async def test_duplicate_review_retains_titles_for_an_explicit_rerip(tmp_path):
    service = make_service(tmp_path)
    await service.poll_once()
    await finish(service)
    service.settings.output_mode = "library"
    service.backend.label = ""
    await service.poll_once()
    service.backend.label = "MY_DVD"
    await service.poll_once()
    await finish(service)
    job = service.database.overview()["active_jobs"][0]
    detail = service.database.job_detail(job["id"])
    assert detail["status"] == "needs_review"
    assert "duplicate" in detail["error"]
    assert len(detail["titles"]) == 1
    assert not detail["titles"][0]["ripped_path"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disc", "library"])
async def test_live_title_and_final_folder_match_published_output(tmp_path, monkeypatch, mode):
    override = tmp_path / "separate destination"
    service = make_service(tmp_path, output_mode=mode, drive_paths={"drive-test": str(override)})
    entered = asyncio.Event()
    release = asyncio.Event()
    original_rip = service.backend.rip_title

    async def rip(device, title_index, destination, progress):
        entered.set()
        await release.wait()
        return await original_rip(device, title_index, destination, progress)

    monkeypatch.setattr(service.backend, "rip_title", rip)
    await service.poll_once()
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        job = service.overview()["active_jobs"][0]
        assert job["telemetry"]["title_name"] == "MainFeature"
        displayed = Path(job["destination_folder"])
        assert displayed.is_relative_to(override)
        assert ".openripper-staging" not in displayed.parts
    finally:
        release.set()
        await finish(service)
    detail = service.database.job_detail(job["id"])
    if mode == "library" and detail["status"] == "needs_review":
        await service.publish_job(job["id"])
        detail = service.database.job_detail(job["id"])
    assert detail["status"] == "complete"
    output = Path(detail["titles"][0]["ripped_path"])
    assert output.parent == displayed


@pytest.mark.asyncio
async def test_rip_now_bypasses_only_selected_job_and_preserves_limit(tmp_path, monkeypatch):
    service = make_service(tmp_path, max_concurrent_rips=1)
    entered = {f"/dev/sr{i}": asyncio.Event() for i in range(3)}
    release = asyncio.Event()

    async def scan(device):
        entered[device].set()
        await release.wait()
        return [TitleInfo(index=0, name="Feature", duration_seconds=7200)]

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    jobs = []
    for index in range(3):
        drive = DriveInfo(f"test-{index}", index, "Test", f"/dev/sr{index}", "DVD", "ready")
        service.database.upsert_drive(drive.to_dict())
        jobs.append(service.queue_drive(drive))
    try:
        await asyncio.wait_for(entered["/dev/sr0"].wait(), 2)
        await service.start_queued_now(jobs[2])
        await asyncio.wait_for(entered["/dev/sr2"].wait(), 2)
        assert not entered["/dev/sr1"].is_set()
        assert service.settings.max_concurrent_rips == service._semaphore.limit == 1
        assert service._semaphore.active == 2
        assert service.overview()["runtime"]["slot_overrides_active"] == 1
        assert service.overview()["totals"]["all_jobs"] == 3
        with pytest.raises(ValueError, match="no longer waiting"):
            await service.start_queued_now(jobs[2])
        await service.cancel_job(jobs[2])
        assert service._semaphore.active == 1
        assert not service._semaphore.active_overrides
        assert not entered["/dev/sr1"].is_set()
        await service.cancel_job(jobs[0])
        await asyncio.wait_for(entered["/dev/sr1"].wait(), 2)
        assert service._semaphore.active == 1
    finally:
        release.set()
        await finish(service)
    assert service._semaphore.active == 0
    assert not service._semaphore._overrides


@pytest.mark.asyncio
async def test_unknown_alternates_force_review_without_writing_video(tmp_path, monkeypatch):
    service = make_service(tmp_path, review_before_rip=False)
    service.backend.label = "EXAMPLE_SHOW_S1_D1"

    async def scan(device):
        return [
            TitleInfo(index=i, duration_seconds=2700, size_bytes=1000, playlist=f"{i:05d}.mpls")
            for i in range(3)
        ]

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    await service.poll_once()
    await finish(service)
    job = service.overview()["active_jobs"][0]
    assert job["status"] == "needs_review"
    assert "alternate titles" in job["error"]
    assert not list(tmp_path.rglob("*.mkv"))
    assert service._semaphore.active == 0


@pytest.mark.asyncio
async def test_extras_do_not_reintroduce_known_duplicates_and_evidence_persists(
    tmp_path, monkeypatch
):
    service = make_service(tmp_path, include_extras=True, review_before_rip=True)

    async def scan(device):
        return [
            TitleInfo(
                index=0,
                duration_seconds=7200,
                size_bytes=1000,
                playlist="00800.mpls",
                segment_map="50",
                chapters=10,
            ),
            TitleInfo(index=1, duration_seconds=7200, size_bytes=1000, playlist="00050.m2ts"),
            TitleInfo(
                index=2,
                duration_seconds=300,
                size_bytes=100,
                playlist="00801.mpls",
                segment_map="51",
                chapters=2,
            ),
            TitleInfo(index=3, duration_seconds=300, size_bytes=100, playlist="00051.m2ts"),
        ]

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    await service.poll_once()
    await finish(service)
    job = service.overview()["active_jobs"][0]
    for _ in range(101):
        service.database.add_event("Later event", job_id=job["id"])
    detail = Database(service.settings.database_url).job_detail(job["id"])
    assert {t["title_index"] for t in detail["titles"] if t["selected"]} == {0, 2}
    by_index = {t["title_index"]: t for t in detail["titles"]}
    assert by_index[1]["duplicate_of"] == 0
    assert by_index[3]["duplicate_of"] == 2
    assert by_index[0]["segment_map"] == "50"


@pytest.mark.asyncio
@pytest.mark.parametrize("season", [0, 2])
async def test_tv_publish_keeps_disc_order_and_does_not_append_disc_label(tmp_path, season):
    service = make_service(tmp_path, output_mode="library", auto_rip=False)
    drive = (await service.backend.list_drives())[0]
    service.database.upsert_drive(drive.to_dict())
    stage = tmp_path / "stage"
    stage.mkdir()
    service.database.create_job(
        dict(
            id="tv-order",
            drive_id=drive.id,
            fingerprint="tv-order",
            disc_name="EXAMPLE_SHOW_S2_D2",
            title="Example Show",
            media_type="tv",
            season=season,
            episode_start=4,
            status="needs_review",
            stage_path=str(stage),
        )
    )
    service.database.update_job("tv-order", season=season, episode_start=4)
    titles = [
        TitleInfo(
            index=i, name="Example Show Season Two Disc 2", duration_seconds=duration, selected=True
        ).to_dict()
        for i, duration in [(7, 2200), (9, 3300), (12, 2700)]
    ]
    service.database.replace_titles("tv-order", titles)
    for item in service.database.job_detail("tv-order")["titles"]:
        path = stage / f"title-{item['title_index']}.mkv"
        path.write_text(str(item["title_index"]))
        service.database.execute(
            "update titles set ripped_path=? where id=?", (str(path), item["id"])
        )
    assert [t["title_index"] for t in service.database.job_detail("tv-order")["titles"]] == [
        7,
        9,
        12,
    ]
    assignments = [
        {"title_id": t["id"], "season": season, "episode": number}
        for t, number in zip(
            service.database.job_detail("tv-order")["titles"], [6, 4, 5], strict=True
        )
    ]
    await service.update_metadata(
        "tv-order",
        media_type="tv",
        title="Example Show",
        year=None,
        season=season,
        episode_start=None,
        edition="",
        selected_title_ids=None,
        episode_assignments=assignments,
    )
    output = service.settings.tv_root / "Example Show" / f"Season {season:02d}"
    assert [
        (output / f"Example Show - S{season:02d}E{episode:02d}.mkv").read_text()
        for episode in [4, 5, 6]
    ] == ["9", "12", "7"]


@pytest.mark.asyncio
async def test_tv_publish_requires_episode_confirmation_without_mutating_job(tmp_path):
    service = make_service(tmp_path, output_mode="library", auto_rip=False)
    drive = (await service.backend.list_drives())[0]
    service.database.upsert_drive(drive.to_dict())
    stage = tmp_path / "stage"
    stage.mkdir()
    source = stage / "episode.mkv"
    source.write_bytes(b"episode")
    service.database.create_job(
        dict(
            id="tv-review",
            drive_id=drive.id,
            fingerprint="tv-review",
            disc_name="EXAMPLE_SHOW_S2_D2",
            title="Example Show",
            media_type="tv",
            season=2,
            status="needs_review",
            stage_path=str(stage),
        )
    )
    service.database.replace_titles("tv-review", [TitleInfo(index=0, selected=True).to_dict()])
    service.database.execute(
        "update titles set ripped_path=? where job_id=?", (str(source), "tv-review")
    )
    with pytest.raises(ValueError, match="Match every selected TV file"):
        await service.publish_job("tv-review")
    assert source.read_bytes() == b"episode"
    assert service.database.job_detail("tv-review")["status"] == "needs_review"
