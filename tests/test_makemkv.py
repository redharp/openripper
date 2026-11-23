import asyncio
import sys

import pytest

from openripper.makemkv import (
    MakeMKVBackend,
    MakeMKVError,
    TitleInfo,
    ambiguous_title_pairs,
    choose_titles,
    fingerprint_disc,
    makemkv_failure,
    parse_drives,
    parse_titles,
    rip_arguments,
    successful_title_scan,
    unique_titles,
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
TINFO:0,26,0,"510,511,512"
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
    assert titles[0].segment_map == "510,511,512"
    assert titles[1].name == "Creating the Impossible"


def test_smart_mode_selects_movie_main_feature() -> None:
    titles = parse_titles(ROBOT_OUTPUT)
    selected = choose_titles(titles, min_seconds=1200, mode="smart")
    assert [title.index for title in selected] == [0]
    assert titles[0].selected is True
    assert titles[1].selected is False


def test_smart_movie_does_not_replace_the_feature_with_an_extras_cluster() -> None:
    titles = [
        TitleInfo(index=index, duration_seconds=duration)
        for index, duration in enumerate((5100, 1350, 1400, 1450))
    ]
    selected = choose_titles(titles, min_seconds=1200, mode="smart", media_type="movie")
    assert [title.index for title in selected] == [0]


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


@pytest.mark.asyncio
async def test_capture_preserves_a_successful_robot_scan_with_warning_exit():
    python = getattr(sys, "_base_executable", sys.executable)
    backend = MakeMKVBackend(python)
    output = await backend._capture(
        "-c",
        f"print({SUCCESSFUL_SCAN_WITH_WARNINGS!r}); raise SystemExit(1)",
        operation="disc scan",
        timeout=10,
        accept_successful_title_scan=True,
    )
    assert parse_titles(output)[0].name == "MainFeature"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["overflow", "timeout", "cancel"])
async def test_capture_stops_a_real_child_and_keeps_scan_diagnostics(monkeypatch, stop):
    python = getattr(sys, "_base_executable", sys.executable)
    backend = MakeMKVBackend(python)
    native_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    child = None

    async def spawn(*args, **kwargs):
        nonlocal child
        child = await native_spawn(*args, **kwargs)
        spawned.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("openripper.makemkv.CAPTURE_LIMIT_BYTES", 256 * 1024)
    if stop == "overflow":
        script = (
            "import sys\nwhile True:\n"
            "\tsys.stdout.buffer.write(b'MSG:2003,0,0,\"Disc read error\"\\n' * 2000)\n"
            "\tsys.stdout.buffer.flush()"
        )
    else:
        script = 'import time; print("Drive has not returned", flush=True); time.sleep(20)'

    task = asyncio.create_task(
        backend._capture(
            "-c", script, operation="disc scan", timeout=2 if stop == "timeout" else 10
        )
    )
    if stop == "cancel":
        await asyncio.wait_for(spawned.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    else:
        message = "too much output" if stop == "overflow" else "timed out"
        evidence = "Disc read error" if stop == "overflow" else "Drive has not returned"
        with pytest.raises(MakeMKVError, match=message) as error:
            await asyncio.wait_for(task, timeout=12)
        assert str(error.value).count(evidence) == 1
        assert len(str(error.value)) < 1000
    assert child is not None and child.returncode is not None


def episodic_alternates():
    return [
        TitleInfo(
            index=i,
            duration_seconds=seconds,
            size_bytes=size,
            playlist=playlist,
            segment_map=segments,
            chapters=chapters,
        )
        for i, seconds, size, playlist, segments, chapters in [
            (4, 4293, 35_646_842_880, "00801.mpls", "56", 11),
            (11, 4293, 35_646_842_880, "00201.mpls", "56", 11),
            (71, 4293, 35_646_842_880, "00056.m2ts", "", 0),
            (3, 3756, 31_082_194_944, "00802.mpls", "57", 8),
            (74, 3756, 31_082_194_944, "00057.m2ts", "", 0),
            (2, 3232, 26_790_211_584, "00803.mpls", "68", 9),
            (73, 3232, 26_790_211_584, "00068.m2ts", "", 0),
        ]
    ]


def test_smart_tv_selects_authored_episodes_once():
    titles = episodic_alternates()
    selected = choose_titles(titles, min_seconds=1200, media_type="tv")
    assert {t.index for t in selected} == {2, 3, 4}
    assert sum(t.size_bytes for t in selected) == 93_519_249_408
    assert not ambiguous_title_pairs(selected)
    assert {t.index for t in titles if t.selected} == {2, 3, 4}


def test_equal_size_runtime_is_not_proof_of_duplicate_content():
    titles = [
        TitleInfo(index=i, duration_seconds=2700, size_bytes=1000, segment_map=segments)
        for i, segments in enumerate(["1,2", "2,1", "3,4"])
    ]
    assert len(unique_titles(titles)) == 3
    assert not ambiguous_title_pairs(titles)
    titles[0].segment_map = ""
    assert len(unique_titles(titles)) == 3
    assert ambiguous_title_pairs(titles)


def test_missing_maps_preserve_all_alternates_for_review():
    titles = episodic_alternates()
    for title in titles:
        title.segment_map = ""
    selected = choose_titles(titles, min_seconds=1200, media_type="tv")
    assert len(selected) == 7
    assert ambiguous_title_pairs(selected)


def test_all_mode_preserves_alternates_and_tv_can_select_two_episodes():
    assert len(choose_titles(episodic_alternates(), min_seconds=1200, mode="all")) == 7
    episodes = [TitleInfo(index=i, duration_seconds=2700 + i) for i in range(2)]
    assert len(choose_titles(episodes, min_seconds=1200, media_type="tv")) == 2


@pytest.mark.asyncio
async def test_read_errors_are_reported_while_process_is_still_running(tmp_path, monkeypatch):
    stream = asyncio.StreamReader()
    stream.feed_data(b'PRGV:100,100,1000\nMSG:2003,0,0,"Read failed"\n')
    stream.feed_eof()
    reports = []

    class Process:
        stdout = stream

        async def wait(self):
            assert reports[-1] == (0, "Disc read error; MakeMKV is retrying")
            (tmp_path / "test.mkv").write_bytes(b"video")
            return 0

    async def spawn(*args, **kwargs):
        return Process()

    async def report(value, message):
        reports.append((value, message))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert (
        await MakeMKVBackend().rip_title("/dev/sr0", 0, tmp_path, report) == tmp_path / "test.mkv"
    )
    assert reports[0] == (0.1, "Ripping title")
