from __future__ import annotations

import re
from pathlib import Path

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACES = re.compile(r"\s+")
YEAR = re.compile(r"(?:^|[\s._-])((?:19|20)\d{2})(?:$|[\s._-])")
NOISE = re.compile(
    r"\b(?:UHD|ULTRA\s*HD|BLU[ -]?RAY|BDMV|DISC|DISK|RETAIL|REMUX|"
    r"THEATRICAL|FEATURE|MAIN)\b",
    re.IGNORECASE,
)


def clean_component(value: str, fallback: str = "Untitled") -> str:
    value = ILLEGAL.sub(" - ", value)
    value = re.sub(r"(?:\s*-\s*){2,}", " - ", value)
    value = SPACES.sub(" ", value).strip(" .-_")
    return value[:180] or fallback


def title_from_disc_label(label: str) -> tuple[str, int | None]:
    raw = label.replace("_", " ").replace(".", " ").replace("-", " ")
    match = YEAR.search(f" {raw} ")
    year = int(match.group(1)) if match else None
    if year:
        raw = re.sub(rf"\b{year}\b", " ", raw)
    raw = NOISE.sub(" ", raw)
    raw = SPACES.sub(" ", raw).strip()
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to"}
    words = []
    for index, word in enumerate(raw.lower().split()):
        if word in {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "x"}:
            words.append(word.upper())
        elif index and word in small:
            words.append(word)
        else:
            words.append(word.capitalize())
    return clean_component(" ".join(words)), year


def movie_folder(title: str, year: int | None) -> str:
    title = clean_component(title)
    return f"{title} ({year})" if year else title


def movie_filename(title: str, year: int | None, edition: str = "") -> str:
    base = movie_folder(title, year)
    if edition:
        base += f" {{edition-{clean_component(edition)}}}"
    return f"{base}.mkv"


def movie_destination(
    root: Path,
    *,
    title: str,
    year: int | None,
    edition: str = "",
    extra_name: str | None = None,
) -> Path:
    folder = root / movie_folder(title, year)
    if extra_name:
        return folder / "Extras" / f"{clean_component(extra_name, 'Extra')}.mkv"
    return folder / movie_filename(title, year, edition)


def tv_destination(
    root: Path,
    *,
    show: str,
    year: int | None,
    season: int,
    episode: int,
    episode_title: str = "",
) -> Path:
    show_folder = movie_folder(show, year)
    filename = f"{clean_component(show)} - S{season:02d}E{episode:02d}"
    if episode_title:
        filename += f" - {clean_component(episode_title)}"
    return root / show_folder / f"Season {season:02d}" / f"{filename}.mkv"
