"""Published media artwork and local frame extraction."""

import asyncio
from pathlib import Path

from .makemkv import process_group_options, stop_process


def published_videos(job: dict) -> list[dict]:
    folder = Path(job["final_path"])
    videos = [
        title
        for title in job["titles"]
        if title["selected"]
        and title["ripped_path"]
        and Path(title["ripped_path"]).is_relative_to(folder)
    ]
    if job["media_type"] == "movie":
        # The main feature is the longest selected title, as in Library publication.
        videos.sort(key=lambda title: title["duration_seconds"], reverse=True)
        return videos[:1]
    return videos


def poster_path(job: dict) -> Path:
    folder = Path(job["final_path"])
    if job["media_type"] == "tv" and (videos := published_videos(job)):
        folder = Path(videos[0]["ripped_path"]).parent.parent
    return folder / "poster.jpg"


def thumbnail_path(video: Path, media_type: str) -> Path:
    if media_type == "tv":
        return video.with_name(f"{video.stem}-thumb.jpg")
    return video.parent / "thumb.jpg"


async def save_thumbnail(
    executable: str, video: Path, thumbnail: Path, duration_seconds: int
) -> bool:
    if thumbnail.is_file():
        return False
    seek = min(duration_seconds * 0.15, 90) if duration_seconds > 0 else 0
    image = await _video_frame(executable, video, seek)
    try:
        with thumbnail.open("xb") as output:
            output.write(image)
    except FileExistsError:
        return False
    return True


async def verify_video(executable: str, video: Path) -> None:
    try:
        await _video_frame(executable, video, 0)
    except (OSError, ValueError, TimeoutError) as exc:
        raise ValueError(f"Output verification failed for {video.name}: {exc}") from exc


async def _video_frame(executable: str, video: Path, seek: float) -> bytes:
    process = await asyncio.create_subprocess_exec(
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(seek),
        "-i",
        str(video),
        "-map",
        "0:V:0",
        "-vf",
        "scale=480:480:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-frames:v",
        "1",
        "-c:v",
        "mjpeg",
        "-f",
        "image2pipe",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **process_group_options(),
    )
    try:
        image, _ = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError as exc:
        await stop_process(process)
        raise TimeoutError("FFmpeg took too long to decode a frame") from exc
    except asyncio.CancelledError:
        await stop_process(process)
        raise
    if process.returncode or not (image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")):
        raise ValueError("FFmpeg could not decode a video frame")
    return image
