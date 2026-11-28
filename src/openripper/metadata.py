from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from .arr import ArrClient, ArrConfig, ArrError, normalized_title
from .makemkv import TitleInfo
from .naming import title_from_disc_label

SEASON_WORDS = {
    word: index
    for index, word in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        ]
    )
}
SEASON_MARKER = re.compile(
    r"(?:^|[\s_.-])(?:S(?P<short>\d{1,3})(?:E\d+)?(?:D\d+)?|"
    r"SEASON[\s_.-]*(?P<long>\d+|" + "|".join(SEASON_WORDS) + r"))(?=$|[\s_.-])",
    re.I,
)
GENERIC_DISC_NAME = re.compile(
    r"(?:dvd|blu\s*ray|bd|bdmv|uhd|avchd|video|disc|disk|volume|main\s*feature|"
    r"feature|movie|title|track|untitled|unknown|default)"
    r"(?:\s*(?:video|rom|ts|disc|disk|movie|feature|\d+))*"
    r"|\d{3,}(?:\s+(?:mpls|m2ts|vob|mkv))?"
)
MASTERING_SUFFIX = re.compile(r"[_-][A-Z]{2,5}[0-9]{2,5}$")


def disc_season(disc_name: str, titles=()) -> int | None:
    match = SEASON_MARKER.search(disc_name)
    if match:
        value = (match.group("short") or match.group("long")).lower()
        return int(value) if value.isdigit() else SEASON_WORDS[value]
    seasons = {disc_season(t.name) for t in titles} - {None}
    return seasons.pop() if len(seasons) == 1 else None


def disc_title(disc_name: str) -> tuple[str, int | None]:
    # Strip disc/season suffixes for lookup, keeping the original label on the job.
    marker = SEASON_MARKER.search(disc_name)
    label = disc_name[: marker.start()] if marker else disc_name
    return title_from_disc_label(label or disc_name)


def disc_media_type(disc_name: str) -> str:
    """Season markers identify TV even before a scan is requested."""
    return "tv" if SEASON_MARKER.search(disc_name) else "movie"


def episode_structure(titles) -> bool:
    episodes = [t for t in titles if 900 <= t.duration_seconds <= 5400]
    named = [t for t in episodes if re.search(r"\b(?:episode\s*\d+|s\d+e\d+)\b", t.name, re.I)]
    if len(named) >= 2:
        return True
    # Equal-length feature variants alone are not an episode cluster. Require
    # at least three distinct durations, no long movie, and a close running time.
    durations = sorted({t.duration_seconds for t in episodes})
    return (
        len(durations) >= 3
        and max(durations) <= min(durations) * 1.25
        and not any(t.duration_seconds > 5400 for t in titles)
    )


def generic_disc_name(name: str) -> bool:
    normalized = re.sub(r"[\W_]+", " ", name.casefold()).strip()
    return not normalized or GENERIC_DISC_NAME.fullmatch(normalized) is not None


def _title_key(name: str) -> str:
    return "".join(char for char in name.casefold() if char.isalnum())


def _feature_names(titles) -> set[tuple[str, int | None]]:
    return {
        disc_title(title.name)
        for title in titles
        if title.duration_seconds >= 3600 and not generic_disc_name(title.name)
    }


def _local_identity(
    match: MetadataMatch,
    names: set[tuple[str, int | None]],
    *,
    volume_named: bool,
) -> MetadataMatch | None:
    if generic_disc_name(match.title) or not any(char.isalpha() for char in match.title):
        return None
    key = _title_key(match.title)
    if any(_title_key(name) != key for name, _ in names):
        return None
    years = {year for _, year in names if year}
    if match.year is not None:
        years.add(match.year)
    if len(years) > 1 or (not years and not (volume_named and names)):
        return None
    return MetadataMatch(
        title=match.title,
        year=next(iter(years), None),
        media_type=match.media_type,
        confidence=0.91,
        source="disc identity",
    )


@dataclass(slots=True)
class MetadataMatch:
    title: str
    year: int | None
    media_type: str = "movie"
    confidence: float = 0.0
    source: str = "disc-label"
    external_id: str | None = None


