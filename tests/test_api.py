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


def test_clear_failures_keeps_files_and_does_not_touch_running_jobs(tmp_path):
    from fastapi.testclient import TestClient

    from openripper.api import create_app
    from openripper.config import Settings

    app = create_app(
        Settings(
            library_root=tmp_path / "library",
            movie_root=tmp_path / "movies",
            tv_root=tmp_path / "tv",
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            simulate=True,
            auto_rip=False,
            compatibility_check=False,
            udev_discovery=False,
            poll_interval=999,
        )
    )
    with TestClient(app) as client:
        client.post("/api/poll")
        db = app.state.database
        drive = client.get("/api/overview").json()["drives"][0]
        partial = tmp_path / "partial.mkv"
        partial.write_bytes(b"keep these bytes")
        for job_id, status in [("bad1", "failed"), ("bad2", "failed"), ("ok", "complete")]:
            db.create_job(
                dict(
                    id=job_id,
                    drive_id=drive["id"],
                    disc_name="Test",
                    fingerprint=job_id,
                    status=status,
                    stage_path=str(tmp_path),
                )
            )
        assert client.post("/api/jobs/ok/dismiss").status_code == 409
        assert client.post("/api/jobs/missing/dismiss").status_code == 404
        assert client.post("/api/jobs/bad1/dismiss").json() == {"cleared": 1}
        assert client.post("/api/jobs/failed/clear").json() == {"cleared": 1}
        assert client.post("/api/jobs/failed/clear").json() == {"cleared": 0}
        overview = client.get("/api/overview").json()
        assert overview["totals"]["failed"] == 0
        assert [j["id"] for j in overview["history"]] == ["ok"]
        assert overview["active_jobs"][0]["status"] == "awaiting_start"
        assert db.job_detail("bad1") is not None
        assert partial.read_bytes() == b"keep these bytes"


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
