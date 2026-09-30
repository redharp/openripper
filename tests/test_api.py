import json
from datetime import UTC, datetime

from openripper.api import websocket_payload


def test_websocket_payload_serializes_database_datetimes() -> None:
    payload = websocket_payload(
        {
            "drives": [{"id": "drive-1", "updated_at": datetime(2026, 7, 25, tzinfo=UTC)}],
            "active_jobs": [],
            "history": [],
        }
    )

    assert payload["drives"][0]["updated_at"] == "2026-07-25T00:00:00+00:00"
    json.dumps(payload)


def test_drives_show_compatibility_and_flashing_is_gone(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from openripper.api import create_app
    from openripper.config import Settings

    settings = Settings(
        library_root=tmp_path / "library",
        movie_root=tmp_path / "library" / "Movies",
        tv_root=tmp_path / "library" / "TV",
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        poll_interval=999,
        auto_rip=False,
        udev_discovery=False,
        simulate=True,
    )
    with TestClient(create_app(settings)) as client:
        client.post("/api/poll", json={})
        drives = {drive["id"]: drive for drive in client.get("/api/overview").json()["drives"]}
        assert drives["drive-demo-pioneer"]["uhd_status"] == "ready"
        assert drives["drive-demo-lg"]["uhd_status"] == "needs_firmware"
        assert "flash_candidate" not in drives["drive-demo-lg"]

        rechecked = client.post("/api/drives/drive-demo-lg/compatibility")
        assert rechecked.status_code == 200
        assert rechecked.json()["uhd_status"] == "needs_firmware"
        assert client.post("/api/drives/drive-demo-lg/firmware/flash", json={}).status_code == 404
        assert client.post("/api/drives/nope/compatibility").status_code == 404
