from __future__ import annotations

import re
from datetime import date
from pathlib import Path

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACES = re.compile(r"\s+")
YEAR = re.compile(r"(?:^|\s)(?:\(((?:19|20)\d{2})\)|((?:19|20)\d{2}))$")
DISC_SUFFIX = re.compile(r"\s+(?:DISC|DISK|D)\s*\d+$", re.IGNORECASE)
NOISE = re.compile(
    r"\b(?:UHD|ULTRA\s*HD|BLU[ -]?RAY|BDMV|RETAIL|REMUX)\b",
    re.IGNORECASE,
)


def clean_component(value: str, fallback: str = "Untitled") -> str:
    value = ILLEGAL.sub(" - ", value)
    value = re.sub(r"(?:\s*-\s*){2,}", " - ", value)
    value = SPACES.sub(" ", value).strip(" .-_")
    value = value[:180].rstrip(" .") or fallback
    # Windows reserves these stems even when followed by an extension.
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", value.split(".")[0], re.I):
        value = "_" + value
    return value


def title_from_disc_label(label: str) -> tuple[str, int | None]:
    raw = label.replace("_", " ").replace(".", " ").replace("-", " ")
    raw = NOISE.sub(" ", raw)
    raw = SPACES.sub(" ", raw).strip()
    raw = DISC_SUFFIX.sub("", raw).strip()
    match = YEAR.search(raw)
    year = None
    # A leading or standalone number is part of the title (1917, 2001).
    # Future title numbers such as Blade Runner 2049 are not release years.
    if match and raw[: match.start()].strip():
        candidate = int(match.group(1) or match.group(2))
        if candidate <= date.today().year + 1:
            year = candidate
            raw = raw[: match.start()].strip()
            raw = DISC_SUFFIX.sub("", raw).strip()
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
        base += f" - {{edition-{clean_component(edition)}}}"
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
