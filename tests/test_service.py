import asyncio
from pathlib import Path

import pytest

from openripper.config import Settings
from openripper.db import Database
from openripper.makemkv import SimulationBackend
from openripper.service import RipperService, stage_bytes


def test_stage_bytes_counts_growing_mkv_files(tmp_path: Path) -> None:
    (tmp_path / "title-1.mkv").write_bytes(b"a" * 20)
    (tmp_path / "title-2.mkv").write_bytes(b"b" * 30)
    (tmp_path / "ignored.txt").write_bytes(b"c" * 100)

    assert stage_bytes(tmp_path) == 50


@pytest.mark.asyncio
async def test_simulated_disc_rips_then_waits_for_safe_publish(tmp_path: Path) -> None:
    settings = Settings(
        output_mode="library",
        library_root=tmp_path / "library",
        movie_root=tmp_path / "library" / "Movies",
        tv_root=tmp_path / "library" / "TV",
        database_url=f"sqlite:///{tmp_path / 'service.db'}",
        poll_interval=999,
        eject_on_success=False,
        udev_discovery=False,
        compatibility_check=True,
        simulate=True,
    )
    database = Database(settings.database_url)
    database.initialize()
    settings.library_root.mkdir(parents=True)
    settings.staging_root.mkdir(parents=True)
    service = RipperService(settings, database, SimulationBackend())

    await service.poll_once()
    running = list(service._tasks.values())
    await asyncio.gather(*running)

    overview = database.overview()
    assert len(overview["active_jobs"]) == 1
    job = overview["active_jobs"][0]
    assert job["status"] == "needs_review"
    detail = database.job_detail(job["id"])
    assert detail is not None
    selected = [title["id"] for title in detail["titles"] if title["selected"]]

    published = await service.update_metadata(
        job["id"],
        media_type="movie",
        title="Dune Part Two",
        year=2024,
        season=None,
        episode_start=None,
        edition="",
        selected_title_ids=selected,
    )
    assert published["status"] == "complete"
    destination = settings.movie_root / "Dune Part Two (2024)" / "Dune Part Two (2024).mkv"
    assert destination.is_file()


@pytest.mark.asyncio
async def test_open_tray_refuses_active_drive(tmp_path: Path) -> None:
    settings = Settings(
        library_root=tmp_path / "library",
        movie_root=tmp_path / "library" / "Movies",
        tv_root=tmp_path / "library" / "TV",
        database_url=f"sqlite:///{tmp_path / 'service.db'}",
        eject_on_success=False,
        udev_discovery=False,
        compatibility_check=False,
        simulate=True,
    )
    database = Database(settings.database_url)
    database.initialize()
    database.upsert_drive(
        {
            "id": "drive-demo-pioneer",
            "disc_index": 0,
            "name": "Pioneer",
            "device": "/dev/sr0",
            "disc_name": "MOVIE",
            "state": "ripping",
            "status_text": "Ripping",
        }
    )
    database.create_job(
        {
            "id": "job-active",
            "drive_id": "drive-demo-pioneer",
            "disc_name": "MOVIE",
            "fingerprint": "active",
            "title": "Movie",
            "status": "ripping",
        }
    )
    service = RipperService(settings, database, SimulationBackend())

    with pytest.raises(ValueError, match="active ingest"):
        await service.eject_drive("drive-demo-pioneer")


@pytest.mark.asyncio
async def test_queue_drive_reuses_existing_active_job(tmp_path: Path) -> None:
    settings = Settings(
        library_root=tmp_path / "library",
        movie_root=tmp_path / "library" / "Movies",
        tv_root=tmp_path / "library" / "TV",
        database_url=f"sqlite:///{tmp_path / 'service.db'}",
        udev_discovery=False,
        compatibility_check=False,
        simulate=True,
    )
    database = Database(settings.database_url)
    database.initialize()
    drive = (await SimulationBackend().list_drives())[0]
    database.upsert_drive(drive.to_dict())
    database.create_job(
        {
            "id": "job-active",
            "drive_id": drive.id,
            "disc_name": drive.disc_name,
            "fingerprint": "active",
            "title": drive.disc_name,
            "status": "scanning",
        }
    )
    service = RipperService(settings, database, SimulationBackend())

    assert service.queue_drive(drive) == "job-active"
    assert database.fetchone("SELECT COUNT(*) AS count FROM jobs") == {"count": 1}
