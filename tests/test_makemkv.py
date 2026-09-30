import pytest

from openripper.makemkv import (
    MakeMKVBackend,
    MakeMKVError,
    choose_titles,
    fingerprint_disc,
    makemkv_failure,
    parse_drives,
    parse_titles,
    rip_arguments,
    successful_title_scan,
)

ROBOT_OUTPUT = """
MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started","%1 started","MakeMKV"
DRV:0,2,999,1,"PIONEER BD-RW BDR-212U 1.02","DUNE_PART_TWO_2024_UHD","/dev/sr0"
DRV:1,2,999,0,"HL-DT-ST BD-RE WH16NS60 1.03","","/dev/sr1"
DRV:2,256,999,0,"","",""
TINFO:0,2,0,"MainFeature"
TINFO:0,8,0,"17"
TINFO:0,9,0,"2:46:00"
TINFO:0,11,0,"81250000000"
TINFO:0,16,0,"00800.mpls"
TINFO:0,27,0,"DUNE_PART_TWO_t00.mkv"
TINFO:1,30,0,"Creating the Impossible"
TINFO:1,8,0,"6"
TINFO:1,9,0,"0:22:00"
TINFO:1,11,0,"7200000000"
TINFO:1,16,0,"00120.mpls"
TINFO:1,27,0,"DUNE_PART_TWO_t01.mkv"
"""

SUCCESSFUL_SCAN_WITH_WARNINGS = """
TINFO:0,2,0,"MainFeature"
TINFO:0,9,0,"2:46:00"
MSG:3025,16777216,3,"Short title was skipped","Title skipped","00021.m2ts"
MSG:5011,0,0,"Operation successfully completed","Operation successfully completed"
"""


def test_parse_drive_inventory() -> None:
    drives = parse_drives(ROBOT_OUTPUT)
    assert len(drives) == 2
    assert drives[0].disc_name == "DUNE_PART_TWO_2024_UHD"
    assert drives[0].state == "ready"
    assert drives[1].state == "empty"
    assert drives[0].id.startswith("drive-")


def test_parse_title_metadata() -> None:
    titles = parse_titles(ROBOT_OUTPUT)
    assert len(titles) == 2
    assert titles[0].duration_seconds == 9960
    assert titles[0].size_bytes == 81_250_000_000
    assert titles[0].playlist == "00800.mpls"
    assert titles[1].name == "Creating the Impossible"


def test_smart_mode_selects_movie_main_feature() -> None:
    titles = parse_titles(ROBOT_OUTPUT)
    selected = choose_titles(titles, min_seconds=1200, mode="smart")
    assert [title.index for title in selected] == [0]
    assert titles[0].selected is True
    assert titles[1].selected is False


def test_smart_mode_selects_episodic_cluster() -> None:
    titles = parse_titles(ROBOT_OUTPUT)
    for index, seconds in enumerate([2700, 2680, 2750, 2710]):
        copy = type(titles[0])(index=index + 10, duration_seconds=seconds)
        titles.append(copy)
    selected = choose_titles(titles, min_seconds=1200, mode="smart")
    assert {title.index for title in selected} == {10, 11, 12, 13}


def test_fingerprint_is_stable() -> None:
    titles = parse_titles(ROBOT_OUTPUT)
    assert fingerprint_disc("DUNE", titles) == fingerprint_disc("DUNE", titles)
    assert fingerprint_disc("DUNE", titles) != fingerprint_disc("DUNE_2", titles)


def test_successful_title_scan_requires_completion_and_title_data() -> None:
    assert successful_title_scan(SUCCESSFUL_SCAN_WITH_WARNINGS) is True
    assert successful_title_scan('MSG:5011,0,0,"Operation successfully completed"') is False
    assert successful_title_scan('TINFO:0,2,0,"MainFeature"') is False


def test_rip_uses_same_unfiltered_title_indexes_as_scan(tmp_path) -> None:
    arguments = rip_arguments("/dev/sr0", 20, tmp_path)
    assert "--minlength=0" in arguments
    assert arguments[-4:] == ("mkv", "dev:/dev/sr0", "20", str(tmp_path))


def test_makemkv_failure_keeps_the_actionable_tail() -> None:
    messages = [f"line {index}" for index in range(20)]
    failure = makemkv_failure(messages, "No MKV was created")
    assert failure.startswith("No MKV was created\nline 8")
    assert failure.endswith("line 19")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('MSG:5053,1544,1,"Start evaluation?"\nTCOUNT:0', "MakeMKV activation required"),
        ('MSG:5010,0,0,"Disc read error"\nTCOUNT:0', "Disc read error"),
    ],
)
async def test_empty_scan_exposes_cause(monkeypatch, output, expected):
    backend = MakeMKVBackend("makemkvcon")

    async def capture(*args, **kwargs):
        return output

    monkeypatch.setattr(backend, "_capture", capture)
    with pytest.raises(MakeMKVError, match=expected):
        await backend.scan_disc("/dev/sr0")
