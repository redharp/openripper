import httpx
import pytest

from openripper.arr import ArrConfig
from openripper.makemkv import TitleInfo
from openripper.metadata import (
    MetadataResolver,
    disc_media_type,
    disc_season,
    episode_structure,
    resolve_library_movie,
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, year", [("movie", 2021), ("tv", 2020)])
async def test_manual_category_limits_metadata_matches(monkeypatch, kind, year):
    mock_metadata(monkeypatch, [
        {"id": 1, "media_type": "tv", "name": "Example", "first_air_date": "2020-01-01"},
        {"id": 2, "media_type": "movie", "title": "Example", "release_date": "2021-01-01"},
    ])
    match = await MetadataResolver("test").resolve("EXAMPLE", media_type=kind)
    assert (match.media_type, match.year, match.source) == (kind, year, "tmdb")


@pytest.mark.asyncio
async def test_generic_volume_uses_a_single_named_feature(monkeypatch):
    requests = mock_metadata(monkeypatch, [
        {"id": 2, "media_type": "movie", "title": "Example Movie", "release_date": "2021-01-01"},
    ])
    titles = [
        TitleInfo(index=0, name="Example Movie", duration_seconds=7200),
        TitleInfo(index=1, name="MainFeature", duration_seconds=7200),
        TitleInfo(index=2, name="Making Of", duration_seconds=400),
    ]
    match = await MetadataResolver("test").resolve("BLURAY_DISC", titles)
    assert requests[-1].url.params["query"] == "Example Movie"
    assert (match.title, match.year, match.source) == ("Example Movie", 2021, "tmdb")
    titles[1].name = "Another Film"
    match = await MetadataResolver("test").resolve("BLURAY_DISC", titles)
    assert match.source == "disc-label"


@pytest.mark.asyncio
async def test_mastering_suffix_requires_corroborating_feature_name():
    resolver = MetadataResolver()
    feature = TitleInfo(index=0, name="Example Film", duration_seconds=5430)
    match = await resolver.resolve("Example_Film_UPK76", [feature])
    assert (match.title, match.year, match.source) == ("Example Film", None, "disc identity")
    assert match.confidence >= 0.88

    feature.name = "Other Film"
    match = await resolver.resolve("Example_Film_UPK76", [feature])
    assert match.confidence < 0.88


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label, feature, duration, expected",
    [
        ("DUNE_PART_TWO_2024_UHD", "MainFeature", 9540, ("Dune Part Two", 2024)),
        ("EXAMPLE_MOVIE", "Example Movie", 7200, ("Example Movie", None)),
        ("EXAMPLE_MOVIE", "Example Movie (2020)", 7200, ("Example Movie", 2020)),
        ("DVD_VIDEO", "Example Movie (2020)", 7200, ("Example Movie", 2020)),
        ("EXAMPLE_MOVIE_2020", "Other Feature", 7200, None),
        ("EXAMPLE_MOVIE_2020", "Example Movie (2018)", 7200, None),
        ("EXAMPLE_MOVIE", "MainFeature", 7200, None),
        ("DVD_VIDEO", "Example Movie", 7200, None),
        ("DVD_VIDEO", "00001.mpls", 7200, None),
        ("EXAMPLE_MOVIE_2020", "Example Movie", 1800, None),
    ],
)
async def test_offline_movie_identity_requires_evidence(label, feature, duration, expected):
    resolver = MetadataResolver()
    match = await resolver.resolve(
        label, [TitleInfo(index=0, name=feature, duration_seconds=duration)]
    )
    if expected is None:
        assert match.confidence < 0.88
    else:
        assert match.media_type == "movie"
        assert (match.title, match.year) == expected
        assert match.source == "disc identity"
        assert match.confidence >= 0.88


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "episode_titles, confident",
    [
        (["Example Show - S01E01", "EXAMPLE_SHOW_S01E02"], True),
        (["S01E01", "S01E02"], False),
        (["Example Show - S01E01", "Another Show - S01E02"], False),
        (["Example Show (2021) S01E01", "Example Show S01E02"], False),
    ],
)
async def test_offline_show_identity_requires_corroboration(episode_titles, confident):
    episodes = [
        TitleInfo(index=index, name=name, duration_seconds=2700)
        for index, name in enumerate(episode_titles)
    ]
    match = await MetadataResolver().resolve("EXAMPLE_SHOW_2022_S01_D01", episodes)
    assert (match.media_type, match.title, match.year) == ("tv", "Example Show", 2022)
    assert (match.confidence >= 0.88) is confident


@pytest.mark.asyncio
async def test_offline_show_identity_does_not_invent_first_air_year():
    episodes = [TitleInfo(index=0, name="Example Show S01E01", duration_seconds=2700)]
    match = await MetadataResolver().resolve("EXAMPLE_SHOW_S01_D01", episodes)
    assert (match.media_type, match.title, match.year) == ("tv", "Example Show", None)
    assert match.confidence >= 0.88