async def resolve_library_movie(
    disc_name: str, titles: Sequence[TitleInfo], config: ArrConfig
) -> MetadataMatch | None:
    """Use a unique, corroborated movie already managed by Radarr."""
    if not config.url or not config.api_key.get_secret_value():
        return None
    if disc_season(disc_name, titles) is not None or episode_structure(titles):
        return None
    longest = max((title.duration_seconds for title in titles), default=0)
    if longest < 3600:
        return None

    title, year = disc_title(disc_name)
    if generic_disc_name(title):
        return None
    label_keys = {normalized_title(title)}
    prefix = MASTERING_SUFFIX.sub("", disc_name)
    if prefix != disc_name:
        prefix_title, prefix_year = disc_title(prefix)
        if not generic_disc_name(prefix_title):
            label_keys.add(normalized_title(prefix_title))
            year = year or prefix_year

    # Named features can corroborate either form. A conflicting name or year
    # should reach review even if an exact catalog candidate exists.
    main_titles = [item for item in titles if item.duration_seconds >= max(3600, longest * 0.75)]
    feature_names = _feature_names(main_titles)
    if any(normalized_title(name) not in label_keys for name, _ in feature_names):
        return None
    years = {value for _, value in feature_names if value}
    if year:
        years.add(year)
    if len(years) > 1:
        return None
    year = next(iter(years), None)

    try:
        async with asyncio.timeout(5):
            movies = await ArrClient("radarr", config).request("GET", "movie")
    except (ArrError, TimeoutError):
        return None
    if not isinstance(movies, list):
        return None
    matches = [
        movie
        for movie in movies
        if isinstance(movie, dict)
        and isinstance(movie.get("title"), str)
        and normalized_title(movie["title"]) in label_keys
        and (not year or movie.get("year") == year)
    ]
    if len(matches) != 1:
        return None
    movie = matches[0]
    identity, release_year, runtime = (movie.get(key) for key in ("id", "year", "runtime"))
    if not (
        isinstance(identity, int)
        and identity > 0
        and isinstance(release_year, int)
        and release_year >= 1888
        and isinstance(runtime, int)
        and runtime > 0
    ):
        return None
    runtime_seconds = runtime * 60
    if abs(longest - runtime_seconds) > max(300, runtime_seconds * 0.1):
        return None
    return MetadataMatch(
        title=movie["title"],
        year=release_year,
        confidence=0.95,
        source="radarr",
        external_id=str(identity),
    )


async def resolve_library_series(
    disc_name: str, titles: Sequence[TitleInfo], config: ArrConfig
) -> MetadataMatch | None:
    """Use a unique TV series already managed by Sonarr."""
    if not config.url or not config.api_key.get_secret_value():
        return None
    title, year = disc_title(disc_name)
    if generic_disc_name(title):
        return None
    label_keys = {normalized_title(title)}
    prefix = MASTERING_SUFFIX.sub("", disc_name)
    if prefix != disc_name:
        prefix_title, prefix_year = disc_title(prefix)
        if not generic_disc_name(prefix_title):
            label_keys.add(normalized_title(prefix_title))
            year = year or prefix_year
    try:
        async with asyncio.timeout(5):
            series_list = await ArrClient("sonarr", config).request("GET", "series")
    except (ArrError, TimeoutError):
        return None
    if not isinstance(series_list, list):
        return None
    matches = [
        show
        for show in series_list
        if isinstance(show, dict)
        and isinstance(show.get("title"), str)
        and normalized_title(show["title"]) in label_keys
        and (not year or show.get("year") == year)
    ]
    if len(matches) != 1:
        return None
    show = matches[0]
    identity, release_year = show.get("id"), show.get("year")
    if not (isinstance(identity, int) and identity > 0):
        return None
    clean_year = release_year if isinstance(release_year, int) and release_year >= 1920 else year
    return MetadataMatch(
        title=show["title"],
        year=clean_year,
        media_type="tv",
        confidence=0.95,
        source="sonarr",
        external_id=str(identity),
    )


