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
@pytest.mark.parametrize("succeed", [True, False])
async def test_encode_only_publishes_successful_output(tmp_path, monkeypatch, succeed):
    source = tmp_path / "feature.mkv"
    source.write_bytes(b"original video")
    settings = Settings(handbrake_keep_original=True)
    calls = []

    class Process:
        returncode = 0 if succeed else 1
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
    if succeed:
        result = await encode(source, settings, progress)
        assert result.name == "feature.encoded.mkv"
        assert result.read_bytes() == b"encoded video"
    else:
        with pytest.raises(MakeMKVError, match="Original retained"):
            await encode(source, settings, progress)
        assert not (tmp_path / "feature.encoded.mkv").exists()
    assert source.read_bytes() == b"original video"
    assert not (tmp_path / "feature.encoding.mkv").exists()
    assert "--all-subtitles" in calls
