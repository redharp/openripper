from pathlib import Path

import pytest

from openripper.naming import (
    clean_component,
    movie_destination,
    title_from_disc_label,
    tv_destination,
)


def test_disc_label_normalization() -> None:
    assert title_from_disc_label("DUNE_PART_TWO_2024_UHD") == ("Dune Part Two", 2024)
    assert title_from_disc_label("THE_LORD_OF_THE_RINGS_IV_DISC_2") == (
        "The Lord of the Rings IV",
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
        "Blade Runner - 2049 (2017) - {edition-Final Cut}.mkv"
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


@pytest.mark.parametrize(
    "label, expected",
    [
        ("1917_UHD", ("1917", None)),
        ("1917_2019_UHD", ("1917", 2019)),
        ("2001_A_SPACE_ODYSSEY", ("2001 a Space Odyssey", None)),
        ("BLADE_RUNNER_2049", ("Blade Runner 2049", None)),
        ("BLADE_RUNNER_2049_2017", ("Blade Runner 2049", 2017)),
        ("THE_MAIN_EVENT_2020", ("The Main Event", 2020)),
        ("EXAMPLE_MOVIE_2020_DISC_2", ("Example Movie", 2020)),
        ("EXAMPLE_MOVIE_D2", ("Example Movie", None)),
        ("EXAMPLE_MOVIE_DISC_2_2020", ("Example Movie", 2020)),
        ("EXAMPLE_MOVIE_(2020)_UHD", ("Example Movie", 2020)),
    ],
)
def test_disc_labels_preserve_title_numbers_and_remove_disc_suffixes(label, expected):
    assert title_from_disc_label(label) == expected


@pytest.mark.parametrize("name", ["CON", "aux", "NUL.txt", "COM1", "LPT9"])
def test_windows_device_names_are_not_used_as_components(name):
    assert clean_component(name).startswith("_")


def test_truncation_does_not_leave_a_windows_invalid_trailing_dot():
    assert not clean_component("A" * 179 + ". more text").endswith(".")
