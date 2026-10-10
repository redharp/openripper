"""Optional post-rip encode. Source is retained until a successful nonempty output exists."""

import asyncio
import os
import re
import shutil
import subprocess
from collections import deque
from pathlib import Path

from .makemkv import MakeMKVError, process_group_options, stop_process


def parse_presets(output: str) -> list[dict[str, str]]:
    """HandBrake's preset list uses folders and four-space nesting."""
    groups: list[tuple[int, str]] = []
    presets = []
    for line in output.splitlines():
        name = line.strip()
        if not name or name.startswith("["):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if name.endswith("/"):
            while groups and groups[-1][0] >= indent:
                groups.pop()
            groups.append((indent, name[:-1]))
        elif groups and indent == groups[-1][0] + 4:
            presets.append({"name": name, "group": " / ".join(g[1] for g in groups)})
    return presets


async def list_presets(executable: str) -> dict:
    if not shutil.which(executable):
        return {
            "available": False,
            "presets": [],
            "message": ("HandBrakeCLI not found. Save its executable path, then refresh presets."),
        }
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--preset-list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **(
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if os.name == "nt"
                else process_group_options()
            ),
        )
    except OSError as exc:
        raise MakeMKVError("HandBrakeCLI could not start to list presets") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except BaseException:
        await stop_process(process)
        raise
    presets = parse_presets(output.decode("utf-8", errors="replace"))
    if process.returncode or not presets:
        raise MakeMKVError("HandBrakeCLI did not return a usable preset list")
    return {
        "available": True,
        "presets": presets,
        "message": f"{len(presets)} presets from the installed HandBrake CLI.",
    }


async def encode(source: Path, settings, progress) -> Path:
    destination = source.with_name(f"{source.stem}.encoded.mkv")
    temporary = source.with_name(f"{source.stem}.encoding.mkv")
    if destination.exists() or temporary.exists():
        raise FileExistsError(f"Encoding output already exists: {destination.name}")
    args = [
        settings.handbrake_bin,
        "--input",
        str(source),
        "--output",
        str(temporary),
        "--preset",
        settings.handbrake_preset,
        "--format",
        "av_mkv",
        "--all-audio",
        "--aencoder",
        "copy",
        "--audio-fallback",
        "av_aac",
        "--all-subtitles",
        "--markers",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_group_options(),
        )
    except OSError as exc:
        raise MakeMKVError(
            "HandBrakeCLI could not start. The original MKV is safe in staging."
        ) from exc
    tail: deque[str] = deque(maxlen=12)
    try:
        assert process.stdout
        buffer = ""
        while chunk := await process.stdout.read(4096):
            buffer += chunk.decode(errors="replace").replace("\r", "\n")
            lines = buffer.split("\n")
            buffer = lines.pop()
            for line in lines:
                tail.append(line)
                match = re.search(r"Encoding:.*?(\d+(?:\.\d+)?) %", line)
                if match:
                    await progress(float(match.group(1)) / 100)
            buffer = buffer[-8192:]
        await process.wait()
        if process.returncode or not temporary.is_file() or not temporary.stat().st_size:
            raise MakeMKVError(
                "HandBrake encoding failed. Original retained in staging.\n" + "\n".join(tail)
            )
        temporary.rename(destination)
        if not settings.handbrake_keep_original:
            source.unlink()
        return destination
    except BaseException:
        await stop_process(process)
        temporary.unlink(missing_ok=True)
        raise
