import httpx
import pytest

from openripper.makemkv import TitleInfo
from openripper.metadata import MetadataResolver, disc_media_type, disc_season, episode_structure


@pytest.mark.parametrize(
    "label", ["EXAMPLE_S1_D1", "EXAMPLE_S02D02", "EXAMPLE_SEASON_3", "EXAMPLE_S01E01"]
)
def test_season_markers_are_tv(label):
    assert disc_media_type(label) == "tv"
    assert disc_season(label) is not None


def test_episode_cluster_does_not_mistake_feature_variants_or_bonus_material():
    def titles(durations):
        return [TitleInfo(index=i, duration_seconds=d) for i, d in enumerate(durations)]

    assert episode_structure(titles([2400, 2450, 2500]))
    assert not episode_structure(titles([7200, 2400, 2450, 2500]))
    assert not episode_structure(titles([4800, 4800, 4800]))
    assert not episode_structure(titles([1200, 2000, 5000]))


@pytest.mark.asyncio
async def test_episode_names_identify_tv_without_a_season_label():
    match = await MetadataResolver().resolve(
        "EXAMPLE_SHOW",
        [
            TitleInfo(index=0, name="Episode 1", duration_seconds=2700),
            TitleInfo(index=1, name="Episode 2", duration_seconds=2900),
        ],
    )
    assert match.media_type == "tv"
    assert match.source == "episode structure"


@pytest.mark.asyncio
async def test_tv_metadata_lookup_and_ambiguous_names(monkeypatch):
    results = [
        {"id": 1, "media_type": "tv", "name": "Example Show", "first_air_date": "2020-01-01"}
    ]
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": results})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "openripper.metadata.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    resolver = MetadataResolver("test-token")
    match = await resolver.resolve("EXAMPLE_SHOW_S02_D1")
    assert match.media_type == "tv"
    assert match.title == "Example Show"
    assert match.year == 2020
    assert requests[0].url.path == "/3/search/multi"
    assert requests[0].url.params["query"] == "Example Show"
    results.append(
        {"id": 2, "media_type": "movie", "title": "Example Show", "release_date": "2021-01-01"}
    )
    ambiguous = await resolver.resolve("EXAMPLE_SHOW")
    assert ambiguous.source == "disc-label"
    assert ambiguous.confidence < 0.88


@pytest.mark.parametrize(
    "label, season",
    [("Example Season One Disc 1", 1), ("EXAMPLE_SEASON_TWO_D2", 2), ("EXAMPLE_S00_D1", 0)],
)
def test_word_seasons_and_specials(label, season):
    assert disc_media_type(label) == "tv"
    assert disc_season(label) == season


@pytest.mark.asyncio
async def test_scan_title_season_identifies_tv_when_volume_label_is_generic():
    titles = [
        TitleInfo(index=i, name="Example Show Season One Disc 2", duration_seconds=2500 + i)
        for i in range(2)
    ]
    match = await MetadataResolver().resolve("BLURAY_DISC", titles)
    assert match.media_type == "tv"
    assert match.title == "Example Show"
    assert disc_season("BLURAY_DISC", titles) == 1
    titles[1].name = "Example Show Season Two Disc 1"
    assert disc_season("BLURAY_DISC", titles) is None
    assert (await MetadataResolver().resolve("BLURAY_DISC", titles)).media_type == "movie"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label",
    [
        "HARRY_POTTER_CHAMBER_SECRETS",
        "Harry Potter_DeathlyHallowsP1",
        "Harry Potter_DeathlyHallowsP2",
        "Harry_Potter_Half_Blood_Prince",
    ],
)
async def test_movie_disc_names_with_parts_are_not_tv(label):
    titles = [
        TitleInfo(index=i, duration_seconds=duration)
        for i, duration in enumerate([8502, 1200, 1210, 1220])
    ]
    match = await MetadataResolver().resolve(label, titles)
    assert match.media_type == "movie"
    assert disc_season(label, titles) is None
