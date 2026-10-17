from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import httpx

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


@dataclass(slots=True)
class MetadataMatch:
    title: str
    year: int | None
    media_type: str = "movie"
    confidence: float = 0.0
    source: str = "disc-label"
    external_id: str | None = None


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
            for candidate in response.json().get("results", [])[:5]:
                name = candidate.get("name" if kind == "tv" else "title", "")
                date = candidate.get("first_air_date" if kind == "tv" else "release_date", "")
                poster = candidate.get("poster_path") or ""
                if SequenceMatcher(None, title.casefold(), name.casefold()).ratio() < 0.95:
                    continue
                if year and not date.startswith(str(year)):
                    continue
                if not re.fullmatch(r"/[A-Za-z0-9_-]+\.jpg", poster):
                    continue
                image = await client.get(f"https://image.tmdb.org/t/p/w500{poster}")
                image.raise_for_status()
                if len(image.content) > 8_000_000 or not image.content.startswith(b"\xff\xd8"):
                    raise ValueError("Invalid poster image")
                with (folder / "poster.jpg").open("xb") as output:
                    output.write(image.content)
                return True
        return False

    async def resolve(self, disc_name: str, titles=()) -> MetadataMatch:
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
        fallback = MetadataMatch(
            title=title,
            year=year,
            media_type=kind,
            confidence=0.68 if year else 0.48,
            source=source,
        )
        if not self.tmdb_token or not title:
            return fallback

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
                results = response.json().get("results", [])
        except (httpx.HTTPError, ValueError, TypeError):
            return fallback

        candidates = []
        for candidate in results:
            media_type = candidate.get("media_type")
            if media_type not in {"movie", "tv"}:
                continue
            if source == "season marker" and media_type != "tv":
                continue
            name = candidate.get("name" if media_type == "tv" else "title") or ""
            date = candidate.get("first_air_date" if media_type == "tv" else "release_date") or ""
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
