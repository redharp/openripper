from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx

from .naming import title_from_disc_label


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

    async def resolve(self, disc_name: str) -> MetadataMatch:
        title, year = title_from_disc_label(disc_name)
        fallback = MetadataMatch(
            title=title,
            year=year,
            confidence=0.68 if year else 0.48,
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
        if year:
            params["primary_release_year"] = year
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://api.themoviedb.org/3/search/movie",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                results = response.json().get("results", [])
        except (httpx.HTTPError, ValueError, TypeError):
            return fallback

        if not results:
            return fallback
        candidate = results[0]
        candidate_title = candidate.get("title") or candidate.get("original_title") or title
        release_date = candidate.get("release_date") or ""
        candidate_year = int(release_date[:4]) if release_date[:4].isdigit() else year
        similarity = SequenceMatcher(None, title.casefold(), candidate_title.casefold()).ratio()
        year_bonus = 0.08 if year and year == candidate_year else 0
        confidence = min(0.99, 0.72 + similarity * 0.2 + year_bonus)
        return MetadataMatch(
            title=candidate_title,
            year=candidate_year,
            confidence=confidence,
            source="tmdb",
            external_id=str(candidate.get("id", "")) or None,
        )
