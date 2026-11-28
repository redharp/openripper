from pathlib import Path

import pytest
from test_autorip import finish, make_service

from openripper.makemkv import TitleInfo
from openripper.metadata import MetadataMatch


async def rip_movies(tmp_path, monkeypatch, durations, names=None, **settings):
    service = make_service(tmp_path, output_mode="library", rip_mode="all", **settings)

    async def scan(device):
        return [
            TitleInfo(
                index=index,
                name=names[index] if names else f"Track {index}",
                duration_seconds=duration,
            )
            for index, duration in enumerate(durations)
        ]

    async def rip(device, title_index, destination, progress):
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"raw_title_{title_index}.mkv"
        path.write_bytes(f"video-{title_index}".encode())
        await progress(1, "Done")
        return path

    async def metadata(disc_name, titles, *, media_type=None):
        return MetadataMatch("Example", 2020, confidence=0.99, source="tmdb")

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    monkeypatch.setattr(service.backend, "rip_title", rip)
    monkeypatch.setattr(service.metadata, "resolve", metadata)
    await service.poll_once()
    await finish(service)
    job = service.database.fetchone("SELECT id FROM jobs")
    return service, service.database.job_detail(job["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("durations", [[7200, 6900], [8500, 4800], [3000, 2900]])
async def test_multiple_movie_features_remain_outside_library(tmp_path, monkeypatch, durations):
    service, job = await rip_movies(tmp_path, monkeypatch, durations)
    assert job["status"] == "needs_review"
    assert "Multiple possible movie features" in job["error"]
    assert job["progress"] == 1
    assert not job["final_path"]
    assert not list(service.settings.movie_root.rglob("*.mkv"))
    assert not Path(job["stage_path"]).is_relative_to(service.settings.movie_root)
    originals = {t["ripped_path"]: Path(t["ripped_path"]).read_bytes() for t in job["titles"]}
    with pytest.raises(ValueError, match="Multiple possible movie features"):
        await service.publish_job(job["id"])
    assert all(Path(p).read_bytes() == data for p, data in originals.items())

    selected = job["titles"][0]
    unselected = job["titles"][1]
    result = await service.update_metadata(
        job["id"], media_type="movie", title="Example", year=2020,
        season=None, episode_start=None, edition="", selected_title_ids=[selected["id"]],
    )
    assert result["status"] == "complete"
    assert (service.settings.movie_root / "Example (2020)" / "Example (2020).mkv").is_file()
    assert Path(unselected["ripped_path"]).read_bytes() == originals[unselected["ripped_path"]]


@pytest.mark.asyncio
async def test_movie_with_short_extra_can_publish(tmp_path, monkeypatch):
    service, job = await rip_movies(tmp_path, monkeypatch, [7200, 300], include_extras=True)
    assert job["status"] == "complete"
    folder = service.settings.movie_root / "Example (2020)"
    assert (folder / "Example (2020).mkv").read_bytes() == b"video-0"
    assert (folder / "Extras" / "Track 1.mkv").read_bytes() == b"video-1"


@pytest.mark.asyncio
async def test_movie_extras_with_equivalent_names_get_distinct_files(tmp_path, monkeypatch):
    service, job = await rip_movies(
        tmp_path, monkeypatch, [7200, 300, 400], names=["Feature", "Bonus", "BONUS"],
        include_extras=True,
    )
    assert job["status"] == "complete", job["error"]
    folder = service.settings.movie_root / "Example (2020)"
    assert (folder / "Example (2020).mkv").read_bytes() == b"video-0"
    assert (folder / "Extras" / "Bonus - t01.mkv").read_bytes() == b"video-1"
    assert (folder / "Extras" / "BONUS - t02.mkv").read_bytes() == b"video-2"


@pytest.mark.asyncio
async def test_blocked_automatic_publication_can_be_reviewed_without_disc(tmp_path, monkeypatch):
    existing = tmp_path / "library" / "Movies" / "Example (2020)" / "Example (2020).mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing movie")

    service, job = await rip_movies(tmp_path, monkeypatch, [7200])
    assert job["status"] == "needs_review"
    assert "Refusing to overwrite" in job["error"]
    service.backend.label = ""
    await service.poll_once()
    result = await service.update_metadata(
        job["id"], media_type="movie", title="Example", year=2020,
        season=None, episode_start=None, edition="Director's Cut", selected_title_ids=None,
    )
    assert result["status"] == "complete"
    assert existing.read_bytes() == b"existing movie"
    assert Path(result["titles"][0]["ripped_path"]).read_bytes() == b"video-0"


@pytest.mark.asyncio
async def test_movie_staging_uses_drive_override_parent(tmp_path, monkeypatch):
    override = tmp_path / "other-drive"
    service, job = await rip_movies(
        tmp_path, monkeypatch, [7200, 6900], drive_paths={"drive-test": str(override)}
    )
    assert Path(job["stage_path"]).parent == override / ".openripper-staging"
    assert not list((override / "Movies").rglob("*.mkv"))


@pytest.mark.asyncio
async def test_confident_movie_rips_directly_to_destination_without_staging(tmp_path, monkeypatch):
    ripped_into = []
    service = make_service(tmp_path, output_mode="library", rip_mode="all")

    async def scan(device):
        return [TitleInfo(index=0, name="Feature", duration_seconds=7200)]

    async def rip(device, title_index, destination, progress):
        ripped_into.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / "raw_title_0.mkv"
        path.write_bytes(b"video-0")
        await progress(1, "Done")
        return path

    async def metadata(disc_name, titles, *, media_type=None):
        return MetadataMatch("Example", 2020, confidence=0.99, source="tmdb")

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    monkeypatch.setattr(service.backend, "rip_title", rip)
    monkeypatch.setattr(service.metadata, "resolve", metadata)
    await service.poll_once()
    await finish(service)
    job = service.database.job_detail(service.database.fetchone("SELECT id FROM jobs")["id"])
    expected_dir = service.settings.movie_root / "Example (2020)"
    assert job["status"] == "complete", job["error"]
    assert ripped_into == [expected_dir]
    assert not service.settings.staging_root.exists()
    assert (expected_dir / "Example (2020).mkv").read_bytes() == b"video-0"


def test_disc_folder_movie_staging_remains_on_destination_filesystem(tmp_path):
    service = make_service(tmp_path, output_mode="disc")
    root = tmp_path / "mounted-movie-root"
    service.settings.movie_root = root
    assert service.staging_folder("drive-test", "movie", "job") == (
        root / ".openripper-staging" / "job"
    )
    service.settings.drive_paths = {"drive-test": str(tmp_path / "mounted-drive")}
    assert service.staging_folder("drive-test", "movie", "job") == (
        tmp_path / "mounted-drive" / "Movies" / ".openripper-staging" / "job"
    )
