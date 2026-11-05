"""Explicit episode assignments; disc track order is not broadcast order."""

from __future__ import annotations

import re
from pathlib import Path

VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".webm"}
EPISODE_TOKEN = re.compile(r"(?<![a-z0-9])s(\d{1,3})e(\d{1,4})((?:-?e\d{1,4})*)(?!\d)", re.I)


def named_episodes(filename: str) -> set[tuple[int, int]]:
    """Recognize canonical single, repeated, and ranged SxxExx filename tokens."""
    episodes = set()
    for match in EPISODE_TOKEN.finditer(filename):
        season, previous = int(match[1]), int(match[2])
        episodes.add((season, previous))
        for extra in re.finditer(r"(-?)e(\d{1,4})", match[3], re.I):
            number = int(extra[2])
            if extra[1] and number >= previous:
                episodes.update((season, n) for n in range(previous, number + 1))
            else:
                episodes.add((season, number))
            previous = number
    return episodes


def check_existing_episodes(directory: Path, assignment: dict, source: Path) -> None:
    """A renamed or differently encoded copy still occupies the same episode."""
    if not directory.exists():
        return
    expected = {
        (assignment["season"], n)
        for n in range(assignment["episode"], assignment["episode_end"] + 1)
    }
    for existing in directory.iterdir():
        if existing == source or existing.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        overlap = expected & named_episodes(existing.stem)
        if overlap:
            season, episode = min(overlap)
            raise FileExistsError(
                f"Episode S{season:02d}E{episode:02d} already exists: {existing.name}. "
                "Review the existing episode before publishing another copy"
            )


def validate_assignments(titles: list[dict], assignments: list[dict] | None) -> dict[int, dict]:
    selected = {t["id"] for t in titles if t["selected"]}
    mapped = {}
    used = set()
    for item in assignments or []:
        title_id = item.get("title_id")
        if title_id not in selected or title_id in mapped:
            raise ValueError("Match each selected file once, using only files from this rip")
        season, episode = item.get("season"), item.get("episode")
        end = item.get("episode_end")
        end = episode if end is None else end
        if (
            type(season) is not int
            or not 0 <= season <= 999
            or type(episode) is not int
            or not 1 <= episode <= 9999
            or type(end) is not int
            or not episode <= end <= min(episode + 49, 9999)
        ):
            raise ValueError(
                "Match every file to a season and episode; episode ranges must increase"
            )
        for number in range(episode, end + 1):
            key = (season, number)
            if key in used:
                raise ValueError(f"Episode S{season:02d}E{number:02d} is assigned more than once")
            used.add(key)
        mapped[title_id] = {**item, "episode_end": end}
    if not selected or mapped.keys() != selected:
        raise ValueError("Match every selected TV file to an episode before publishing")
    return mapped


def episode_code(assignment: dict) -> str:
    code = f"S{assignment['season']:02d}E{assignment['episode']:02d}"
    if assignment["episode_end"] != assignment["episode"]:
        code += f"-E{assignment['episode_end']:02d}"
    return code