class MetadataResolver:
    def __init__(self, tmdb_token: str | None = None):
        self.tmdb_token = tmdb_token

    async def save_poster(
        self, folder: Path, title: str, year: int | None, media_type: str
    ) -> bool:
        if not self.tmdb_token or (folder / "poster.jpg").exists():
            return False
        kind = "tv" if media_type == "tv" else "movie"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"https://api.themoviedb.org/3/search/{kind}",
                headers={"Authorization": f"Bearer {self.tmdb_token}"},
                params={"query": title, "include_adult": "false"},
            )
            response.raise_for_status()
            candidates = {}
            for candidate in response.json().get("results") or []:
                name = candidate.get("name" if kind == "tv" else "title") or ""
                date = candidate.get("first_air_date" if kind == "tv" else "release_date") or ""
                poster = candidate.get("poster_path") or ""
                if SequenceMatcher(None, title.casefold(), name.casefold()).ratio() < 0.95:
                    continue
                if year and not date.startswith(str(year)):
                    continue
                identity = candidate.get("id")
                if not isinstance(identity, int) or identity <= 0:
                    continue
                candidates[identity] = poster
            # A title alone can name several remakes. Never make the first search
            # result into a local poster, which library scanners prefer thereafter.
            if len(candidates) != 1:
                return False
            poster = next(iter(candidates.values()))
            if not re.fullmatch(r"/[A-Za-z0-9_-]+\.jpg", poster):
                return False
            image = await client.get(f"https://image.tmdb.org/t/p/w780{poster}")
            image.raise_for_status()
            if len(image.content) > 8_000_000 or not image.content.startswith(b"\xff\xd8"):
                raise ValueError("Invalid poster image")
            with (folder / "poster.jpg").open("xb") as output:
                output.write(image.content)
            return True
        return False

    async def resolve(
        self, disc_name: str, titles=(), *, media_type: str | None = None
    ) -> MetadataMatch:
        title, year = disc_title(disc_name)
        kind = disc_media_type(disc_name)
        # Volume labels can be generic while MakeMKV's title names carry the season.
        if kind != "tv" and disc_season(disc_name, titles) is not None:
            named = [t.name for t in titles if disc_season(t.name) is not None]
            names = {disc_title(name) for name in named}
            if len(names) == 1:
                title, year = names.pop()
                kind = "tv"
        source = "season marker" if kind == "tv" else "disc-label"
        if kind != "tv" and episode_structure(titles):
            kind, source = "tv", "episode structure"
        if media_type:
            kind, source = media_type, "manual override"
        feature_names = _feature_names(titles) if kind == "movie" else set()
        if kind == "movie" and generic_disc_name(disc_name) and len(feature_names) == 1:
            title, year = next(iter(feature_names))
            source = "feature title"
        elif kind == "movie" and feature_names:
            prefix = MASTERING_SUFFIX.sub("", disc_name)
            prefix_title, prefix_year = disc_title(prefix)
            if prefix != disc_name and all(
                _title_key(name) == _title_key(prefix_title) for name, _ in feature_names
            ):
                title, year = prefix_title, prefix_year
                source = "feature title"
        fallback = MetadataMatch(
            title=title,
            year=year,
            media_type=kind,
            confidence=0.68 if year else 0.48,
            source=source,
        )
        offline = fallback
        if kind == "movie" and any(item.duration_seconds >= 3600 for item in titles):
            offline = _local_identity(
                fallback, feature_names, volume_named=not generic_disc_name(disc_name)
            ) or fallback
        if kind == "tv" and disc_season(disc_name) is not None:
            # Only names with a real prefix corroborate a show. A bare S01E01 or
            # an inferred episode cluster does not identify a series.
            show_names = {
                disc_title(item.name)
                for item in titles
                if (marker := SEASON_MARKER.search(item.name))
                and item.name[: marker.start()].strip()
                and item.duration_seconds >= 900
            }
            if show_names:
                offline = _local_identity(fallback, show_names, volume_named=True) or fallback
        if not self.tmdb_token or not title:
            return offline

        headers = {
            "Authorization": f"Bearer {self.tmdb_token}",
            "accept": "application/json",
        }
        params: dict[str, str | int | bool] = {
            "query": title,
            "include_adult": False,
            "language": "en-US",
            "page": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://api.themoviedb.org/3/search/multi",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                results = response.json().get("results") or []
        except (httpx.HTTPError, ValueError, TypeError):
            return offline

        required_kind = media_type or ("tv" if kind == "tv" else None)
        candidates = []
        for candidate in results:
            media_type = candidate.get("media_type")
            if media_type not in {"movie", "tv"}:
                continue
            if required_kind and media_type != required_kind:
                continue
            name = candidate.get("name" if media_type == "tv" else "title") or ""
            date = candidate.get("first_air_date" if media_type == "tv" else "release_date") or ""
            # Automatic publication requires a real provider identity and release
            # year; an incomplete search record is only a review suggestion.
            if not isinstance(candidate.get("id"), int) or candidate["id"] <= 0:
                continue
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                continue
            score = SequenceMatcher(None, title.casefold(), name.casefold()).ratio()
            if score < 0.9 or (year and not date.startswith(str(year))):
                continue
            candidates.append((score, candidate))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates or (len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.05):
            return fallback
        similarity, candidate = candidates[0]
        kind = candidate["media_type"]
        candidate_title = candidate.get("name" if kind == "tv" else "title") or title
        release_date = candidate.get("first_air_date" if kind == "tv" else "release_date") or ""
        candidate_year = int(release_date[:4]) if release_date[:4].isdigit() else year
        year_bonus = 0.08 if year and year == candidate_year else 0
        confidence = min(0.99, 0.72 + similarity * 0.2 + year_bonus)
        return MetadataMatch(
            title=candidate_title,
            year=candidate_year,
            media_type=kind,
            confidence=confidence,
            source="tmdb",
            external_id=str(candidate.get("id", "")) or None,
        )
