"""Validated filename templates and isolated MakeMKV track selection."""

import re
from pathlib import Path
from string import Formatter
from xml.etree import ElementTree as ET

from .naming import clean_component

FIELDS = {
    "title",
    "disc",
    "job_id",
    "year",
    "year_suffix",
    "edition_suffix",
    "season",
    "episode",
    "episode_suffix",
    "title_index",
}


def validate_template(template: str, *, unique: bool = False) -> None:
    if (
        not template.strip()
        or len(template) > 200
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', re.sub(r"\{[^}]+\}", "", template))
    ):
        raise ValueError(
            "Naming templates must be a filename, without paths or reserved characters"
        )
    seen = set()
    try:
        for _, name, spec, conversion in Formatter().parse(template):
            if name is None:
                continue
            if name not in FIELDS or conversion or spec not in {"", "02d", "03d"}:
                raise ValueError("Unsupported naming placeholder or format")
            if spec and name not in {"year", "season", "episode", "title_index"}:
                raise ValueError("Number formatting requires a numeric placeholder")
            seen.add(name)
    except ValueError as exc:
        raise ValueError(f"Invalid naming template: {exc}") from exc
    if unique and "job_id" not in seen:
        raise ValueError("Disc folder template must contain {job_id} to prevent overwrites")


def render_name(template: str, **values) -> str:
    validate_template(template)
    defaults = {name: "" for name in FIELDS}
    defaults.update(year=0, season=1, episode=1, title_index=0)
    defaults.update(values)
    return clean_component(template.format_map(defaults))


def track_profile(path: Path, *, audio: str, subtitles: str, languages: str) -> Path:
    """Literal selection on the output avoids changing MakeMKV's global preferences."""
    if not re.fullmatch(r"[a-z]{3}(,[a-z]{3})*", languages):
        raise ValueError("Use comma-separated three-letter language codes, such as eng,jpn")
    lang = languages.replace(",", "|")
    selection = "+sel:all,-sel:mvcvideo"
    if audio == "preferred":
        selection += f",-sel:audio,+sel:audio&({lang}|nolang)"
    if subtitles == "none":
        selection += ",-sel:subtitle"
    elif subtitles == "preferred":
        selection += f",-sel:subtitle,+sel:subtitle&({lang})"
    profile = ET.Element("profile")
    ET.SubElement(profile, "name", lang="eng").text = "OpenRipper track selection"
    ET.SubElement(profile, "mkvSettings", setFirstSubtitleTrackAsDefault="false")
    ET.SubElement(profile, "outputSettings", name="copy", outputFormat="directCopy")
    track = ET.SubElement(profile, "trackSettings", input="default")
    ET.SubElement(track, "output", outputSettingsName="copy", defaultSelection=selection)
    ET.ElementTree(profile).write(path, encoding="utf-8", xml_declaration=True)
    return path
