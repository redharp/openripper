from pathlib import Path

from openripper.naming import (
    clean_component,
    movie_destination,
    title_from_disc_label,
    tv_destination,
)


def test_disc_label_normalization() -> None:
    assert title_from_disc_label("DUNE_PART_TWO_2024_UHD") == ("Dune Part Two", 2024)
    assert title_from_disc_label("THE_LORD_OF_THE_RINGS_IV_DISC_2") == (
        "The Lord of the Rings IV 2",
        None,
    )


def test_movie_naming_is_plex_and_jellyfin_safe() -> None:
    path = movie_destination(
        Path("/media/library"),
        title="Blade Runner: 2049",
        year=2017,
        edition="Final Cut",
    )
    assert path == Path(
        "/media/library/Blade Runner - 2049 (2017)/"
        "Blade Runner - 2049 (2017) {edition-Final Cut}.mkv"
    )


def test_tv_naming() -> None:
    path = tv_destination(
        Path("/media/tv"),
        show="Chernobyl",
        year=2019,
        season=1,
        episode=3,
        episode_title="Open Wide, O Earth",
    )
    assert path == Path(
        "/media/tv/Chernobyl (2019)/Season 01/Chernobyl - S01E03 - Open Wide, O Earth.mkv"
    )


def test_windows_unsafe_characters_are_removed() -> None:
    assert clean_component('A <Bad>: "Name"?') == "A - Bad - Name"
