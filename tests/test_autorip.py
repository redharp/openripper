import asyncio
from pathlib import Path

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
async def test_insert_rips_to_destination_once_and_reinsert_makes_unique_folder(tmp_path):
    service = make_service(tmp_path)
    await service.poll_once()
    await finish(service)
    first = service.database.overview()["history"][0]
    assert first["status"] == "complete"
    assert Path(first["final_path"]).parent == service.settings.library_root
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
    assert all(job["status"] == "complete" for job in jobs)
    assert len({job["final_path"] for job in jobs}) == 2
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
    assert service.database.overview()["totals"]["all_jobs"] == 0
    service.queue_drive((await service.backend.list_drives())[0])
    await finish(service)
    assert service.database.overview()["totals"]["completed"] == 1


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
    occupied = service.settings.library_root / f"MY_DVD - {job['id']}"
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
    await finish(service)
    assert service.database.job_detail(job["id"])["status"] == "complete"
    assert service.database.overview()["totals"]["all_jobs"] == 1
