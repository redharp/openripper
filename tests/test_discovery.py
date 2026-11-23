import errno
import subprocess
import sys
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openripper import discovery
from openripper.discovery import parse_blkid_label, parse_windows_media


def test_parse_blkid_label() -> None:
    output = '/dev/sr0: LABEL="THE_BATMAN" TYPE="udf"\n'

    assert parse_blkid_label(output) == "THE_BATMAN"
    assert parse_blkid_label('/dev/sr2: TYPE="udf"\n') == ""


def test_parse_windows_media_keys_by_drive_letter() -> None:
    output = "F:|UltramanACE\r\ng:|\r\nnot a drive\r\n"

    assert parse_windows_media(output) == {"F:": "UltramanACE", "G:": "DISC_G"}


def fake_linux(monkeypatch, statuses, *, output='', returncode=0):
    """Provide stale sysfs/udev evidence plus independent hardware responses."""
    reads = []

    class OpticalPath(PurePosixPath):
        def glob(self, pattern):
            return [OpticalPath(name) for name in statuses]

        def read_text(self, **kwargs):
            reads.append(str(self))
            return "2097151\n"  # An empty optical drive can retain its old capacity.

    handles = {}

    def open_device(name, flags):
        assert flags == 2048  # O_RDONLY | O_NONBLOCK; never write or close the tray.
        descriptor = len(handles) + 10
        handles[descriptor] = name
        return descriptor

    def ioctl(descriptor, command, slot):
        assert (command, slot) == (0x5326, 0x7FFFFFFF)
        status = statuses[handles[descriptor]]
        if isinstance(status, list):
            status = status.pop(0)
        if isinstance(status, OSError):
            raise status
        return status

    platform = SimpleNamespace(
        name="posix", O_RDONLY=0, O_NONBLOCK=2048,
        open=Mock(side_effect=open_device), close=Mock(),
    )
    stale = [
        SimpleNamespace(
            device_node=name, sys_name=PurePosixPath(name).name,
            get={"ID_CDROM_MEDIA": "1", "ID_FS_LABEL": "PREVIOUS_DISC"}.get,
        ) for name in statuses
    ]
    udev = SimpleNamespace(Context=Mock(return_value=SimpleNamespace(
        list_devices=lambda **kwargs: stale
    )))
    monkeypatch.setitem(sys.modules, "pyudev", udev)
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(ioctl=ioctl))
    monkeypatch.setattr(discovery, "os", platform)
    monkeypatch.setattr(discovery, "Path", OpticalPath)
    probe = Mock(return_value=subprocess.CompletedProcess([], returncode, stdout=output))
    monkeypatch.setattr(discovery.subprocess, "run", probe)
    return platform, probe, reads, udev


@pytest.mark.parametrize("status", [0, 1, 2, 3, 5])
def test_empty_open_or_unready_drives_ignore_stale_capacity_and_udev(monkeypatch, status):
    platform, probe, reads, udev = fake_linux(monkeypatch, {"/dev/sr0": status})
    assert discovery.optical_media_labels() == {}
    probe.assert_not_called()
    assert reads == []
    udev.Context.assert_not_called()
    platform.close.assert_called_once_with(10)


def test_loaded_unlabelled_media_is_detected_only_from_drive_status(monkeypatch):
    platform, probe, _, _ = fake_linux(monkeypatch, {"/dev/sr3": 4}, returncode=2)
    assert discovery.optical_media_labels() == {"/dev/sr3": "DISC_sr3"}
    assert probe.call_args.args[0] == ["blkid", "-p", "/dev/sr3"]
    assert platform.close.call_count == 2


def test_current_probe_label_replaces_stale_udev_label(monkeypatch):
    fake_linux(monkeypatch, {"/dev/sr0": 4}, output='/dev/sr0: LABEL="NEW_DISC" TYPE="udf"')
    assert discovery.optical_media_labels() == {"/dev/sr0": "NEW_DISC"}


def test_ejection_during_label_probe_does_not_queue_old_media(monkeypatch):
    fake_linux(monkeypatch, {"/dev/sr0": [4, 2]}, output='/dev/sr0: LABEL="OLD_DISC"')
    assert discovery.optical_media_labels() == {}


@pytest.mark.parametrize(
    "error", [getattr(errno, "ENOMEDIUM", 123), errno.EIO, errno.ENOTTY, errno.EACCES]
)
def test_ioctl_failures_never_imply_loaded_media(monkeypatch, error):
    platform, probe, _, _ = fake_linux(monkeypatch, {"/dev/sr0": OSError(error, "failed")})
    assert discovery.optical_media_labels() == {}
    platform.close.assert_called_once_with(10)
    probe.assert_not_called()


def test_open_failure_never_probes_or_closes_an_invalid_descriptor(monkeypatch):
    platform, probe, _, _ = fake_linux(monkeypatch, {"/dev/sr0": 4})
    platform.open.side_effect = PermissionError("denied")
    assert discovery.optical_media_labels() == {}
    platform.close.assert_not_called()
    probe.assert_not_called()


def test_label_probe_timeout_is_conservative(monkeypatch):
    _, probe, _, _ = fake_linux(monkeypatch, {"/dev/sr0": 4})
    probe.side_effect = subprocess.TimeoutExpired("blkid", 10)
    assert discovery.optical_media_labels() == {}


def test_each_drive_has_independent_presence(monkeypatch):
    fake_linux(monkeypatch, {"/dev/sr0": 1, "/dev/sr1": 2, "/dev/sr2": 4, "/dev/sr3": 4})
    assert discovery.optical_media_labels() == {"/dev/sr2": "DISC_sr2", "/dev/sr3": "DISC_sr3"}


def test_windows_still_uses_volume_manager_without_linux_probe(monkeypatch):
    monkeypatch.setattr(discovery, "os", SimpleNamespace(name="nt"))
    windows = Mock(return_value={"F:": "MOVIE"})
    linux = Mock(side_effect=AssertionError("Linux probe ran on Windows"))
    monkeypatch.setattr(discovery, "windows_media_labels", windows)
    monkeypatch.setattr(discovery, "linux_disc_loaded", linux)
    assert discovery.optical_media_labels() == {"F:": "MOVIE"}
    linux.assert_not_called()
