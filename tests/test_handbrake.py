import asyncio

import pytest

from openripper.config import Settings
from openripper.handbrake import encode
from openripper.makemkv import MakeMKVError


def test_preset_parser_keeps_names_and_groups_not_descriptions():
    from openripper.handbrake import parse_presets

    result = parse_presets("""[03:01] Starting HandBrake
General/
    Fast 1080p30
        Description of this preset.
        Wrapped description.
    HQ 2160p60 4K HEVC Surround
        Another description.
Devices/
    Apple 1080p30 Surround
        Device support
HandBrake has exited.
""")
    assert result == [
        {"group": "General", "name": "Fast 1080p30"},
        {"group": "General", "name": "HQ 2160p60 4K HEVC Surround"},
        {"group": "Devices", "name": "Apple 1080p30 Surround"},
    ]


@pytest.mark.asyncio
async def test_missing_cli_returns_help_without_running_anything(monkeypatch):
    from openripper.handbrake import list_presets

    monkeypatch.setattr("openripper.handbrake.shutil.which", lambda _: None)
    result = await list_presets("unavailable")
    assert result["available"] is False
    assert result["presets"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["success", "encode failure", "verification failure"])
async def test_encode_only_publishes_successful_output(tmp_path, monkeypatch, result):
    source = tmp_path / "feature.mkv"
    source.write_bytes(b"original video")
    settings = Settings(handbrake_keep_original=result != "verification failure")
    calls = []

    class Process:
        returncode = 1 if result == "encode failure" else 0
        stdout = asyncio.StreamReader()

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        calls.extend(args)
        (tmp_path / "feature.encoding.mkv").write_bytes(b"encoded video")
        Process.stdout.feed_data(b"Encoding: task 1 of 1, 50.00 %\n")
        Process.stdout.feed_eof()
        return Process()

    async def progress(value):
        assert value == 0.5

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async def verify(video):
        assert video.read_bytes() == b"encoded video"
        assert source.is_file()
        raise ValueError("Output verification failed")

    if result == "success":
        output = await encode(source, settings, progress)
        assert output.name == "feature.encoded.mkv"
        assert output.read_bytes() == b"encoded video"
    elif result == "verification failure":
        with pytest.raises(ValueError, match="Output verification failed"):
            await encode(source, settings, progress, verify=verify)
        assert not (tmp_path / "feature.encoded.mkv").exists()
    else:
        with pytest.raises(MakeMKVError, match="Original retained"):
            await encode(source, settings, progress)
        assert not (tmp_path / "feature.encoded.mkv").exists()
    assert source.read_bytes() == b"original video"
    assert not (tmp_path / "feature.encoding.mkv").exists()
    assert "--all-subtitles" in calls
