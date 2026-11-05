from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path


def parse_blkid_label(output: str) -> str:
    match = re.search(r'\bLABEL="([^"]+)"', output)
    return match.group(1).strip() if match else ""


# DriveInfo answers from the volume manager; Win32_CDROMDrive can stall while
# MakeMKV holds the drive.
WINDOWS_MEDIA_QUERY = (
    "[System.IO.DriveInfo]::GetDrives() | "
    "Where-Object { $_.DriveType -eq 'CDRom' -and $_.IsReady } | "
    "ForEach-Object { $_.Name.Substring(0, 2) + '|' + $_.VolumeLabel }"
)


def parse_windows_media(output: str) -> dict[str, str]:
    """Parse `F:|LABEL` lines; a loaded disc without a label still counts."""
    labels: dict[str, str] = {}
    for line in output.splitlines():
        drive, _, label = line.strip().partition("|")
        if re.fullmatch(r"[A-Za-z]:", drive):
            labels[drive.upper()] = label.strip() or f"DISC_{drive[0].upper()}"
    return labels


def windows_media_labels() -> dict[str, str]:
    """Return loaded optical media labels keyed by drive letter, e.g. `F:`."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_MEDIA_QUERY],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A drive that is still spinning up can stall the query; retry next poll.
        return {}
    return parse_windows_media(result.stdout)


def linux_disc_loaded(device: str) -> bool:
    """Ask the drive, not cached block capacity or udev properties, about media."""
    try:
        import fcntl

        descriptor = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except (ImportError, OSError):
        return False
    try:
        # linux/cdrom.h: CDROM_DRIVE_STATUS, CDSL_CURRENT, CDS_DISC_OK.
        # O_NONBLOCK allows querying an empty/open tray without closing it.
        return fcntl.ioctl(descriptor, 0x5326, 0x7FFFFFFF) == 4
    except OSError:
        # Not ready, unsupported ioctls and permission failures are not media.
        return False
    finally:
        with suppress(OSError):
            os.close(descriptor)


def optical_media_labels() -> dict[str, str]:
    """Return loaded optical media labels keyed by device (/dev/sr* or a drive letter)."""
    if os.name == "nt":
        return windows_media_labels()

    labels: dict[str, str] = {}
    for device in Path("/dev").glob("sr*"):
        name = str(device)
        if not re.fullmatch(r"sr\d+", device.name) or not linux_disc_loaded(name):
            continue
        try:
            result = subprocess.run(
                # Low-level probing bypasses the blkid cache, which may still
                # describe an earlier disc. Udev labels can be stale too.
                ["blkid", "-p", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        label = parse_blkid_label(result.stdout) if result.returncode == 0 else ""
        # A user may eject the disc during the label probe. Unlabelled loaded
        # media is valid, but only another positive drive-status check proves it.
        if linux_disc_loaded(name):
            labels[name] = label or f"DISC_{device.name}"
    return labels


async def optical_hotplug_events() -> AsyncIterator[str]:
    """Yield Linux optical-device events; quietly idle on unsupported platforms."""
    if os.name == "nt":
        while True:
            await asyncio.sleep(3600)

    try:
        import pyudev

        context = pyudev.Context()
    except (ImportError, OSError):
        while True:
            await asyncio.sleep(3600)

    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="block")
    monitor.start()
    while True:
        device = await asyncio.to_thread(monitor.poll, 1)
        if device is None:
            await asyncio.sleep(0)
            continue
        name = device.get("DEVNAME", "")
        optical = (
            name.startswith("/dev/sr")
            or device.get("ID_CDROM") == "1"
            or device.get("ID_CDROM_MEDIA") == "1"
        )
        if optical:
            yield f"{device.action or 'change'}:{name}"
