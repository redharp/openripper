from pathlib import Path

import pytest
from test_autorip import finish, make_service

from openripper.episodes import automatic_episode_assignments, has_explicit_episode
from openripper.makemkv import TitleInfo
from openripper.metadata import MetadataMatch


def title(title_id, name, *, selected=True, duplicate_of=None):
    return {
        "id": title_id,
        "name": name,
        "selected": selected,
        "duplicate_of": duplicate_of,
    }


def test_automatic_episode_matching_uses_embedded_codes_instead_of_track_order():
    rows = [
        title(10, "Example Show S02E08-E10"),
        title(20, "Example Show Season 2 Episode 6"),
        title(30, "Example Show S02E04E05"),
        title(40, "Featurette", selected=False),
        title(50, "Example Show S02E08-E10", selected=False, duplicate_of=0),
    ]
    assert automatic_episode_assignments(rows, season=2, disc_name="EXAMPLE_S02_D2") == [
        {"title_id": 10, "season": 2, "episode": 8, "episode_end": 10},
        {"title_id": 20, "season": 2, "episode": 6, "episode_end": 6},
        {"title_id": 30, "season": 2, "episode": 4, "episode_end": 5},
    ]


@pytest.mark.parametrize(
    "ambiguous",
    [
        "Title 3",
        "Episode 3",
        "Example Show S01E02",
        "Example Show S02E05-E07",
        "Example Show S02E02 S02E03",
        "Example Show S02E02E04",
        "Example Show S02E02 - E04",
        "Example Show S02E03-E02",
        "Example Show S02E00",
        "Example Show S02E02D1",
    ],
)
def test_automatic_episode_matching_requires_every_selected_file(ambiguous):
    rows = [title(10, "Example Show S02E07-E08"), title(20, ambiguous)]
    assert automatic_episode_assignments(rows, season=2, disc_name="EXAMPLE_S02_D2") == []


def test_filtered_out_named_episode_still_requires_review():
    rows = [
        title(10, "Example Show S02E01"),
        title(20, "Example Show S02E02", selected=False),
    ]
    assert automatic_episode_assignments(rows, season=2) == []


def test_volume_label_is_not_independent_episode_evidence():
    rows = [title(10, "Example Show S02E01 - Main Feature")]
    assert automatic_episode_assignments(rows, disc_name="EXAMPLE_SHOW_S02E01") == []
    assert not has_explicit_episode(rows[0]["name"], disc_name="EXAMPLE_SHOW_S02E01")


def test_explicit_episode_selection_disagrees_with_conflicting_season():
    assert has_explicit_episode("Example Show S02E01-E02", season=2)
    assert not has_explicit_episode("Example Show S02E01-E02", season=3)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "confidence, second_name, second_duration, publishes",
    [
        (0.99, "Example Show S02E02", 1510, True),
        (0.99, "Example Show S02E02", 600, True),
        (0.99, "Title 7", 1510, False),
        (0.48, "Example Show S02E02", 1510, False),
    ],
)
async def test_disc_ingest_publishes_only_verified_episode_sets(
    tmp_path, monkeypatch, confidence, second_name, second_duration, publishes
):
    service = make_service(tmp_path, output_mode="library")
    service.backend.label = "EXAMPLE_SHOW_S02_D2"

    async def scan(device):
        return [
            TitleInfo(index=2, name="Example Show S02E04-E05", duration_seconds=1500),
            TitleInfo(index=7, name=second_name, duration_seconds=second_duration),
        ]

    async def resolve(disc_name, titles, *, media_type=None):
        if confidence >= 0.88:
            return MetadataMatch("Example Show", 2024, "tv", confidence, "tmdb", "42")
        return MetadataMatch("Example Show", None, "tv", confidence, "season marker")

    async def rip(device, title_index, destination, progress):
        output = destination / f"Title {title_index}.mkv"
        output.write_bytes(f"test title {title_index}".encode())
        return output

    monkeypatch.setattr(service.backend, "scan_disc", scan)
    monkeypatch.setattr(service.metadata, "resolve", resolve)
    monkeypatch.setattr(service.backend, "rip_title", rip)

    await service.poll_once()
    await finish(service)
    snapshot = service.database.overview()
    if not publishes:
        (pending,) = snapshot["active_jobs"]
        assert pending["status"] == "needs_review"
        assert not list(service.settings.tv_root.rglob("*.mkv"))
        assert len(list(Path(pending["stage_path"]).glob("*.mkv"))) == 2
        return

    (completed,) = snapshot["history"]
    assert completed["status"] == "complete"
    season = service.settings.tv_root / "Example Show (2024)" / "Season 02"
    assert (season / "Example Show - S02E02.mkv").read_bytes() == b"test title 7"
    assert (season / "Example Show - S02E04-E05.mkv").read_bytes() == b"test title 2"
    detail = service.database.job_detail(completed["id"])
    assert [(a["episode"], a["episode_end"]) for a in detail["episode_assignments"]] == [
        (4, 5),
        (2, 2),
    ]
