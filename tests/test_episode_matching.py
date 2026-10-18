import errno
from pathlib import Path

import pytest
from test_autorip import finish, make_service

from openripper.db import Database
from openripper.episodes import validate_assignments
from openripper.makemkv import TitleInfo


async def staged_tv(tmp_path, **settings):
    service = make_service(tmp_path, **settings)
    service.backend.label = "EXAMPLE_SHOW_S1_D1"
    await service.poll_once()
    await finish(service)
    job = service.database.overview()["active_jobs"][0]
    return service, service.database.job_detail(job["id"])


async def match(service, job, assignments, selected=None):
    return await service.update_metadata(
        job["id"],
        media_type="tv",
        title="Example Show",
        year=2024,
        season=None,
        episode_start=None,
        edition="",
        selected_title_ids=selected,
        episode_assignments=assignments,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disc", "library"])
async def test_tv_rip_waits_then_publishes_matched_range_and_frees_drive(tmp_path, mode):
    service, job = await staged_tv(tmp_path, output_mode=mode)
    assert job["status"] == "needs_review" and job["progress"] == 1
    assert not list(service.settings.tv_root.rglob("*.mkv"))
    assert service.overview()["runtime"]["slots_in_use"] == 0
    # Reopening the database does not lose the waiting job.
    reopened = Database(service.settings.database_url)
    assert reopened.job_detail(job["id"])["status"] == "needs_review"
    completed = await match(
        service,
        job,
        [
            dict(
                title_id=job["titles"][0]["id"],
                season=0,
                episode=2,
                episode_end=3,
                episode_title="The beginning",
            )
        ],
    )
    assert completed["status"] == "complete"
    output = Path(completed["titles"][0]["ripped_path"])
    assert output.name == "Example Show - S00E02-E03 - The beginning.mkv"
    assert output.parent.name == "Season 00"
    assert output.read_bytes() == b"test video contents"
    assert not list(output.parent.glob("*.pending"))
    assert reopened.job_detail(job["id"])["episode_assignments"][0]["episode_end"] == 3


@pytest.mark.asyncio
async def test_tv_disc_completion_cannot_bypass_matching(tmp_path):
    service, job = await staged_tv(tmp_path)
    await service.complete_disc(job["id"])
    assert service.database.job_detail(job["id"])["status"] == "needs_review"
    with pytest.raises(ValueError, match="Match every selected"):
        await service.publish_job(job["id"])
    assert Path(job["titles"][0]["ripped_path"]).is_file()


@pytest.mark.parametrize(
    "assignments",
    [
        [],
        [dict(title_id=1, season=1, episode=1)],
        [dict(title_id=1, season=1, episode=1), dict(title_id=1, season=1, episode=2)],
        [dict(title_id=1, season=1, episode=1), dict(title_id=2, season=1, episode=1)],
        [
            dict(title_id=1, season=1, episode=1, episode_end=3),
            dict(title_id=2, season=1, episode=3),
        ],
        [dict(title_id=1, season=1, episode=0), dict(title_id=2, season=1, episode=2)],
        [dict(title_id=1, season=-1, episode=1), dict(title_id=2, season=1, episode=2)],
        [
            dict(title_id=1, season=1, episode=3, episode_end=2),
            dict(title_id=2, season=1, episode=4),
        ],
        [dict(title_id=1, season=1, episode=1), dict(title_id=999, season=1, episode=2)],
    ],
)
def test_episode_mapping_rejects_missing_duplicate_and_invalid_assignments(assignments):
    with pytest.raises(ValueError):
        validate_assignments([dict(id=1, selected=True), dict(id=2, selected=True)], assignments)


@pytest.mark.asyncio
async def test_later_collision_keeps_all_staged_files_and_matching_is_retryable(tmp_path):
    service, job = await staged_tv(tmp_path)
    stage = Path(job["stage_path"])
    service.database.replace_titles(
        job["id"], [TitleInfo(index=i, selected=True).to_dict() for i in [0, 8]]
    )
    job = service.database.job_detail(job["id"])
    assignments = []
    for number, title in enumerate(job["titles"], 1):
        source = stage / f"title_t{title['title_index']:02d}.mkv"
        source.write_bytes(f"episode {number}".encode())
        service.database.execute(
            "UPDATE titles SET ripped_path=? WHERE id=?", (str(source), title["id"])
        )
        assignments.append(dict(title_id=title["id"], season=1, episode=number))
    collision = service.settings.tv_root / "Example Show (2024)" / "Season 01"
    collision.mkdir(parents=True)
    existing = collision / "Example Show - S01E02.mkv"
    existing.write_bytes(b"existing library episode")
    with pytest.raises(FileExistsError):
        await match(service, job, assignments)
    assert not (collision / "Example Show - S01E01.mkv").exists()
    assert existing.read_bytes() == b"existing library episode"
    assert len(list(stage.glob("title_t*.mkv"))) == 2
    pending = service.database.job_detail(job["id"])
    assert pending["status"] == "needs_review"
    assert len(pending["episode_assignments"]) == 2
    # Correct the mapping instead of overwriting an existing episode.
    assignments[1]["episode"] = 3
    result = await match(service, job, assignments)
    assert result["status"] == "complete"
    assert (collision / "Example Show - S01E03.mkv").read_bytes() == b"episode 2"


@pytest.mark.asyncio
async def test_rejected_mapping_does_not_change_metadata_or_selection(tmp_path):
    service, job = await staged_tv(tmp_path)
    with pytest.raises(ValueError):
        await match(service, job, [], selected=[])
    after = service.database.job_detail(job["id"])
    assert after["title"] == job["title"]
    assert after["titles"] == job["titles"]
    assert after["episode_assignments"] == []


@pytest.mark.asyncio
async def test_new_disc_can_use_drive_while_previous_tv_waits(tmp_path):
    service, job = await staged_tv(tmp_path)
    service.backend.label = "NEXT_MOVIE"
    await service.poll_once()
    await finish(service)
    assert service.database.job_detail(job["id"])["status"] == "needs_review"
    assert service.database.overview()["totals"]["completed"] == 1


@pytest.mark.asyncio
async def test_tv_custom_template_keeps_episode_identifier(tmp_path):
    service, job = await staged_tv(tmp_path, tv_name_template="{title}")
    result = await match(
        service,
        job,
        [
            dict(
                title_id=job["titles"][0]["id"],
                season=1,
                episode=8,
            )
        ],
    )
    assert Path(result["titles"][0]["ripped_path"]).name == "Example Show - S01E08.mkv"


@pytest.mark.asyncio
async def test_cross_filesystem_copy_keeps_original_until_promotion(tmp_path, monkeypatch):
    service, job = await staged_tv(tmp_path)
    source = Path(job["titles"][0]["ripped_path"])
    original_rename = Path.rename

    def cross_device(path, target):
        if path == source:
            raise OSError(errno.EXDEV, "Cross-device rename")
        assert source.exists(), "Original must survive until the copied file is promoted"
        assert path.suffix == ".pending"
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", cross_device)
    result = await match(
        service,
        job,
        [
            dict(
                title_id=job["titles"][0]["id"],
                season=1,
                episode=2,
            )
        ],
    )
    assert result["status"] == "complete"
    assert not source.exists()
    assert Path(result["titles"][0]["ripped_path"]).read_bytes() == b"test video contents"


@pytest.mark.asyncio
async def test_metadata_api_requires_assignments_and_accepts_typed_mapping(tmp_path):
    import httpx

    from openripper.api import create_app

    service, job = await staged_tv(tmp_path)
    app = create_app(service.settings, service.backend)
    payload = dict(media_type="tv", title="Example Show", year=2024)
    route = f"/api/jobs/{job['id']}/metadata"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.post(route, json=payload)
        assert missing.status_code == 409
        assert "Match every selected" in missing.json()["detail"]
        payload["episode_assignments"] = [
            dict(
                title_id=job["titles"][0]["id"],
                season=1,
                episode=8,
            )
        ]
        completed = await client.post(route, json=payload)
        assert completed.status_code == 200
        assert completed.json()["status"] == "complete"
        assert "S01E08" in completed.json()["titles"][0]["ripped_path"]