@pytest.mark.asyncio
async def test_successful_but_ambiguous_catalog_lookup_holds_local_identity(monkeypatch):
    mock_metadata(monkeypatch, [
        {"id": 1, "media_type": "movie", "title": "Example", "release_date": "2020-01-01"},
        {"id": 2, "media_type": "movie", "title": "Example", "release_date": "2020-02-01"},
    ])
    match = await MetadataResolver("test").resolve(
        "EXAMPLE_2020",
        [TitleInfo(index=0, name="Example", duration_seconds=7200)],
    )
    assert match.confidence < 0.88


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label, feature, duration, other_title, result_year, queries",
    [
        ("EXAMPLE_MOVIE", "MainFeature", 5400, None, 2021, 1),
        ("EXAMPLE_MOVIE_XYZ42", "Example Movie", 5400, None, 2021, 1),
        ("EXAMPLE_MOVIE_XYZ42", "EXAMPLE_MOVIE_XYZ42", 5400, None, 2021, 1),
        ("EXAMPLE_MOVIE_2021_XYZ42", "MainFeature", 5400, "Example Movie", 2021, 1),
        ("EXAMPLE_MOVIE_2022_XYZ42", "MainFeature", 5400, None, None, 1),
        ("EXAMPLE_MOVIE_XYZ42", "MainFeature", 5400, "Example Movie", None, 1),
        ("EXAMPLE_MOVIE_XYZ42", "MainFeature", 5400, "Example Movie XYZ42", None, 1),
        ("EXAMPLE_MOVIE_XYZ42", "MainFeature", 7000, None, None, 1),
        ("EXAMPLE_MOVIE_XYZ42", "A different film", 5400, None, None, 0),
        ("EXAMPLE_MOVIE_42", "MainFeature", 5400, None, None, 1),
        ("EXAMPLE_MOVIE_S02_D01", "MainFeature", 5400, None, None, 0),
    ],
)
async def test_managed_movie_lookup(
    monkeypatch, label, feature, duration, other_title, result_year, queries
):
    movies = [{"id": 1, "title": "Example Movie", "year": 2021, "runtime": 90}]
    if other_title:
        movies.append({"id": 2, "title": other_title, "year": 1990, "runtime": 120})
    calls = []

    async def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=movies)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "openripper.arr.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    match = await resolve_library_movie(
        label,
        [TitleInfo(index=0, name=feature, duration_seconds=duration)],
        ArrConfig(url="https://catalog.example.test", api_key="test-key"),
    )
    assert calls == [("GET", "/api/v3/movie")] * queries
    if result_year is None:
        assert match is None
    else:
        assert match is not None
        assert (match.title, match.year) == ("Example Movie", result_year)
        assert match.media_type == "movie"
        assert match.source == "radarr"
        assert match.confidence >= 0.88


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [502, "timeout", "incomplete"])
async def test_managed_movie_lookup_returns_no_match_without_valid_evidence(monkeypatch, error):
    async def respond(request):
        if error == "timeout":
            raise TimeoutError("lookup exceeded budget")
        if error == "incomplete":
            return httpx.Response(200, json=[{"id": 1, "title": "Example Movie", "year": 2021}])
        return httpx.Response(error)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "openripper.arr.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    assert await resolve_library_movie(
        "EXAMPLE_MOVIE_XYZ42",
        [TitleInfo(index=0, name="MainFeature", duration_seconds=5400)],
        ArrConfig(url="https://catalog.example.test", api_key="test-key"),
    ) is None


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


def mock_metadata(monkeypatch, results):
    requests = []

    async def respond(request):
        requests.append(request)
        if request.url.host == "image.tmdb.org":
            return httpx.Response(200, content=b"\xff\xd8test-poster")
        return httpx.Response(200, json={"results": results})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "openripper.metadata.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("second_poster", ["/remake.jpg", None])
async def test_ambiguous_remake_does_not_write_a_poster(monkeypatch, tmp_path, second_poster):
    requests = mock_metadata(monkeypatch, [
        {"id": 1, "title": "Example", "release_date": "1984-01-01", "poster_path": "/old.jpg"},
        {"id": 2, "title": "Example", "release_date": "2020-01-01", "poster_path": second_poster},
    ])
    assert not await MetadataResolver("test").save_poster(tmp_path, "Example", None, "movie")
    assert not (tmp_path / "poster.jpg").exists()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_poster_uses_confirmed_release_year(monkeypatch, tmp_path):
    requests = mock_metadata(monkeypatch, [
        {"id": 1, "title": "Example", "release_date": "1984-01-01", "poster_path": "/old.jpg"},
        {"id": 2, "title": "Example", "release_date": "2020-01-01", "poster_path": "/new.jpg"},
    ])
    assert await MetadataResolver("test").save_poster(tmp_path, "Example", 2020, "movie")
    assert requests[-1].url.path == "/t/p/w500/new.jpg"
    assert (tmp_path / "poster.jpg").read_bytes() == b"\xff\xd8test-poster"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["id", "release_date"])
async def test_incomplete_search_result_cannot_autopublish(monkeypatch, missing):
    candidate = {"id": 1, "media_type": "movie", "title": "Example", "release_date": "2020-01-01"}
    candidate.pop(missing)
    mock_metadata(monkeypatch, [candidate])
    match = await MetadataResolver("test").resolve("EXAMPLE_2020")
    assert match.source == "disc-label"
    assert match.confidence < 0.88


@pytest.mark.asyncio
async def test_movie_search_cannot_override_episode_structure(monkeypatch):
    mock_metadata(monkeypatch, [
        {"id": 1, "media_type": "movie", "title": "Example", "release_date": "2020-01-01"}
    ])
    titles = [TitleInfo(index=i, duration_seconds=d) for i, d in enumerate([2400, 2450, 2500])]
    match = await MetadataResolver("test").resolve("EXAMPLE", titles)
    assert match.media_type == "tv"
    assert match.source == "episode structure"


@pytest.mark.asyncio
async def test_empty_search_results_keep_disc_identity(monkeypatch):
    mock_metadata(monkeypatch, None)
    match = await MetadataResolver("test").resolve("1917")
    assert match.title == "1917"
    assert match.year is None
    assert match.source == "disc-label"
