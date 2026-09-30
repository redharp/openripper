from __future__ import annotations

import asyncio
import csv
import hashlib
import os
import re
import signal
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .compatibility import CompatibilityInfo, parse_compatibility_info

ProgressCallback = Callable[[float, str], Awaitable[None]]


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a child process and escalate to kill if it does not exit."""
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()


def process_group_options() -> dict[str, bool]:
    return {"start_new_session": True} if os.name != "nt" else {}


@dataclass(slots=True)
class DriveInfo:
    id: str
    disc_index: int
    name: str
    device: str
    disc_name: str
    state: str
    status_text: str = ""

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(slots=True)
class TitleInfo:
    index: int
    name: str = ""
    duration_seconds: int = 0
    size_bytes: int = 0
    chapters: int = 0
    playlist: str = ""
    source_filename: str = ""
    selected: bool = False

    def to_dict(self) -> dict[str, str | int | bool]:
        return asdict(self)


class MakeMKVError(RuntimeError):
    pass


class RipperBackend(Protocol):
    async def list_drives(self) -> list[DriveInfo]: ...

    async def scan_disc(self, device: str) -> list[TitleInfo]: ...

    async def rip_title(
        self,
        device: str,
        title_index: int,
        destination: Path,
        progress: ProgressCallback,
    ) -> Path: ...

    async def eject(self, device: str) -> None: ...

    async def compatibility_info(self, device: str) -> CompatibilityInfo: ...


def _csv_fields(line: str) -> list[str]:
    _, _, payload = line.partition(":")
    return next(csv.reader([payload], skipinitialspace=False))


def _drive_id(index: int, name: str, device: str) -> str:
    stable = f"{device}|{name}" if (device or name) else f"index:{index}"
    return "drive-" + hashlib.sha256(stable.encode()).hexdigest()[:12]


def parse_duration(value: str) -> int:
    parts = value.strip().split(":")
    if not all(part.isdigit() for part in parts):
        return 0
    numbers = [int(part) for part in parts]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] if numbers else 0


def parse_drives(output: str) -> list[DriveInfo]:
    drives: list[DriveInfo] = []
    for line in output.splitlines():
        if not line.startswith("DRV:"):
            continue
        fields = _csv_fields(line)
        if len(fields) < 7 or not fields[0].isdigit():
            continue
        index = int(fields[0])
        name, disc_name, device = fields[4].strip(), fields[5].strip(), fields[6].strip()
        if not name and not device:
            continue
        has_disc = bool(disc_name)
        drives.append(
            DriveInfo(
                id=_drive_id(index, name, device),
                disc_index=index,
                name=name or f"Optical drive {index + 1}",
                device=device,
                disc_name=disc_name,
                state="ready" if has_disc else "empty",
                status_text="Disc ready" if has_disc else "Waiting for a disc",
            )
        )
    return drives


def parse_titles(output: str) -> list[TitleInfo]:
    rows: dict[int, TitleInfo] = {}
    for line in output.splitlines():
        if not line.startswith("TINFO:"):
            continue
        fields = _csv_fields(line)
        if len(fields) < 4 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        title_index, field_id = int(fields[0]), int(fields[1])
        value = fields[3].strip()
        title = rows.setdefault(title_index, TitleInfo(index=title_index))
        if field_id in {2, 30, 49} and value and not title.name:
            title.name = value
        elif field_id == 8 and value.isdigit():
            title.chapters = int(value)
        elif field_id == 9:
            title.duration_seconds = parse_duration(value)
        elif field_id == 11 and value.isdigit():
            title.size_bytes = int(value)
        elif field_id == 16:
            title.playlist = value
        elif field_id == 27:
            title.source_filename = value
    return sorted(rows.values(), key=lambda item: item.index)


def successful_title_scan(output: str) -> bool:
    """Return true when robot output proves a title scan completed successfully."""
    return "Operation successfully completed" in output and bool(parse_titles(output))


def rip_arguments(device: str, title_index: int, destination: Path) -> tuple[str, ...]:
    return (
        "--robot",
        "--cache=1024",
        "--decrypt",
        "--minlength=0",
        "--noscan",
        "mkv",
        f"dev:{device}",
        str(title_index),
        str(destination),
    )


def makemkv_failure(messages: list[str], fallback: str) -> str:
    tail = "\n".join(messages[-12:])
    return f"{fallback}\n{tail}" if tail else fallback


def fingerprint_disc(disc_name: str, titles: list[TitleInfo]) -> str:
    signature = "|".join(
        f"{title.index}:{title.duration_seconds}:{title.size_bytes}:{title.playlist}"
        for title in titles
    )
    return hashlib.sha256(f"{disc_name}|{signature}".encode()).hexdigest()


def choose_titles(
    titles: list[TitleInfo], *, min_seconds: int, mode: str = "smart"
) -> list[TitleInfo]:
    eligible = [title for title in titles if title.duration_seconds >= min_seconds]
    if not eligible:
        eligible = sorted(titles, key=lambda item: item.duration_seconds, reverse=True)[:1]
    if not eligible:
        return []
    if mode == "all":
        selected = eligible
    elif mode == "main_feature":
        selected = [max(eligible, key=lambda item: item.duration_seconds)]
    else:
        episode_like = [title for title in eligible if 18 * 60 <= title.duration_seconds <= 95 * 60]
        if len(episode_like) >= 3:
            durations = sorted(title.duration_seconds for title in episode_like)
            median = durations[len(durations) // 2]
            selected = [
                title
                for title in episode_like
                if median * 0.68 <= title.duration_seconds <= median * 1.35
            ]
            if len(selected) < 3:
                selected = [max(eligible, key=lambda item: item.duration_seconds)]
        else:
            selected = [max(eligible, key=lambda item: item.duration_seconds)]
    selected_indexes = {title.index for title in selected}
    for title in titles:
        title.selected = title.index in selected_indexes
    return selected


class MakeMKVBackend:
    def __init__(
        self,
        binary: str = "makemkvcon",
        sdf_path: Path | None = None,
    ):
        self.binary = binary
        self.sdf_path = sdf_path

    async def _capture(
        self,
        *arguments: str,
        allow_failure: bool = False,
        accept_successful_title_scan: bool = False,
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **process_group_options(),
            )
        except FileNotFoundError as exc:
            raise MakeMKVError(f"{self.binary} was not found") from exc
        try:
            stdout, _ = await process.communicate()
        except asyncio.CancelledError:
            await stop_process(process)
            raise
        output = stdout.decode(errors="replace")
        accepted_nonzero_scan = accept_successful_title_scan and successful_title_scan(output)
        if process.returncode and not allow_failure and not accepted_nonzero_scan:
            tail = "\n".join(output.splitlines()[-12:])
            raise MakeMKVError(tail or f"MakeMKV exited with code {process.returncode}")
        return output

    async def list_drives(self) -> list[DriveInfo]:
        output = await self._capture(
            "--robot", "--cache=1", "--noscan", "info", "disc:9999", allow_failure=True
        )
        return parse_drives(output)

    async def scan_disc(self, device: str) -> list[TitleInfo]:
        output = await self._capture(
            "--robot",
            "--cache=1",
            "--minlength=0",
            "--noscan",
            "info",
            f"dev:{device}",
            accept_successful_title_scan=True,
        )
        titles = parse_titles(output)
        if not titles:
            messages = [line for line in output.splitlines() if line.startswith("MSG:")]
            if any(line.startswith("MSG:5053,") for line in messages):
                raise MakeMKVError(
                    "MakeMKV activation required. Open MakeMKV on this host and activate "
                    "your license or start its evaluation, then retry this job."
                )
            raise MakeMKVError(
                makemkv_failure(messages, "MakeMKV found no usable titles on the disc")
            )
        return titles

    async def rip_title(
        self,
        device: str,
        title_index: int,
        destination: Path,
        progress: ProgressCallback,
    ) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        before = set(destination.glob("*.mkv"))
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                *rip_arguments(device, title_index, destination),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **process_group_options(),
            )
        except FileNotFoundError as exc:
            raise MakeMKVError(f"{self.binary} was not found") from exc
        assert process.stdout
        messages: list[str] = []
        log_path = destination / "makemkv.log"
        progress_pattern = re.compile(r"^PRGV:(\d+),(\d+),(\d+)")
        try:
            with log_path.open("w", encoding="utf-8", buffering=1) as output_log:
                async for raw_line in process.stdout:
                    line = raw_line.decode(errors="replace").rstrip()
                    output_log.write(f"{line}\n")
                    messages.append(line)
                    messages = messages[-30:]
                    match = progress_pattern.match(line)
                    if match:
                        current, total, maximum = (int(value) for value in match.groups())
                        denominator = maximum or total or 1
                        await progress(min(1.0, current / denominator), "Ripping title")
            return_code = await process.wait()
        except asyncio.CancelledError:
            await stop_process(process)
            raise
        if return_code:
            raise MakeMKVError(makemkv_failure(messages, "MakeMKV rip failed"))
        created = list(set(destination.glob("*.mkv")) - before)
        if not created:
            raise MakeMKVError(
                makemkv_failure(messages, "MakeMKV finished but no MKV file was created")
            )
        log_path.unlink(missing_ok=True)
        return max(created, key=lambda path: path.stat().st_mtime)

    async def eject(self, device: str) -> None:
        if not device:
            return
        if os.name == "nt":
            if not re.fullmatch(r"[A-Za-z]:", device):
                raise MakeMKVError(f"Unable to open the tray for {device}")
            # Same as Explorer's right-click > Eject on the drive letter.
            command = (
                "(New-Object -ComObject Shell.Application).Namespace(17)"
                f".ParseName('{device.upper()}').InvokeVerb('Eject')"
            )
            arguments = ("powershell", "-NoProfile", "-NonInteractive", "-Command", command)
        else:
            arguments = ("eject", device)
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return_code = await process.wait()
        if return_code:
            raise MakeMKVError(f"Unable to open the tray for {device}")

    async def compatibility_info(self, device: str) -> CompatibilityInfo:
        arguments = ["f"]
        # Windows MakeMKV locates its own SDF; only pass one that exists.
        if self.sdf_path and self.sdf_path.is_file():
            arguments.extend(["-f", str(self.sdf_path)])
        arguments.extend(["-d", device, "--info"])
        output = await self._capture(*arguments)
        sysfs = Path("/sys/class/block") / Path(device).name / "device"

        def read_identity(name: str) -> str:
            try:
                return (sysfs / name).read_text(encoding="utf-8").strip()
            except OSError:
                return ""

        return parse_compatibility_info(
            output,
            manufacturer=read_identity("vendor"),
            product=read_identity("model"),
            revision=read_identity("rev"),
        )


class SimulationBackend:
    async def list_drives(self) -> list[DriveInfo]:
        return [
            DriveInfo(
                id="drive-demo-pioneer",
                disc_index=0,
                name="PIONEER BD-RW BDR-212U",
                device="/dev/sr0",
                disc_name="DUNE_PART_TWO_2024_UHD",
                state="ready",
                status_text="Disc ready",
            ),
            DriveInfo(
                id="drive-demo-lg",
                disc_index=1,
                name="HL-DT-ST BD-RE WH16NS60",
                device="/dev/sr1",
                disc_name="",
                state="empty",
                status_text="Waiting for a disc",
            ),
        ]

    async def scan_disc(self, device: str) -> list[TitleInfo]:
        await asyncio.sleep(0.8)
        return [
            TitleInfo(
                index=0,
                name="MainFeature",
                duration_seconds=9960,
                size_bytes=81_250_000_000,
                chapters=17,
                playlist="00800.mpls",
                source_filename="DUNE_PART_TWO_t00.mkv",
            ),
            TitleInfo(
                index=1,
                name="Creating the Impossible",
                duration_seconds=1320,
                size_bytes=7_200_000_000,
                chapters=6,
                playlist="00120.mpls",
                source_filename="DUNE_PART_TWO_t01.mkv",
            ),
        ]

    async def rip_title(
        self,
        device: str,
        title_index: int,
        destination: Path,
        progress: ProgressCallback,
    ) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        for step in range(1, 11):
            await asyncio.sleep(0.25)
            await progress(step / 10, f"Demo rip · title {title_index}")
        output = destination / f"demo_t{title_index:02d}.mkv"
        output.write_bytes(b"OPENRIPPER SIMULATION\n")
        return output

    async def eject(self, device: str) -> None:
        await asyncio.sleep(0.1)

    async def compatibility_info(self, device: str) -> CompatibilityInfo:
        await asyncio.sleep(0.1)
        if device == "/dev/sr0":
            return CompatibilityInfo(
                manufacturer="PIONEER",
                product="BD-RW BDR-212UBK",
                revision="1.02",
                firmware_version="1.02",
                libredrive_status="Enabled",
                bd_raw_data_read=True,
                status="ready",
                message="Rips 4K UHD, Blu-ray and DVD",
            )
        return CompatibilityInfo(
            manufacturer="HL-DT-ST",
            product="BD-RE WH16NS60",
            revision="1.05",
            firmware_version="1.05",
            libredrive_status="Possible, not yet enabled",
            status="needs_firmware",
            message=(
                "UHD-capable drive, but LibreDrive isn't enabled on this firmware. "
                "Blu-ray and DVD rip normally."
            ),
        )
